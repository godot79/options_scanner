"""
data/ib_client.py
-----------------
All IB Gateway / TWS interactions.
"""

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from ib_insync import IB, Contract, Future

import options_scanner.config as cfg
from options_scanner.data.utils import parse_expiry_date, safe_mid


# ── Failed-contract cache ────────────────────────────────────────────────────

_invalid_contract_cache: dict[tuple, float] = {}
_INVALID_CACHE_PATH: 'Path | None' = None


def _invalid_cache_path() -> 'Path':
    from pathlib import Path
    global _INVALID_CACHE_PATH
    if _INVALID_CACHE_PATH is None:
        _INVALID_CACHE_PATH = Path(getattr(cfg, 'CACHE_DIR', './cache')) / 'qualify_invalid.json'
    return _INVALID_CACHE_PATH


def _load_invalid_cache() -> None:
    import json
    path = _invalid_cache_path()
    if not path.exists():
        return
    try:
        with open(path) as f:
            raw = json.load(f)
        now = time.time()
        loaded = 0
        for key_str, exp in raw.items():
            if exp > now:
                parts = key_str.split('|')
                if len(parts) == 4:
                    sym, expiry, strike_s, right = parts
                    _invalid_contract_cache[(sym, expiry, float(strike_s), right)] = exp
                    loaded += 1
        if loaded:
            print(f"[IB] Loaded {loaded} cached-invalid contracts "
                  f"from {path.name} (suppressed for up to "
                  f"{getattr(cfg, 'QUALIFY_ERROR_TTL_SEC', 3600)//60}min)")
    except Exception as e:
        print(f"[IB][WARN] Could not load invalid-contract cache: {e}")


def _save_invalid_cache() -> None:
    import json
    path = _invalid_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        raw = {
            f'{sym}|{expiry}|{strike}|{right}': exp
            for (sym, expiry, strike, right), exp
            in _invalid_contract_cache.items()
            if exp > now
        }
        with open(path, 'w') as f:
            json.dump(raw, f)
    except Exception as e:
        print(f"[IB][WARN] Could not save invalid-contract cache: {e}")


def _is_invalid_cached(sym: str, expiry: str,
                        strike: float, right: str) -> bool:
    key = (sym, expiry, strike, right)
    exp = _invalid_contract_cache.get(key)
    if exp is None:
        return False
    if time.time() > exp:
        del _invalid_contract_cache[key]
        return False
    return True


def _mark_invalid(sym: str, expiry: str, strike: float, right: str) -> None:
    ttl = getattr(cfg, 'QUALIFY_ERROR_TTL_SEC', 3600)
    _invalid_contract_cache[(sym, expiry, strike, right)] = time.time() + ttl


# ── Qualified contract cache (details_cache persistence) ────────────────────
#
# The details_cache on InstrumentScanner (conId -> SimpleNamespace with .contract
# and .underConId) is rebuilt from scratch each process start via 4000+ IB calls
# taking ~110s.  These functions persist it to disk so subsequent starts load in
# milliseconds.  Stored separately from the ChainSpec cache so version bumps
# on one don't invalidate the other.

_QUALIFIED_VERSION = '1.0'


def _qual_cache_path(instrument: str) -> 'Path':
    from pathlib import Path
    return Path(getattr(cfg, 'CACHE_DIR', './cache')) / f'qualified_{instrument}.json'


def save_qualified_cache(instrument: str, details_cache: dict) -> None:
    """
    Persist details_cache to disk.
    Only stores non-expired contracts; expired ones are silently dropped.
    """
    from pathlib import Path
    import json as _json
    if not details_cache:
        return
    now = datetime.now(timezone.utc)
    rows = []
    for conid, cd in details_cache.items():
        c = getattr(cd, 'contract', None)
        if c is None:
            continue
        expiry_str = getattr(c, 'lastTradeDateOrContractMonth', '') or ''
        exp_dt = parse_expiry_date(expiry_str)
        if exp_dt is not None and exp_dt.date() < now.date():
            continue  # drop expired
        rows.append({
            'conId'          : getattr(c, 'conId',                          conid),
            'symbol'         : getattr(c, 'symbol',                         ''),
            'localSymbol'    : getattr(c, 'localSymbol',                    ''),
            'secType'        : getattr(c, 'secType',                        ''),
            'exchange'       : getattr(c, 'exchange',                       ''),
            'currency'       : getattr(c, 'currency',                       ''),
            'strike'         : getattr(c, 'strike',                         0.0),
            'right'          : getattr(c, 'right',                          ''),
            'expiry'         : expiry_str,
            'multiplier'     : getattr(c, 'multiplier',                     ''),
            'tradingClass'   : getattr(c, 'tradingClass',                   ''),
            'underConId'     : getattr(cd, 'underConId',                    0),
        })
    data = {
        'version'    : _QUALIFIED_VERSION,
        'instrument' : instrument,
        'saved_at'   : now.isoformat(),
        'contracts'  : rows,
    }
    try:
        path = _qual_cache_path(instrument)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(data, indent=2))
        print(f"[QCACHE] {instrument}: saved {len(rows)} qualified contracts to disk.")
    except Exception as e:
        print(f"[QCACHE][WARN] {instrument}: could not save qualified cache: {e}")


