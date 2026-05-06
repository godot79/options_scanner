"""
data/contract_cache.py
-----------------------
CacheManager — contract metadata caching layer.

Responsibilities
----------------
1. Serve contracts instantly from disk cache (scanner never blocks on IB).
2. Detect when a refresh is needed:
     a. TTL exceeded (CACHE_TTL_HOURS)
     b. Wall-clock anchor reached (CACHE_REFRESH_TIME_ET, once per day)
     c. Underlying price has drifted > CACHE_MONEYNESS_DRIFT_THRESHOLD
        since last discovery (per-instrument, surgical refresh)
3. Run refresh in the background — scanner keeps using current cache,
   atomically swaps to new cache when refresh completes.
4. Archive non-expired historical contracts (never deleted — future DB seed).
5. Manual trigger: write instrument name(s) to CACHE_DIR/refresh_request
   (one per line) or 'ALL' for all instruments.  Checked every
   CACHE_CHECK_INTERVAL_SEC seconds.

Cache files (in CACHE_DIR, separate from LOG_DIR)
--------------------------------------------------
  cache_{INSTRUMENT}.json         active contracts
  archive_{INSTRUMENT}.json       all historically seen contracts

Cache JSON schema (active)
--------------------------
{
  "version"        : "0.1.0",          # invalidated on code upgrade
  "instrument"     : "CL",
  "discovered_at"  : "2025-01-01T...", # ISO UTC
  "underlying_price_at_discovery": 75.3,
  "contracts": [
    {
      "conId"          : 123456,
      "localSymbol"    : "CLH5 P7000",
      "symbol"         : "CL",
      "secType"        : "FOP",
      "exchange"       : "NYMEX",
      "currency"       : "USD",
      "strike"         : 70.0,
      "right"          : "P",
      "expiry"         : "20250319",
      "multiplier"     : "1000",
      "tradingClass"   : "LO",
      "underConId"     : 654321,
      "first_seen"     : "2025-01-01T...",
      "last_seen"      : "2025-01-02T..."
    },
    ...
  ]
}

Future DB migration point
-------------------------
Each contract dict maps 1:1 to a SQLite row.  Table name: contracts_{instrument}.
Primary key: conId.  Index on (strike, right, expiry).
Replace load_cache() / save_cache() with SELECT / UPSERT and nothing else changes.
"""

import asyncio
import json
import os
import signal as _signal
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


import options_scanner.config as cfg
from options_scanner.data.utils import parse_expiry_date
# Minimal ChainSpec definition — duplicated here so contract_cache.py can be
# imported without ib_insync installed (e.g. in test environments).
# Must stay in sync with ChainSpec in ib_client.py.
try:
    from options_scanner.data.ib_client import ChainSpec
except ImportError:
    from dataclasses import dataclass as _dc, field as _dcf
    @_dc
    class ChainSpec:
        symbol        : str
        sec_type      : str
        exchange      : str
        currency      : str
        trading_class : str
        multiplier    : str
        expirations   : set
        strikes       : set
        und_con_id    : int
        und_symbol    : str = ''

_VERSION = '0.3.0'  # bumped: cache stores ChainSpec params not qualified contracts
_REFRESH_REQUEST_FILE = 'refresh_request'


# ── Contract serialisation helpers ───────────────────────────────────────────

def _chain_spec_to_dict(spec: 'ChainSpec') -> dict:
    """Serialise a ChainSpec to a JSON-compatible dict."""
    return {
        'symbol'       : spec.symbol,
        'sec_type'     : spec.sec_type,
        'exchange'     : spec.exchange,
        'currency'     : spec.currency,
        'trading_class': spec.trading_class,
        'multiplier'   : spec.multiplier,
        'expirations'  : sorted(spec.expirations),
        'strikes'      : sorted(spec.strikes),
        'und_con_id'   : spec.und_con_id,
        'und_symbol'   : spec.und_symbol,
    }


def _dict_to_chain_spec(d: dict) -> 'ChainSpec':
    """Deserialise a dict back to a ChainSpec."""
    return ChainSpec(
        symbol        = d['symbol'],
        sec_type      = d['sec_type'],
        exchange      = d['exchange'],
        currency      = d['currency'],
        trading_class = d['trading_class'],
        multiplier    = d['multiplier'],
        expirations   = set(d['expirations']),
        strikes       = set(d['strikes']),
        und_con_id    = d['und_con_id'],
        und_symbol    = d.get('und_symbol', ''),
    )


