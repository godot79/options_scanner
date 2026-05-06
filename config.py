"""
config.py
---------
Central configuration.  Change values directly in this file.

Design rule: operational parameters (thresholds, bands, intervals) are
plain literals here — no env-var override.  This prevents .env files from
silently overriding tuned values and makes the running configuration fully
readable in one place.

The .env file is for infrastructure secrets only:
  IB_HOST, IB_PORT, IB_CLIENT_ID, LOG_DIR, CACHE_DIR
Those three IB connection values still read from env so the same codebase
can point at paper vs live gateway without touching source.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))

def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))

def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)

def _env_bool(key: str, default: bool) -> bool:
    return os.environ.get(key, str(default)).lower() in ('1', 'true', 'yes')

# ── IB Connection ─────────────────────────────────────────────────────────────

IB_HOST      : str = _env_str('IB_HOST', '127.0.0.1')
IB_PORT      : int = _env_int('IB_PORT', 4002)
IB_CLIENT_ID : int = _env_int('IB_CLIENT_ID', 42)

# ── Scan parameters ───────────────────────────────────────────────────────────

SCAN_INTERVAL_SEC       : int   = 60
INSTRUMENT_STAGGER_SEC  : float = 3.0
MAX_EXPIRY_DAYS         : int   = 30
MONEYNESS_BAND          : float = 0.10
RISK_FREE_RATE          : float = 0.05
MARKET_DATA_TIMEOUT_SEC : float = 8.0
IB_BATCH_SIZE           : int   = 50

# ── Streaming ticks (Option A) ────────────────────────────────────────────────
# False = Option B: volume delta between snapshots (default, lower overhead)
# True  = Option A: reqTickByTickData streaming (richer fingerprinting)
USE_STREAMING_TICKS : bool = False

# ── Instruments ───────────────────────────────────────────────────────────────
# secType : 'FOP' (futures options) | 'OPT' (equity options)
# model   : 'black76' | 'bsm' | 'merton_jd'
# Jump-diffusion params (only used when model='merton_jd'):
#   jd_lambda  : average number of jumps per year
#   jd_mu_j    : mean log-jump size
#   jd_sigma_j : std-dev of log-jump size
# div_yield    : continuous dividend yield (equity only; 0.0 for TSLA)
# use_und_price_field : try undPrice from option ticker before requesting STK quote

INSTRUMENTS: dict[str, dict] = {
    'CL': {
        'symbol'               : 'CL',
        'secType'              : 'FOP',
        'exchange'             : 'NYMEX',
        'currency'             : 'USD',
        'model'                : 'black76',
        'description'          : 'Crude Oil Futures Options',
        'jd_lambda'            : 0.1,
        'jd_mu_j'              : 0.0,
        'jd_sigma_j'           : 0.1,
        # None = include all series (LO monthly + WL/XL weekly + ML/NL daily).
        # With MONEYNESS_BAND=0.10 the in-band count is ~700 contracts — manageable.
        # Previously ['LO'] only returned 2 monthly expiries, making the
        # expiry_concentration fingerprint fire spuriously (100% of volume in
        # one expiry because there WAS only one expiry with volume).
        'preferred_trading_classes': None,
        # CL futures tradingClass — prevents mini/micro contracts being returned
        'futures_trading_class'    : 'CL',
    },
    'SI': {
        'symbol'               : 'SI',
        'secType'              : 'FOP',
        'exchange'             : 'COMEX',
        'currency'             : 'USD',
        'model'                : 'black76',
        'description'          : 'Silver Futures Options',
        'jd_lambda'            : 0.1,
        'jd_mu_j'              : 0.0,
        'jd_sigma_j'           : 0.1,
        # All SI series pulled into cache — with recent volatility even weekly
        # series are seeing real volume. Moneyness pre-filter in the scan loop
        # keeps snapshot requests manageable regardless of total cache size.
        # SO       = standard monthly silver options (primary, 23K+ contracts)
        # SO1-SO4  = weekly expirations off the front month
        # S1T-S4T  = shorter-dated weekly series
        # W1S-W5S, R1S-R5S, M1S-M3S = additional weekly/flex series
        # None = accept all series IB returns (full chain).
        'preferred_trading_classes': None,
        # SI futures tradingClass — 'SI' = standard silver, 'SIL' = mini silver.
        # Must specify 'SI' or IB may return mini contracts (SIK6/SILK6) which
        # return 0 chains from reqSecDefOptParams.
        'futures_trading_class'    : 'SI',
    },
    'TSLA': {
        'symbol'               : 'TSLA',
        'secType'              : 'OPT',
        'exchange'             : 'SMART',
        'currency'             : 'USD',
        'model'                : 'bsm',
        'description'          : 'Tesla Equity Options',
        'div_yield'            : 0.0,
        'use_und_price_field'  : True,
        'jd_lambda'            : 0.15,
        'jd_mu_j'              : -0.05,
        'jd_sigma_j'           : 0.15,
    },
}

# ── Signal thresholds ─────────────────────────────────────────────────────────

SIGNAL_CONFIG: dict = {
    # Put/Call volume ratio
    'pc_ratio_bearish_threshold'     : 1.5,
    'pc_ratio_bullish_threshold'     : 0.67,

    # IV skew: avg(put IV) - avg(call IV), computed over ATM options only.
    # ATM is defined as |delta| in [iv_skew_delta_lo, iv_skew_delta_hi].
    # Using the full chain (including deep ITM/OTM) produces noise because
    # extreme-moneyness IV is numerically unstable and dominates simple averages.
    'iv_skew_bearish_threshold'      : 0.03,
    'iv_skew_bullish_threshold'      : -0.03,
    'iv_skew_delta_lo'               : 0.15,   # |delta| lower bound for ATM filter
    'iv_skew_delta_hi'               : 0.85,   # |delta| upper bound for ATM filter

    # Delta-weighted P/C ratio
    'dw_pc_ratio_bearish_threshold'  : 1.5,
    'dw_pc_ratio_bullish_threshold'  : 0.67,

    # Volume anomaly
    'volume_anomaly_multiplier'      : 2.0,
    'volume_rolling_days'            : 3,
}

# ── Fingerprint thresholds ────────────────────────────────────────────────────

FINGERPRINT_CONFIG: dict = {
    # Minimum confidence to surface a finding (0-1)
    'min_confidence'                : 0.5,

    # Alert threshold after multi-model corroboration bonus
    'alert_threshold'               : 0.6,

    # Corroboration bonus per additional model flagging same strike/expiry
    'corroboration_bonus'           : 0.1,

    # OI build: how many consecutive scans of monotonic growth to flag
    'oi_build_min_scans'            : 3,

    # Volume cluster: minimum number of strikes with same lot-size bucket
    'volume_cluster_min_strikes'    : 3,

    # Lot-size bucket width (round to nearest N contracts)
    'lot_size_bucket'               : 10,

    # Sweep detector: minimum strikes lit in one scan to flag a sweep
    'sweep_min_strikes'             : 3,

    # Sweep: require strike adjacency (True = stricter, False = same-expiry only)
    'sweep_require_adjacency'       : False,

    # *** NEW — sweep_min_print_size ***
    # Minimum per-strike volume delta (contracts) for a strike to be counted
    # as swept in snapshot mode.  Filters out single-lot retail prints that
    # would otherwise satisfy sweep_min_strikes.
    # In tick mode (USE_STREAMING_TICKS=True) this is the minimum individual
    # print size (contracts) that is counted toward a tick sweep.
    # Set to 0 to disable (count any positive delta / any tick size).
    # Override with env var SWEEP_MIN_PRINT_SIZE.
    'sweep_min_print_size'          : 10,

    # Print cluster (Option A only): time window in seconds to cluster prints
    'print_cluster_window_sec'      : 10,

    # Print cluster: minimum prints in window to flag
    'print_cluster_min_prints'      : 3,

    # Expiry concentration: fraction of total volume in one expiry to flag
    'expiry_concentration_threshold': 0.70,

    # How many historical scan records to keep per strike/expiry key
    'max_history_per_key'           : 50,

    # ── OI Put/Call signal ────────────────────────────────────────────────────
    # Minimum number of non-None OI observations (calls + puts combined) needed
    # across the moneyness band before the OI P/C ratio is computed.
    # Guards against alerting when IB has not yet delivered OI for the session.
    # Override with env var OI_PC_MIN_OBSERVATIONS.
    'oi_pc_min_observations'        : 10,

    # Absolute OI P/C ratio thresholds (put OI / call OI across moneyness band).
    # Ratio > bearish_threshold => bearish OI positioning signal.
    # Ratio < bullish_threshold => bullish OI positioning signal.
    # Override with env vars OI_PC_BEARISH_THRESHOLD / OI_PC_BULLISH_THRESHOLD.
    'oi_pc_bearish_threshold'       : 1.5,
    'oi_pc_bullish_threshold'       : 0.67,

    # Shift alert: flag when the OI P/C ratio changes by more than this fraction
    # from the previous scan (e.g. 0.20 = 20% relative change).
    # Set to 0.0 to disable shift detection.
    # Override with env var OI_PC_SHIFT_THRESHOLD.
    'oi_pc_shift_threshold'         : 0.20,

    # Number of nearest expiries to include in the per-expiry OI P/C breakdown.
    # Set to 0 to disable per-expiry breakdown entirely.
    # Override with env var OI_PC_NEAR_EXPIRY_COUNT.
    'oi_pc_near_expiry_count'       : 2,
}

# ── Alert suppression ─────────────────────────────────────────────────────────

# How many scan cycles to suppress a repeated alert for.
# 5 min * 30 = 30 min minimum between identical fingerprint re-fires.
# Signal alerts use the same counter but typically clear naturally
# when the composite drops (at which point suppression is reset).
ALERT_SUPPRESSION_SCANS : int = 30

# ── Qualify error cache ──────────────────────────────────────────────────────
# How long (seconds) to suppress a contract that returned Error 200 from
# qualifyContractsAsync before retrying it.  3600 = 1 hour.
# Set to 0 to disable caching (always retry — not recommended).
QUALIFY_ERROR_TTL_SEC   : int = 3600

# ── Output / Logging ──────────────────────────────────────────────────────────

LOG_DIR            : Path = Path(_env_str('LOG_DIR', './logs'))
FINGERPRINT_FILE   : Path = LOG_DIR / 'trade_fingerprint.json'
STATE_FILE         : Path = LOG_DIR / 'scanner_state.json'

# ── Cache ─────────────────────────────────────────────────────────────────────
# Contract metadata cache — separate from logs (this is reference data, not
# operational logs).  Future migration point: replace JSON files with SQLite.

CACHE_DIR                    : Path  = Path(_env_str('CACHE_DIR', './cache'))

# Maximum age of cache before a scheduled refresh is triggered
CACHE_TTL_HOURS              : float = 24.0

# Wall-clock refresh time in ET (HH:MM 24h).  Triggers once per day at this
# time regardless of TTL.  Set to '' to disable wall-clock trigger.
CACHE_REFRESH_TIME_ET        : str   = '06:00'

# How often CacheManager checks whether a refresh is needed (seconds)
CACHE_CHECK_INTERVAL_SEC     : int   = 300

# Timeout for a single reqContractDetails call during discovery.
# Large chains (CL 25K+, SI 28K+) can take 30-50s.  90s is safe.
CACHE_DISCOVERY_TIMEOUT_SEC  : float = 120.0

# Sleep between qualifyContractsAsync batches of 50 contracts.
# 50 msgs / 1.1s = ~45 msg/s — safely under IB's 50 msg/s hard limit.
# Reduce to 0.5 if you want faster cache builds and are confident your
# Gateway isn't rate-limited (risk: Error 100 / pacing violations).
QUALIFY_BATCH_SLEEP          : float = 1.1

# If underlying price moves more than this fraction from the price at last
# discovery, trigger a background per-instrument chain refresh
CACHE_MONEYNESS_DRIFT_THRESHOLD : float = 0.05