def load_qualified_cache(instrument: str) -> dict:
    """
    Load a previously saved details_cache from disk.
    Returns {} if missing, wrong version, or all entries expired.
    """
    import json as _json
    from types import SimpleNamespace as _SN
    path = _qual_cache_path(instrument)
    if not path.exists():
        return {}
    try:
        data = _json.loads(path.read_text())
    except Exception as e:
        print(f"[QCACHE][WARN] {instrument}: could not read qualified cache: {e}")
        return {}
    if data.get('version') != _QUALIFIED_VERSION:
        print(f"[QCACHE] {instrument}: version mismatch — will re-qualify.")
        return {}
    now    = datetime.now(timezone.utc)
    result : dict = {}
    n_expired = 0
    for row in data.get('contracts', []):
        expiry_str = row.get('expiry', '')
        exp_dt     = parse_expiry_date(expiry_str)
        if exp_dt is not None and exp_dt.date() < now.date():
            n_expired += 1
            continue
        from ib_insync import Contract
        c                                    = Contract()
        c.conId                              = row['conId']
        c.symbol                             = row.get('symbol', '')
        c.localSymbol                        = row.get('localSymbol', '')
        c.secType                            = row.get('secType', '')
        c.exchange                           = row.get('exchange', '')
        c.currency                           = row.get('currency', '')
        c.strike                             = float(row.get('strike', 0))
        c.right                              = row.get('right', '')
        c.lastTradeDateOrContractMonth       = expiry_str
        c.multiplier                         = row.get('multiplier', '')
        c.tradingClass                       = row.get('tradingClass', '')
        cd                                   = _SN()
        cd.contract                          = c
        cd.underConId                        = row.get('underConId', 0)
        result[c.conId]                      = cd
    print(f"[QCACHE] {instrument}: loaded {len(result)} qualified contracts "
          f"({n_expired} expired dropped).")
    return result


# ── Connection ────────────────────────────────────────────────────────────────

def connect(ib: IB) -> bool:
    try:
        ib.connect(cfg.IB_HOST, cfg.IB_PORT, clientId=cfg.IB_CLIENT_ID)
        print(f"[IB] Connected to {cfg.IB_HOST}:{cfg.IB_PORT} "
              f"(clientId={cfg.IB_CLIENT_ID})")
        _load_invalid_cache()
        return True
    except Exception as e:
        print(f"[IB][FATAL] Connection failed: {e}")
        return False


def disconnect(ib: IB) -> None:
    try:
        ib.disconnect()
        print("[IB] Disconnected.")
    except Exception:
        pass


# ── Contract details ──────────────────────────────────────────────────────────

async def req_contract_details(ib: IB, contract: Contract,
                                label: str = '',
                                timeout: float = 60.0) -> list:
    try:
        cds = await asyncio.wait_for(
            ib.reqContractDetailsAsync(contract),
            timeout=timeout,
        )
        return cds or []
    except asyncio.TimeoutError:
        print(f"[IB][WARN] reqContractDetails timed out after {timeout:.0f}s [{label}]")
        return []
    except Exception as e:
        print(f"[IB][WARN] reqContractDetails failed [{label}]: {e}")
        return []


# ── Futures discovery ─────────────────────────────────────────────────────────

async def discover_futures(ib: IB, instrument_cfg: dict) -> list:
    sym                   = instrument_cfg['symbol']
    exch                  = instrument_cfg['exchange']
    curr                  = instrument_cfg['currency']
    futures_trading_class = instrument_cfg.get('futures_trading_class', None)

    template = Future(symbol=sym, exchange=exch, currency=curr)
    cds = await req_contract_details(
        ib, template, label=f"{sym} futures", timeout=60.0
    )
    if not cds:
        print(f"[IB][WARN] No futures found for {sym}.")
        return []

    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=62)

    if futures_trading_class:
        preferred_tc = futures_trading_class
    else:
        from collections import Counter
        tc_counts    = Counter(cd.contract.tradingClass for cd in cds)
        preferred_tc = tc_counts.most_common(1)[0][0]

    print(f"[IB] {sym} futures: using tradingClass='{preferred_tc}' "
          f"({sum(1 for cd in cds if cd.contract.tradingClass == preferred_tc)} contracts)")

    qualifying = []
    for cd in cds:
        c = cd.contract
        if c.tradingClass != preferred_tc:
            continue
        exp_dt = parse_expiry_date(c.lastTradeDateOrContractMonth)
        if exp_dt is not None and exp_dt.date() >= now.date() and exp_dt <= cutoff:
            qualifying.append(c)

    qualifying = sorted(qualifying, key=lambda c: c.lastTradeDateOrContractMonth)

    if not qualifying:
        all_tc = sorted(
            (cd.contract for cd in cds if cd.contract.tradingClass == preferred_tc),
            key=lambda c: c.lastTradeDateOrContractMonth,
        )
        qualifying = all_tc[:2]
        print(f"[IB][WARN] No {sym} futures within 62d for class "
              f"'{preferred_tc}'; using nearest {len(qualifying)}.")

    depth = getattr(cfg, 'FUT_CHAIN_DEPTH', 2)
    if len(qualifying) > depth:
        qualifying = qualifying[:depth]

    print(f"[IB] {sym}: qualifying {len(qualifying)} futures contracts...")
    try:
        qualified  = await ib.qualifyContractsAsync(*qualifying)
        qualifying = [c for c in qualified if getattr(c, 'conId', 0) > 0]
        print(f"[IB] {sym}: futures qualified: "
              f"{[c.localSymbol for c in qualifying]}")
    except Exception as e:
        print(f"[IB][WARN] {sym}: futures qualify failed ({e}), "
              f"using unqualified conIds")

    print(f"[IB] {sym}: {len(qualifying)} futures in window: "
          f"{[c.localSymbol for c in qualifying]}")
    return qualifying