def _future_to_dict(contract) -> dict:
    """Serialise a futures Contract to a dict (for price feed storage)."""
    return {
        'conId'       : getattr(contract, 'conId',        0),
        'localSymbol' : getattr(contract, 'localSymbol',  ''),
        'symbol'      : getattr(contract, 'symbol',       ''),
        'secType'     : getattr(contract, 'secType',      'FUT'),
        'exchange'    : getattr(contract, 'exchange',     ''),
        'currency'    : getattr(contract, 'currency',     ''),
        'expiry'      : getattr(contract,
                                 'lastTradeDateOrContractMonth', ''),
        'tradingClass': getattr(contract, 'tradingClass', ''),
        'multiplier'  : getattr(contract, 'multiplier',  ''),
    }


def _dict_to_future(d: dict):
    """Reconstruct a futures Contract from a cached dict."""
    from ib_insync import Contract
    c = Contract()
    c.conId                        = d.get('conId', 0)
    c.localSymbol                  = d.get('localSymbol', '')
    c.symbol                       = d.get('symbol', '')
    c.secType                      = d.get('secType', 'FUT')
    c.exchange                     = d.get('exchange', '')
    c.currency                     = d.get('currency', '')
    c.lastTradeDateOrContractMonth = d.get('expiry', '')
    c.tradingClass                 = d.get('tradingClass', '')
    c.multiplier                   = d.get('multiplier', '')
    return c


# ── Cache file I/O ────────────────────────────────────────────────────────────

def _cache_path(instrument: str) -> Path:
    return cfg.CACHE_DIR / f'cache_{instrument}.json'


def _archive_path(instrument: str) -> Path:
    return cfg.CACHE_DIR / f'archive_{instrument}.json'


def _ensure_cache_dir() -> None:
    cfg.CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_cache(instrument: str) -> dict | None:
    """
    Load cache for one instrument.  Returns None if file missing or corrupt.
    Invalidates (returns None) if version tag doesn't match current code.
    """
    path = _cache_path(instrument)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return None
        if data.get('version') != _VERSION:
            print(f"[CACHE] {instrument}: version mismatch — will re-discover.")
            return None
        return data
    except Exception as e:
        print(f"[CACHE][WARN] Could not load cache for {instrument}: {e}")
        return None


def save_cache(instrument       : str,
               chain_specs      : list,          # list of ChainSpec objects
               underlying_price : float | None,
               futures          : list | None = None) -> None:  # underlying futures
    """
    Persist ChainSpec objects and underlying futures to cache file.
    chain_specs come from reqSecDefOptParams — no qualification needed at cache time.
    Futures are stored separately for price feed resolution at scan time.
    """
    _ensure_cache_dir()

    now_str      = datetime.now(timezone.utc).isoformat()
    specs_serial = [_chain_spec_to_dict(s) for s in chain_specs]
    futs_serial  = [_future_to_dict(f) for f in (futures or [])]

    data = {
        'version'                      : _VERSION,
        'instrument'                   : instrument,
        'discovered_at'                : now_str,
        'underlying_price_at_discovery': underlying_price,
        'futures'                      : futs_serial,
        'chain_specs'                  : specs_serial,
    }
    try:
        p = _cache_path(instrument)
        p.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[CACHE][WARN] Could not save cache for {instrument}: {e}")


def archive_contracts(instrument: str, contracts_dicts: list[dict]) -> None:
    """
    Merge contract dicts into the instrument's archive file.
    Archive is append-only: existing entries are updated (last_seen),
    new entries are added.  Nothing is ever deleted.

    Future DB migration: UPSERT into contracts_archive_{instrument} table.
    """
    _ensure_cache_dir()
    path = _archive_path(instrument)

    # Load existing archive
    existing: dict[int, dict] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
            for c in data.get('contracts', []):
                existing[c['conId']] = c
        except Exception as e:
            print(f"[CACHE][WARN] Could not load archive for {instrument}: {e}")

    now_str = datetime.now(timezone.utc).isoformat()
    for c in contracts_dicts:
        cid = c['conId']
        if cid in existing:
            existing[cid]['last_seen'] = now_str
            # Mark expired if expiry date is in the past
            exp_dt = parse_expiry_date(c.get('expiry', ''))
            if exp_dt and exp_dt < datetime.now(timezone.utc):
                existing[cid].setdefault('expired_at', now_str)
        else:
            entry = dict(c)
            entry['first_seen'] = c.get('first_seen', now_str)
            entry['last_seen']  = now_str
            existing[cid]       = entry

    try:
        path.write_text(json.dumps({
            'instrument'  : instrument,
            'last_updated': now_str,
            'contracts'   : list(existing.values()),
        }, indent=2))
    except Exception as e:
        print(f"[CACHE][WARN] Could not save archive for {instrument}: {e}")


