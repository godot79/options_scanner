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

SCAN_INTERVAL_SEC       : int   = 20    # CL-only mode, fast cycle
INSTRUMENT_STAGGER_SEC  : float = 3.0
MAX_EXPIRY_DAYS         : int   = 30
MONEYNESS_BAND          : float = 0.20
RISK_FREE_RATE          : float = 0.05
MARKET_DATA_TIMEOUT_SEC : float = 8.0
IB_BATCH_SIZE           : int   = 50

# Number of front-month futures for OI streams, tick streams, and chain discovery.
# 2 = front 2 months only. 3 = front 3 months (catches deferred-month flow).
FUT_CHAIN_DEPTH : int = 3

# ── Display ───────────────────────────────────────────────────────────────────

# Top-N liquidity table shows only options expiring within this many calendar
# days from today.  In volatile markets short-dated options carry all the flow.
# Set to 0 to disable the filter (show all expiries).
# Examples: 1 = today only, 2 = today + tomorrow, 7 = this week.
# Fallback: if no options qualify (e.g. nothing expiring this week), the table
# falls back to the nearest available expiry so the display is never blank.
DISPLAY_NEAR_EXPIRY_DAYS : int = 2

# ── Streaming ticks (Option A — option contracts) ─────────────────────────────
USE_STREAMING_TICKS : bool = False

# ── Instruments ───────────────────────────────────────────────────────────────

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
        'preferred_trading_classes': None,
        'futures_trading_class'    : 'CL',
    },
}

# ── Signal thresholds ─────────────────────────────────────────────────────────

SIGNAL_CONFIG: dict = {
    'pc_ratio_bearish_threshold'     : 1.5,
    'pc_ratio_bullish_threshold'     : 0.67,
    'iv_skew_bearish_threshold'      : 0.03,
    'iv_skew_bullish_threshold'      : -0.03,
    'iv_skew_delta_lo'               : 0.15,
    'iv_skew_delta_hi'               : 0.85,
    'dw_pc_ratio_bearish_threshold'  : 1.5,
    'dw_pc_ratio_bullish_threshold'  : 0.67,
    'volume_anomaly_multiplier'      : 2.0,
    'volume_rolling_days'            : 3,
}

# ── Fingerprint thresholds ────────────────────────────────────────────────────

FINGERPRINT_CONFIG: dict = {
    'min_confidence'                : 0.5,
    'alert_threshold'               : 0.6,
    'corroboration_bonus'           : 0.1,
    'oi_build_min_scans'            : 3,
    'volume_cluster_min_strikes'    : 3,
    'lot_size_bucket'               : 10,
    'sweep_min_strikes'             : 3,
    'sweep_require_adjacency'       : False,
    'sweep_min_print_size'          : 5,
    'print_cluster_window_sec'      : 10,
    'print_cluster_min_prints'      : 3,
    'expiry_concentration_threshold': 0.70,
    'max_history_per_key'           : 50,
    'oi_pc_min_observations'        : 10,
    'oi_pc_bearish_threshold'       : 1.5,
    'oi_pc_bullish_threshold'       : 0.67,
    'oi_pc_shift_threshold'         : 0.20,
    'oi_pc_near_expiry_count'       : 2,
}

# ── Alert suppression ─────────────────────────────────────────────────────────

# At 20s interval: 6 scans = 2 min suppression window
ALERT_SUPPRESSION_SCANS : int = 6

# ── Qualify error cache ──────────────────────────────────────────────────────

QUALIFY_ERROR_TTL_SEC   : int = 3600

# ── Output / Logging ──────────────────────────────────────────────────────────

LOG_DIR            : Path = Path(_env_str('LOG_DIR', './logs'))
FINGERPRINT_FILE   : Path = LOG_DIR / 'trade_fingerprint.json'
STATE_FILE         : Path = LOG_DIR / 'scanner_state.json'

# ── Cache ─────────────────────────────────────────────────────────────────────

CACHE_DIR                       : Path  = Path(_env_str('CACHE_DIR', './cache'))
CACHE_TTL_HOURS                 : float = 24.0
CACHE_REFRESH_TIME_ET           : str   = '06:00'
CACHE_CHECK_INTERVAL_SEC        : int   = 300
CACHE_DISCOVERY_TIMEOUT_SEC     : float = 120.0
QUALIFY_BATCH_SLEEP             : float = 0.6
CACHE_MONEYNESS_DRIFT_THRESHOLD : float = 0.05