# ── ChainSpec ─────────────────────────────────────────────────────────────────

from dataclasses import dataclass as _dc

@_dc
class ChainSpec:
    symbol         : str
    sec_type       : str
    exchange       : str
    currency       : str
    trading_class  : str
    multiplier     : str
    expirations    : set
    strikes        : set
    und_con_id     : int
    und_symbol     : str = ''


# ── reqSecDefOptParams ────────────────────────────────────────────────────────

_QUALIFY_BATCH_SIZE = 25


async def _req_sec_def_opt_params(ib: IB, sym: str, fop_exchange: str,
                                   und_sec_type: str,
                                   und_con_id: int) -> list:
    async def _call(exch: str) -> list:
        try:
            chains = await asyncio.wait_for(
                ib.reqSecDefOptParamsAsync(sym, exch, und_sec_type, und_con_id),
                timeout=30.0,
            )
            return chains or []
        except asyncio.TimeoutError:
            print(f"[IB][WARN] reqSecDefOptParams timed out for {sym} "
                  f"(conId={und_con_id}, exchange='{exch}')")
            return []
        except Exception as e:
            print(f"[IB][WARN] reqSecDefOptParams failed for {sym}: {e}")
            return []

    chains = await _call(fop_exchange)
    if not chains and fop_exchange:
        print(f"[IB] {sym}: no chains with exchange='{fop_exchange}', "
              f"retrying with ''...")
        chains = await _call('')
    return chains


async def _qualify_contracts_batched(ib: IB, contracts: list,
                                      label: str = '') -> list:
    qualified     = []
    n_unqualified = 0
    n_timeouts    = 0
    total         = len(contracts)
    if not total:
        return []

    n_batches = (total + _QUALIFY_BATCH_SIZE - 1) // _QUALIFY_BATCH_SIZE
    print(f"[IB] {label}: qualifying {total} contracts "
          f"({n_batches} batches of {_QUALIFY_BATCH_SIZE}, "
          f"~{(n_batches-1)*cfg.QUALIFY_BATCH_SLEEP:.0f}s sleep total)...")

    for i in range(0, total, _QUALIFY_BATCH_SIZE):
        batch     = contracts[i : i + _QUALIFY_BATCH_SIZE]
        batch_num = i // _QUALIFY_BATCH_SIZE + 1
        try:
            result = await asyncio.wait_for(
                ib.qualifyContractsAsync(*batch),
                timeout=60.0,
            )
            for c in result:
                if getattr(c, 'conId', 0) > 0:
                    qualified.append(c)
                else:
                    n_unqualified += 1
        except asyncio.TimeoutError:
            n_timeouts += 1
            print(f"[IB][WARN] qualifyContractsAsync timed out "
                  f"[{label} batch {batch_num}/{n_batches}]")
        except Exception as e:
            print(f"[IB][WARN] qualifyContractsAsync failed "
                  f"[{label} batch {batch_num}/{n_batches}]: {e}")

        done = min(i + _QUALIFY_BATCH_SIZE, total)
        if done % 500 == 0 or done == total:
            print(f"[IB] {label}: {done}/{total} sent to qualify, "
                  f"{len(qualified)} valid so far...")

        if i + _QUALIFY_BATCH_SIZE < total:
            await asyncio.sleep(cfg.QUALIFY_BATCH_SLEEP)

    print(f"[IB] {label}: qualify complete — "
          f"{len(qualified)} valid, {n_unqualified} invalid combos, "
          f"{n_timeouts} batch timeouts")
    return qualified