def prune_expired_specs(chain_specs: list) -> tuple[list, list]:
    """
    Split ChainSpec list into (active, expired).

    A ChainSpec is only pruned when ALL of its expirations have passed.
    Contracts that are out-of-the-money today are kept — they may come back
    in-the-money later, and re-qualification would require another IB round-trip.
    Moneyness filtering happens at scan time in qualify_chain_for_scan(), not here.
    """
    now = datetime.now(timezone.utc)
    active, expired = [], []
    for spec in chain_specs:
        # Keep if any expiry is still in the future (not yet expired)
        has_active = any(
            (dt := parse_expiry_date(e)) is not None and dt >= now
            for e in spec.expirations
        )
        if has_active:
            active.append(spec)
        else:
            expired.append(spec)
    return active, expired


# ── Cache staleness checks ────────────────────────────────────────────────────

def is_stale_ttl(cache_data: dict) -> bool:
    """True if cache is older than CACHE_TTL_HOURS."""
    discovered_at = cache_data.get('discovered_at')
    if not discovered_at:
        return True
    try:
        ts  = datetime.fromisoformat(discovered_at)
        age = datetime.now(timezone.utc) - ts
        return age > timedelta(hours=cfg.CACHE_TTL_HOURS)
    except Exception:
        return True


def is_stale_wall_clock(cache_data: dict) -> bool:
    """
    True if CACHE_REFRESH_TIME_ET has passed today and the cache was last
    refreshed before that time today (i.e. needs one refresh per day at
    the configured wall-clock time).
    """
    refresh_time_str = cfg.CACHE_REFRESH_TIME_ET.strip()
    if not refresh_time_str:
        return False

    try:
        import zoneinfo
        et_tz    = zoneinfo.ZoneInfo('America/New_York')
        now_et   = datetime.now(et_tz)
        h, m     = map(int, refresh_time_str.split(':'))
        trigger  = now_et.replace(hour=h, minute=m, second=0, microsecond=0)

        if now_et < trigger:
            return False  # trigger time hasn't passed yet today

        discovered_at = cache_data.get('discovered_at')
        if not discovered_at:
            return True
        disc_ts  = datetime.fromisoformat(discovered_at).astimezone(et_tz)
        return disc_ts < trigger   # refreshed before today's trigger time
    except Exception:
        return False


def moneyness_drift(cache_data: dict,
                    current_price: float | None) -> bool:
    """
    True if underlying price has moved > CACHE_MONEYNESS_DRIFT_THRESHOLD
    since last discovery.
    """
    if current_price is None or current_price <= 0:
        return False
    cached_price = cache_data.get('underlying_price_at_discovery')
    if not cached_price or cached_price <= 0:
        return False
    drift = abs(current_price - cached_price) / cached_price
    return drift > cfg.CACHE_MONEYNESS_DRIFT_THRESHOLD


# ── Manual trigger file ───────────────────────────────────────────────────────

def read_refresh_requests() -> set[str]:
    """
    Check CACHE_DIR/refresh_request for manually requested instruments.
    File format: one instrument key per line, or 'ALL'.
    Deletes the file after reading so it doesn't re-trigger.
    Returns set of instrument keys to refresh, or {'ALL'}.
    """
    path = cfg.CACHE_DIR / _REFRESH_REQUEST_FILE
    if not path.exists():
        return set()
    try:
        lines = {l.strip().upper() for l in path.read_text().splitlines()
                 if l.strip()}
        path.unlink()
        return lines
    except Exception as e:
        print(f"[CACHE][WARN] Could not read refresh request: {e}")
        return set()


def write_refresh_request(instruments: list[str] | None = None) -> None:
    """
    Programmatically request a cache refresh.
    instruments=None means ALL.  Used by --refresh CLI flag.
    """
    _ensure_cache_dir()
    path    = cfg.CACHE_DIR / _REFRESH_REQUEST_FILE
    content = 'ALL' if not instruments else '\n'.join(instruments)
    path.write_text(content)


# ── CacheManager ─────────────────────────────────────────────────────────────