async def discover_fop_chain(ib: IB, instrument_cfg: dict,
                              details_cache: dict,
                              futures: list | None = None) -> list:
    sym       = instrument_cfg['symbol']
    exch      = instrument_cfg['exchange']
    curr      = instrument_cfg['currency']
    preferred = instrument_cfg.get('preferred_trading_classes', None)
    now       = datetime.now(timezone.utc)

    if futures:
        fut_list = sorted(futures,
                           key=lambda c: c.lastTradeDateOrContractMonth)
        print(f"[IB] {sym}: using {len(fut_list)} pre-fetched futures: "
              f"{[f.localSymbol for f in fut_list]}")
    else:
        print(f"[IB] {sym}: fetching futures list...")
        fut_cds = await req_contract_details(
            ib, Future(symbol=sym, exchange=exch, currency=curr),
            label=f"{sym} futures", timeout=60.0,
        )
        if not fut_cds:
            print(f"[IB][WARN] {sym}: no futures found.")
            return []
        fut_list = sorted(
            (cd.contract for cd in fut_cds),
            key=lambda c: c.lastTradeDateOrContractMonth
        )

    all_chains      : list = []
    seen_chain_keys : set  = set()

    for fut in fut_list:
        print(f"[IB] {sym}: reqSecDefOptParams for {fut.localSymbol} "
              f"(conId={fut.conId})...")
        chains_for_fut = await _req_sec_def_opt_params(
            ib, sym, exch, 'FUT', fut.conId
        )
        new_count = 0
        for chain in chains_for_fut:
            key = (chain.tradingClass, frozenset(chain.expirations))
            if key not in seen_chain_keys:
                seen_chain_keys.add(key)
                all_chains.append(chain)
                new_count += 1
        print(f"[IB] {sym}: {new_count} new chains from {fut.localSymbol} "
              f"(total unique: {len(all_chains)})")
        await asyncio.sleep(0.3)

    if not all_chains:
        print(f"[IB] {sym}: reqSecDefOptParams found nothing via dated futures conIds.")
        return []

    if preferred:
        primary = [c for c in all_chains if c.tradingClass in preferred]
        if not primary:
            print(f"[IB][WARN] {sym}: none of preferred classes {preferred} found; "
                  f"using all {len(all_chains)} chains.")
            primary = all_chains
    else:
        primary = all_chains

    chain_specs : list[ChainSpec] = []
    for fut in fut_list:
        for chain in primary:
            valid_expiries = set()
            for expiry in chain.expirations:
                exp_dt = parse_expiry_date(expiry)
                if exp_dt is not None and exp_dt.date() < now.date():
                    continue
                valid_expiries.add(expiry)
            if not valid_expiries:
                continue
            chain_specs.append(ChainSpec(
                symbol        = sym,
                sec_type      = 'FOP',
                exchange      = exch,
                currency      = curr,
                trading_class = chain.tradingClass,
                multiplier    = chain.multiplier,
                expirations   = valid_expiries,
                strikes       = set(chain.strikes),
                und_con_id    = fut.conId,
                und_symbol    = sym,
            ))

    # Deduplicate chain specs by (tradingClass, expirations)
    seen : set = set()
    deduped : list[ChainSpec] = []
    for cs in chain_specs:
        key = (cs.trading_class, frozenset(cs.expirations))
        if key not in seen:
            seen.add(key)
            deduped.append(cs)

    total_combos = sum(len(s.expirations) * len(s.strikes) * 2 for s in deduped)
    print(f"[IB] {sym} FOP: {len(deduped)} chain specs cached "
          f"({total_combos} theoretical contracts, qualify at scan time)")
    return deduped


async def discover_equity_options(ib: IB, instrument_cfg: dict,
                                   details_cache: dict) -> tuple[list, list]:
    sym      = instrument_cfg['symbol']
    exch     = instrument_cfg.get('exchange', 'SMART')
    curr     = instrument_cfg['currency']
    now      = datetime.now(timezone.utc)

    stk         = Contract()
    stk.symbol  = sym
    stk.secType = 'STK'
    stk.exchange= exch
    stk.currency= curr

    cds = await req_contract_details(ib, stk, label=f"{sym} STK")
    if not cds:
        print(f"[IB][WARN] {sym}: no STK contract found.")
        return [], []

    und_contract = cds[0].contract
    und_con_id   = und_contract.conId

    chains = await _req_sec_def_opt_params(ib, sym, '', 'STK', und_con_id)
    if not chains:
        print(f"[IB][WARN] {sym}: no OPT chains from reqSecDefOptParams.")
        return [und_contract], []

    chain_specs : list[ChainSpec] = []
    for chain in chains:
        valid_expiries = set()
        for expiry in chain.expirations:
            exp_dt = parse_expiry_date(expiry)
            if exp_dt is not None and exp_dt.date() < now.date():
                continue
            valid_expiries.add(expiry)
        if not valid_expiries:
            continue
        chain_specs.append(ChainSpec(
            symbol        = sym,
            sec_type      = 'OPT',
            exchange      = 'SMART',
            currency      = curr,
            trading_class = chain.tradingClass,
            multiplier    = chain.multiplier,
            expirations   = valid_expiries,
            strikes       = set(chain.strikes),
            und_con_id    = und_con_id,
            und_symbol    = sym,
        ))

    total_combos = sum(
        len(s.expirations) * len(s.strikes) * 2 for s in chain_specs
    )
    print(f"[IB] {sym} OPT: {len(chain_specs)} chain specs cached "
          f"({total_combos} theoretical contracts across all non-expired expiries, "
          f"qualify at scan time on ±20% subset)")
    return [und_contract], chain_specs


# ── Scan-time contract qualification ─────────────────────────────────────────

async def qualify_chain_for_scan(ib: IB,
                                  chain_specs: list,
                                  underlying_price: float | None,
                                  moneyness_band: float,
                                  details_cache: dict) -> list:
    sym = chain_specs[0].symbol if chain_specs else '?'

    # Build lookup of already-qualified contracts from details_cache
    existing_key_lookup: dict[tuple, int] = {}
    for conid, cd in details_cache.items():
        if not hasattr(cd, 'contract'):
            continue
        c   = cd.contract
        key = (c.symbol, c.lastTradeDateOrContractMonth,
               float(c.strike), c.right)
        existing_key_lookup[key] = conid

    already_qualified : list = []
    raw               : list = []
    und_con_ids       : dict = {}   # id(contract) -> underConId

    for spec in chain_specs:
        for expiry in spec.expirations:
            exp_dt = parse_expiry_date(expiry)
            if exp_dt is not None and exp_dt.date() < datetime.now(timezone.utc).date():
                continue
            for strike in spec.strikes:
                if underlying_price and underlying_price > 0:
                    lo = underlying_price * (1 - moneyness_band)
                    hi = underlying_price * (1 + moneyness_band)
                    if not (lo <= strike <= hi):
                        continue
                for right in ('C', 'P'):
                    key = (spec.symbol, expiry, float(strike), right)

                    # Cache hit — skip re-qualification
                    if key in existing_key_lookup:
                        conid = existing_key_lookup[key]
                        cd    = details_cache[conid]
                        already_qualified.append(cd.contract)
                        continue

                    # Skip known-invalid combos
                    if _is_invalid_cached(spec.symbol, expiry, strike, right):
                        continue

                    c                              = Contract()
                    c.symbol                       = spec.symbol
                    c.secType                      = spec.sec_type
                    c.exchange                     = spec.exchange
                    c.currency                     = spec.currency
                    c.tradingClass                 = spec.trading_class
                    c.multiplier                   = spec.multiplier
                    c.lastTradeDateOrContractMonth = expiry
                    c.strike                       = strike
                    c.right                        = right
                    raw.append(c)
                    und_con_ids[id(c)]             = spec.und_con_id

    if not raw:
        print(f"[IB] {sym}: all {len(already_qualified)} in-band contracts "
              f"already cached — skipping qualification.")
        return already_qualified

    print(f"[IB] {sym}: qualify_chain_for_scan — "
          f"{len(already_qualified)} cache hits, {len(raw)} new to qualify")

    qualified     = await _qualify_contracts_batched(ib, raw, label=sym)
    n_newly_cached = 0

    # Mark failed combos as invalid
    qualified_keys = {
        (c.symbol, c.lastTradeDateOrContractMonth, float(c.strike), c.right)
        for c in qualified
    }
    for c in raw:
        key = (c.symbol, c.lastTradeDateOrContractMonth, float(c.strike), c.right)
        if key not in qualified_keys:
            _mark_invalid(c.symbol, c.lastTradeDateOrContractMonth,
                          float(c.strike), c.right)
            n_newly_cached += 1

    if n_newly_cached:
        ttl = getattr(cfg, 'QUALIFY_ERROR_TTL_SEC', 3600)
        print(f"[IB] qualify_chain_for_scan: {n_newly_cached} new invalid contracts "
              f"cached (suppressed for {ttl//60}min)")
        _save_invalid_cache()

    from types import SimpleNamespace as _SN
    for ct in qualified:
        cd            = _SN()
        cd.contract   = ct
        cd.underConId = und_con_ids.get(id(ct), 0)
        details_cache[ct.conId] = cd

    return already_qualified + qualified


# ── Snapshot market data ──────────────────────────────────────────────────────

async def fetch_snapshot(ib: IB, contracts: list,
                          timeout: float = None) -> dict:
    if not contracts:
        return {}

    per_batch_timeout = timeout or cfg.MARKET_DATA_TIMEOUT_SEC
    tickers: dict     = {}

    for i in range(0, len(contracts), cfg.IB_BATCH_SIZE):
        batch = contracts[i : i + cfg.IB_BATCH_SIZE]

        batch_tickers: dict = {}
        for c in batch:
            try:
                t = ib.reqMktData(c, genericTickList='',
                                   snapshot=True, regulatorySnapshot=False)
                batch_tickers[c.conId] = t
            except Exception as e:
                print(f"[IB][WARN] reqMktData failed conId={c.conId}: {e}")

        deadline = time.time() + per_batch_timeout
        while time.time() < deadline:
            filled = sum(
                1 for t in batch_tickers.values()
                if t.bid is not None or t.ask is not None or t.last is not None
            )
            if filled == len(batch_tickers):
                break
            await asyncio.sleep(0.2)

        tickers.update(batch_tickers)
        await asyncio.sleep(0.3)

    return tickers