class CacheManager:
    """
    Central contract cache manager.

    The scanner calls serve() to get contracts — always returns immediately.
    A background asyncio task calls check_and_refresh() periodically.

    Thread safety: all state is mutated only within the asyncio event loop.
    No locks needed as long as this runs in a single-threaded async context.
    """

    def __init__(self, ib):   # ib: ib_insync.IB (lazy import — no top-level ib_insync dep)
        self._ib                  = ib
        # instrument -> list of ChainSpec objects (option chain params)
        self._chain_specs         : dict[str, list]         = {}
        # instrument -> list of underlying futures contracts (price feed)
        self._futures             : dict[str, list]         = {}
        # instrument -> last known underlying price (updated by scanner)
        self._last_prices         : dict[str, float]        = {}
        # instrument -> raw cache data dict (for staleness checks)
        self._cache_data          : dict[str, dict]         = {}
        # instruments currently being refreshed (prevent double-refresh)
        self._refreshing          : set[str]                = set()
        # legacy compat — kept so old call-sites don't AttributeError
        self._contracts           : dict[str, list]         = {}
        self._details             : dict[str, dict]         = {}

    # ── Startup ───────────────────────────────────────────────────────────────

    async def initialise(self, force_refresh: bool = False) -> None:
        """
        Load all instruments from cache.  If cache is missing or force_refresh
        is True, performs a blocking discovery (startup only).
        Staggered per instrument to respect IB pacing.
        """
        _ensure_cache_dir()
        instruments = list(cfg.INSTRUMENTS.keys())

        # Brief settle time after connection before firing contract requests
        # Prevents IB pacing violations when restarting after a failed session
        await asyncio.sleep(3.0)

        for i, key in enumerate(instruments):
            if i > 0:
                await asyncio.sleep(cfg.INSTRUMENT_STAGGER_SEC)

            cache_data = None if force_refresh else load_cache(key)

            if cache_data and not is_stale_ttl(cache_data):
                self._load_from_cache(key, cache_data)
                n_specs = len(self._chain_specs.get(key, []))
                print(f"[CACHE] {key}: loaded {n_specs} chain specs from cache "
                      f"(age: {self._cache_age_str(cache_data)})")
            else:
                reason = 'forced' if force_refresh else (
                    'no cache' if not cache_data else 'stale')
                print(f"[CACHE] {key}: discovering ({reason})...")
                await self._discover_and_save(key)
                # Pacing gap after discovery to let IB rate limiter recover
                # before the next instrument's discovery starts
                await asyncio.sleep(5.0)

    def _load_from_cache(self, instrument: str, cache_data: dict) -> None:
        """Deserialise cached ChainSpecs and futures into runtime objects."""
        specs_dicts  = cache_data.get('chain_specs', [])
        chain_specs  = [_dict_to_chain_spec(d) for d in specs_dicts]
        active, _exp = prune_expired_specs(chain_specs)

        self._chain_specs[instrument] = active

        # Restore underlying futures for price feed
        fut_dicts = cache_data.get('futures', [])
        self._futures[instrument] = [_dict_to_future(d) for d in fut_dicts]

        # Legacy compat: clear old contract/details dicts
        self._contracts[instrument] = []
        self._details[instrument]   = {}

        self._cache_data[instrument] = cache_data

    # ── Public API ────────────────────────────────────────────────────────────

    def serve(self, instrument: str) -> tuple[list, dict]:
        """
        Return (option_contracts, details_cache) for an instrument.
        Always returns immediately — never blocks.
        Returns empty structures if instrument not yet cached.
        """
        return (
            self._contracts.get(instrument, []),
            self._details.get(instrument, {}),
        )

    def serve_underlying(self, instrument: str) -> list:
        """
        Return underlying futures contracts for a FOP instrument.
        Always returns immediately from cache — never blocks.
        """
        return self._futures.get(instrument, [])

    def report_price(self, instrument: str, price: float) -> None:
        """Scanner calls this after each scan with the current underlying price."""
        if price and price > 0:
            self._last_prices[instrument] = price

    def is_ready(self, instrument: str) -> bool:
        """True if the instrument has at least some cached chain specs."""
        return bool(self._chain_specs.get(instrument))

    def serve_chain_specs(self, instrument: str) -> list:
        """
        Return list of ChainSpec objects for an instrument.
        scanner/instrument.py calls qualify_chain_for_scan() on these at scan time.
        """
        return self._chain_specs.get(instrument, [])

    # ── Background refresh loop ───────────────────────────────────────────────

    async def run_background(self) -> None:
        """
        Background task: checks every CACHE_CHECK_INTERVAL_SEC whether
        any instrument needs a refresh (TTL, wall-clock, drift, manual trigger).
        Refresh runs async — scanner is never paused.
        """
        while True:
            await asyncio.sleep(cfg.CACHE_CHECK_INTERVAL_SEC)
            await self._check_and_refresh()

    async def _check_and_refresh(self) -> None:
        """Evaluate all refresh triggers and act on any that fire."""
        # Manual trigger file (highest priority — checked first)
        requests = read_refresh_requests()
        if requests:
            targets = (list(cfg.INSTRUMENTS.keys())
                       if 'ALL' in requests
                       else [k for k in requests if k in cfg.INSTRUMENTS])
            for key in targets:
                await self._schedule_refresh(key, reason='manual trigger')
            return   # manual trigger pre-empts other checks this cycle

        # Scheduled checks per instrument
        for key in cfg.INSTRUMENTS:
            cache_data    = self._cache_data.get(key)
            current_price = self._last_prices.get(key)

            if cache_data is None:
                await self._schedule_refresh(key, reason='no cache data')
            elif is_stale_wall_clock(cache_data):
                await self._schedule_refresh(key, reason='wall-clock trigger')
            elif is_stale_ttl(cache_data):
                await self._schedule_refresh(key, reason='TTL expired')
            elif moneyness_drift(cache_data, current_price):
                await self._schedule_refresh(key,
                    reason=f'moneyness drift '
                           f'(price={current_price:.4g}, '
                           f'cached={cache_data.get("underlying_price_at_discovery","?"):.4g})')

    async def _schedule_refresh(self, instrument: str, reason: str) -> None:
        """
        Launch a background refresh for one instrument if not already running.
        Atomically swaps new contracts in when done — scanner sees no downtime.
        """
        if instrument in self._refreshing:
            return
        self._refreshing.add(instrument)
        print(f"[CACHE] {instrument}: background refresh queued ({reason})")
        asyncio.create_task(self._refresh_task(instrument))

    async def _refresh_task(self, instrument: str) -> None:
        """Runs the actual IB discovery, saves cache, swaps contracts."""
        try:
            await self._discover_and_save(instrument)
            n_specs = len(self._chain_specs.get(instrument, []))
            print(f"[CACHE] {instrument}: background refresh complete — "
                  f"{n_specs} chain specs active")
        except Exception as e:
            print(f"[CACHE][ERROR] {instrument}: refresh failed: {e}")
        finally:
            self._refreshing.discard(instrument)

    # ── Discovery ─────────────────────────────────────────────────────────────

    async def _discover_and_save(self, instrument: str) -> None:
        """
        Call IB to discover contracts, save to cache, update runtime state.
        On IB returning zero contracts: keep existing cache and warn.
        """
        from options_scanner.data.ib_client import (
            discover_futures, discover_fop_chain, discover_equity_options
        )

        inst_cfg      = cfg.INSTRUMENTS[instrument]
        details_cache : dict = {}
        contracts     : list = []
        underlying_price: float | None = self._last_prices.get(instrument)

        futs        : list = []
        chain_specs : list = []

        if inst_cfg['secType'] == 'FOP':
            futs = await discover_futures(self._ib, inst_cfg)
            if not futs:
                print(f"[CACHE][WARN] {instrument}: IB returned no futures — "
                      f"keeping existing cache.")
                return

            await asyncio.sleep(1.0)
            chain_specs = await discover_fop_chain(
                self._ib, inst_cfg, {}, futures=futs
            )
        else:
            _, chain_specs = await discover_equity_options(
                self._ib, inst_cfg, {}
            )

        if not chain_specs:
            print(f"[CACHE][WARN] {instrument}: IB returned no chain specs — "
                  f"keeping existing cache.")
            return

        total = sum(len(s.expirations) * len(s.strikes) * 2
                    for s in chain_specs)
        print(f"[CACHE] {instrument}: {len(chain_specs)} chain specs "
              f"({total} theoretical contracts) cached")

        # Save ChainSpecs + futures to disk
        save_cache(instrument, chain_specs, underlying_price, futures=futs)

        # Reload into runtime state
        new_cache = load_cache(instrument)
        if new_cache:
            self._load_from_cache(instrument, new_cache)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _cache_age_str(cache_data: dict) -> str:
        try:
            ts  = datetime.fromisoformat(cache_data['discovered_at'])
            age = datetime.now(timezone.utc) - ts
            h   = int(age.total_seconds() // 3600)
            m   = int((age.total_seconds() % 3600) // 60)
            return f"{h}h{m:02d}m"
        except Exception:
            return 'unknown'