# ── Underlying price ──────────────────────────────────────────────────────────

async def resolve_underlying_price(ib: IB,
                                    instrument_cfg: dict,
                                    opt_tickers: dict) -> Optional[float]:
    if instrument_cfg.get('use_und_price_field', True):
        for t in opt_tickers.values():
            und = getattr(t, 'undPrice', None)
            if und and float(und) > 0:
                return float(und)

    sym  = instrument_cfg['symbol']
    exch = instrument_cfg.get('exchange', 'SMART')
    curr = instrument_cfg['currency']

    stk          = Contract()
    stk.symbol   = sym
    stk.secType  = 'STK'
    stk.exchange = exch
    stk.currency = curr

    cds = await req_contract_details(ib, stk, label=f"{sym} STK fallback")
    if not cds:
        return None

    tickers = await fetch_snapshot(ib, [cds[0].contract], timeout=6.0)
    t       = next(iter(tickers.values()), None)
    if t is None:
        return None

    price = (safe_mid(getattr(t, 'bid', None), getattr(t, 'ask', None))
             or getattr(t, 'last',  None)
             or getattr(t, 'close', None))
    return float(price) if price else None


# ── EquityStream ──────────────────────────────────────────────────────────────

class EquityStream:
    """Persistent streaming quote for an equity underlying (STK)."""

    def __init__(self, ib: IB, instrument_cfg: dict):
        self._ib      = ib
        self._cfg     = instrument_cfg
        self._ticker  = None
        self._contract= None

    async def start(self) -> None:
        sym  = self._cfg['symbol']
        exch = self._cfg.get('exchange', 'SMART')
        curr = self._cfg['currency']

        stk          = Contract()
        stk.symbol   = sym
        stk.secType  = 'STK'
        stk.exchange = exch
        stk.currency = curr

        cds = await req_contract_details(self._ib, stk,
                                          label=f"{sym} equity stream")
        if not cds:
            print(f"[IB][WARN] Could not find STK contract for {sym}")
            return

        self._contract = cds[0].contract
        try:
            self._ticker = self._ib.reqMktData(
                self._contract,
                genericTickList='100,101,104,106',
                snapshot=False,
                regulatorySnapshot=False,
            )
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if self.price() is not None:
                    break
                await self._ib.sleep(0.3)
            p = self.price()
            print(f"[IB] Equity stream started for {sym}: "
                  f"{'price=' + str(round(p,2)) if p else 'no price yet'}")
        except Exception as e:
            print(f"[IB][WARN] Equity stream failed for {sym}: {e}")

    def price(self) -> Optional[float]:
        if self._ticker is None:
            return None
        t = self._ticker
        p = safe_mid(getattr(t, 'bid', None), getattr(t, 'ask', None))
        if p and p > 0:
            return p
        for field in ('last', 'close'):
            v = getattr(t, field, None)
            if v and float(v) > 0:
                return float(v)
        return None

    def stop(self) -> None:
        if self._ticker is not None and self._contract is not None:
            try:
                self._ib.cancelMktData(self._contract)
            except Exception:
                pass
            self._ticker   = None
            self._contract = None


# ── TickStream (option contracts, Option A) ───────────────────────────────────

class TickStream:
    """Tick-by-tick stream for one option contract (USE_STREAMING_TICKS=True)."""

    def __init__(self, ib: IB, contract: Contract):
        self._ib       = ib
        self._contract = contract
        self._ticker   = None
        self._prints   : list[dict] = []

    def start(self) -> None:
        try:
            self._ticker = self._ib.reqTickByTickData(
                self._contract, 'Last', numberOfTicks=0, ignoreSize=False
            )
            self._ticker.updateEvent += self._on_tick
        except Exception as e:
            print(f"[IB][WARN] Tick stream failed for "
                  f"{self._contract.conId}: {e}")

    def stop(self) -> None:
        if self._ticker is not None:
            try:
                self._ib.cancelTickByTickData(self._ticker)
            except Exception:
                pass

    def drain(self) -> list[dict]:
        buf          = self._prints[:]
        self._prints = []
        return buf

    def _on_tick(self, ticker, *args) -> None:
        try:
            self._prints.append({
                'ts'    : datetime.now(timezone.utc).isoformat(),
                'price' : float(ticker.last) if ticker.last else None,
                'size'  : float(ticker.lastSize) if ticker.lastSize else None,
                'conId' : self._contract.conId,
            })
        except Exception:
            pass


# ── OIStream ──────────────────────────────────────────────────────────────────

class OIStream:
    """
    Persistent non-snapshot stream for a FOP underlying future.

    genericTickList='588,233':
      '588' → Futures Open Interest (tick 86), read via futuresOpenInterest.
      '233' → RTVolume, ensures bid/ask/last are delivered promptly.
               Without '233', default bid/ask ticks may arrive after the
               5-second wait loop exits, leaving FuturesTickStream with
               no quote to classify against → high unclassified rate.

    Also delivers price ticks (bid/ask/last/close) via price().
    """

    def __init__(self, ib: IB, contract: Contract):
        self._ib       = ib
        self._contract = contract
        self._ticker   = None

    async def start(self) -> None:
        sym = getattr(self._contract, 'symbol', str(self._contract.conId))
        try:
            self._ticker = self._ib.reqMktData(
                self._contract,
                genericTickList='588,233',
                snapshot=False,
                regulatorySnapshot=False,
            )
            # Wait until both OI and a valid price (bid/ask) have arrived.
            # Previously only waited for oi() — bid/ask could still be nan
            # when the loop exited, so FuturesTickStream snapshotted nan
            # bid/ask for every tick and classified nothing.
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if self.oi() is not None and self.price() is not None:
                    break
                await self._ib.sleep(0.3)
            oi_val = self.oi()
            print(f"[IB] OI stream started for {sym} "
                  f"(conId={self._contract.conId}): "
                  f"{'OI=' + str(int(oi_val)) if oi_val is not None else 'no OI yet'}")
        except Exception as e:
            print(f"[IB][WARN] OI stream failed for {sym} "
                  f"(conId={self._contract.conId}): {e}")

    def price(self) -> Optional[float]:
        """Return mid/last/close from the persistent futures stream."""
        if self._ticker is None:
            return None
        t = self._ticker
        p = safe_mid(getattr(t, 'bid', None), getattr(t, 'ask', None))
        if p is not None and p > 0:
            return p
        for field in ('last', 'close'):
            v = getattr(t, field, None)
            if v is not None and float(v) > 0:
                return float(v)
        return None

    def oi(self) -> Optional[float]:
        if self._ticker is None:
            return None
        val = getattr(self._ticker, 'futuresOpenInterest', None)
        if val is not None and float(val) >= 0:
            return float(val)
        return None

    def stop(self) -> None:
        if self._ticker is not None and self._contract is not None:
            try:
                self._ib.cancelMktData(self._contract)
            except Exception:
                pass
            self._ticker = None


# ── FuturesTickStream ─────────────────────────────────────────────────────────

@dataclass
class FuturesMomentum:
    """
    Momentum snapshot for one futures contract over one scan window.

    buy_vol / sell_vol:
      Aggressor side inferred by comparing trade price to prevailing bid/ask
      from the co-located OIStream ticker.
        last >= ask  → buy-initiated (lifting the offer)
        last <= bid  → sell-initiated (hitting the bid)
        bid < last < ask → unclassified (price improvement or inside spread)

      IB does not provide an aggressor-side flag on tick data.
      TickAttribLast only has pastLimit (trade through limit) and unreported
      — neither gives direction. Bid/ask comparison is the industry standard
      for futures aggressor inference.

    buy_pct: buy_vol / (buy_vol + sell_vol) — fraction of CLASSIFIED volume.
             This always sums to 100% with sell_pct for classified trades.
    classified_pct: (buy_vol + sell_vol) / total_vol — how much was classifiable.
                    Low classified_pct means bid/ask was unavailable or wide.
    """
    symbol          : str
    local_symbol    : str
    price_first     : Optional[float] = None
    price_last      : Optional[float] = None
    price_delta     : Optional[float] = None   # last - first
    vwap            : Optional[float] = None
    buy_vol         : float = 0.0
    sell_vol        : float = 0.0
    total_vol       : float = 0.0
    tick_count      : int   = 0
    buy_pct         : Optional[float] = None   # buy / (buy+sell), None if no classified
    classified_pct  : Optional[float] = None   # (buy+sell) / total, None if no total


class FuturesTickStream:
    """
    Tick-by-tick stream for a futures contract using reqTickByTickData('AllLast').

    IB API tick-by-tick data (ib_insync):
      reqTickByTickData returns a Ticker object.
      Each tick-by-tick update appends a TickByTickAllLast namedtuple to
      ticker.tickByTicks and fires ticker.updateEvent.

      TickByTickAllLast fields (from ib_insync.objects):
        tickType          : int   (1=Last, 2=AllLast)
        time              : datetime
        price             : float
        size              : float
        tickAttribLast    : TickAttribLast (pastLimit: bool, unreported: bool)
        exchange          : str
        specialConditions : str

      tickAttribLast.pastLimit: True if trade was at/through the limit price.
      This is NOT an aggressor-side flag — it only means the trade filled
      past the prevailing limit. Direction must be inferred from bid/ask.

      IB does not provide aggressor side on futures ticks. The correct
      method is bid/ask comparison using the live OIStream quote:
        price >= ask → buy-initiated
        price <= bid → sell-initiated
        between     → unclassified

    _on_tick drains ticker.tickByTicks (the list of new ticks since last
    update) rather than reading ticker.last/lastSize (level-1 stream values
    which are not individual tick-by-tick prints).

    Note on subscription limits: ib_insync tick-by-tick streams are limited
    to 3 simultaneous subscriptions per client. With FUT_CHAIN_DEPTH=3 this
    uses all 3 slots for CL. If other tick streams are needed, reduce depth.
    """

    BUFFER_MAX = 5000

    def __init__(self, ib: IB, contract: Contract):
        self._ib          = ib
        self._contract    = contract
        self._ticker      = None
        self._oi_ticker   = None   # set by instrument.py after OIStream starts
        self._buf: deque  = deque(maxlen=self.BUFFER_MAX)
        self._sym         = getattr(contract, 'localSymbol',
                                    getattr(contract, 'symbol',
                                            str(contract.conId)))

    def set_oi_ticker(self, oi_ticker) -> None:
        """
        Provide the OIStream's internal ticker for live bid/ask access.
        Called by instrument.py after _start_oi_streams().
        The OIStream ticker is a reqMktData non-snapshot stream that always
        has current bid/ask for the underlying future.
        """
        self._oi_ticker = oi_ticker

    def start(self) -> None:
        sym = self._sym
        try:
            self._ticker = self._ib.reqTickByTickData(
                self._contract,
                tickType      = 'AllLast',   # required for futures; 'Last' is equities only
                numberOfTicks = 0,           # continuous stream
                ignoreSize    = False,
            )
            self._ticker.updateEvent += self._on_tick
            print(f"[IB] FuturesTickStream started for {sym} "
                  f"(conId={self._contract.conId})")
        except Exception as e:
            print(f"[IB][WARN] FuturesTickStream failed for {sym}: {e}")

    def stop(self) -> None:
        if self._ticker is not None:
            try:
                self._ib.cancelTickByTickData(self._ticker)
            except Exception:
                pass
            self._ticker = None

    def _on_tick(self, ticker, *args) -> None:
        """
        Called by ib_insync on each updateEvent for the ticker.
        Drains ticker.tickByTicks — the list of new TickByTickAllLast
        objects appended since the last event.

        Does NOT read ticker.last / ticker.lastSize — those are level-1
        reqMktData stream values, not individual tick-by-tick prints.
        """
        try:
            # Snapshot bid/ask once per event for all ticks in this batch
            bid = ask = None
            if self._oi_ticker is not None:
                b = getattr(self._oi_ticker, 'bid', None)
                a = getattr(self._oi_ticker, 'ask', None)
                if b is not None and not math.isnan(float(b)) and float(b) > 0:
                    bid = float(b)
                if a is not None and not math.isnan(float(a)) and float(a) > 0:
                    ask = float(a)

            # Drain all new tick-by-tick prints from this event
            for t in ticker.tickByTicks:
                price = getattr(t, 'price', None)
                size  = getattr(t, 'size',  None)
                if price is None or size is None:
                    continue
                try:
                    price = float(price)
                    size  = float(size)
                except (TypeError, ValueError):
                    continue
                if price <= 0 or size < 0:
                    continue
                self._buf.append({
                    'price': price,
                    'size' : size,
                    'bid'  : bid,
                    'ask'  : ask,
                })
        except Exception:
            pass

    def drain_momentum(self) -> 'FuturesMomentum':
        """
        Consume all buffered ticks and return a FuturesMomentum snapshot.
        Buffer is cleared after each call.

        buy_pct is computed as buy_vol / (buy_vol + sell_vol) — fraction of
        classified volume only.  This correctly sums to 100% with sell_pct.

        classified_pct = (buy_vol + sell_vol) / total_vol shows how much
        of the volume was classifiable. Low values mean wide spreads or
        missing bid/ask at time of trade.
        """
        ticks = list(self._buf)
        self._buf.clear()

        sym  = getattr(self._contract, 'symbol', '')
        lsym = self._sym
        m    = FuturesMomentum(symbol=sym, local_symbol=lsym)

        if not ticks:
            return m

        m.tick_count = len(ticks)
        prices  = [t['price'] for t in ticks]
        sizes   = [t['size']  for t in ticks]

        m.price_first = prices[0]
        m.price_last  = prices[-1]
        m.price_delta = m.price_last - m.price_first

        total_vol   = sum(sizes)
        m.total_vol = total_vol

        if total_vol > 0:
            m.vwap = sum(p * s for p, s in zip(prices, sizes)) / total_vol

        # Buy / sell classification via bid/ask comparison
        buy_vol = sell_vol = 0.0
        for t in ticks:
            s, p, bid, ask = t['size'], t['price'], t['bid'], t['ask']
            if bid is not None and ask is not None:
                if p >= ask:
                    buy_vol  += s
                elif p <= bid:
                    sell_vol += s
                # else: unclassified — between bid/ask, not counted either side

        m.buy_vol  = buy_vol
        m.sell_vol = sell_vol

        classified = buy_vol + sell_vol
        if classified > 0:
            # buy_pct as fraction of CLASSIFIED volume only — always sums to 100% with sell_pct
            m.buy_pct = buy_vol / classified
        if total_vol > 0:
            m.classified_pct = classified / total_vol

        return m
