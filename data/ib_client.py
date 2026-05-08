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


def _is_invalid_cached(symbol: str, expiry: str,
                        strike: float, right: str) -> bool:
    key = (symbol, expiry, strike, right)
    exp = _invalid_contract_cache.get(key)
    if exp is None:
        return False
    if time.time() > exp:
        del _invalid_contract_cache[key]
        return False
    return True


def _mark_invalid(symbol: str, expiry: str,
                   strike: float, right: str) -> None:
    ttl = getattr(cfg, 'QUALIFY_ERROR_TTL_SEC', 3600)
    _invalid_contract_cache[(symbol, expiry, strike, right)] = time.time() + ttl


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
    """Discover the front FUT_CHAIN_DEPTH futures for a FOP instrument."""
    sym                   = instrument_cfg['symbol']
    exch                  = instrument_cfg['exchange']
    curr                  = instrument_cfg['currency']
    futures_trading_class = instrument_cfg.get('futures_trading_class', None)
    depth                 = getattr(cfg, 'FUT_CHAIN_DEPTH', 3)

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
        tc_counts = Counter(cd.contract.tradingClass for cd in cds)
        preferred_tc = tc_counts.most_common(1)[0][0]

    print(f"[IB] {sym} futures: using tradingClass='{preferred_tc}' "
          f"({sum(1 for cd in cds if cd.contract.tradingClass == preferred_tc)} contracts), "
          f"depth={depth}")

    qualifying = []
    for cd in cds:
        c = cd.contract
        if c.tradingClass != preferred_tc:
            continue
        exp_dt = parse_expiry_date(c.lastTradeDateOrContractMonth)
        if exp_dt is not None and now < exp_dt <= cutoff:
            qualifying.append(c)

    qualifying = sorted(qualifying, key=lambda c: c.lastTradeDateOrContractMonth)

    if not qualifying:
        all_tc = sorted(
            (cd.contract for cd in cds if cd.contract.tradingClass == preferred_tc),
            key=lambda c: c.lastTradeDateOrContractMonth,
        )
        qualifying = all_tc[:depth]
        print(f"[IB][WARN] No {sym} futures within 62d for class "
              f"'{preferred_tc}'; using nearest {len(qualifying)}.")
    else:
        qualifying = qualifying[:depth]

    print(f"[IB] {sym}: qualifying {len(qualifying)} futures contracts...")
    try:
        qualified = await ib.qualifyContractsAsync(*qualifying)
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
          f"{n_timeouts} batch timeouts, "
          f"{total - len(qualified) - n_unqualified - n_timeouts*_QUALIFY_BATCH_SIZE} other")
    return qualified


# ── FOP chain discovery ───────────────────────────────────────────────────────

async def discover_fop_chain(ib: IB, instrument_cfg: dict,
                              details_cache: dict,
                              futures: list | None = None) -> list:
    """Discover FOP option chain. Includes canonical-underConId fallback for SI/COMEX."""
    sym       = instrument_cfg['symbol']
    exch      = instrument_cfg['exchange']
    curr      = instrument_cfg['currency']
    preferred = instrument_cfg.get('preferred_trading_classes', None)
    depth     = getattr(cfg, 'FUT_CHAIN_DEPTH', 3)
    now       = datetime.now(timezone.utc)

    if futures:
        fut_list = sorted(futures,
                           key=lambda c: c.lastTradeDateOrContractMonth)[:depth]
        print(f"[IB] {sym}: using {len(fut_list)} pre-fetched futures "
              f"(depth={depth}): {[f.localSymbol for f in fut_list]}")
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
        )[:depth]
        print(f"[IB] {sym}: using front {len(fut_list)} futures: "
              f"{[f.localSymbol for f in fut_list]}")

    if not fut_list:
        print(f"[IB][WARN] {sym}: no futures in range.")
        return []

    all_chains     : list = []
    seen_chain_keys: set  = set()

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

    # ── Canonical underConId fallback (SI/COMEX) ──────────────────────────────
    if not all_chains:
        print(f"[IB] {sym}: reqSecDefOptParams found nothing via dated futures conIds.")
        print(f"[IB] {sym}: trying canonical underlying conId via generic FUT qualify...")

        generic_fut          = Contract()
        generic_fut.symbol   = sym
        generic_fut.secType  = 'FUT'
        generic_fut.exchange = exch
        generic_fut.currency = curr
        try:
            qualified_futs = await ib.qualifyContractsAsync(generic_fut)
            canonical_ids = sorted(set(
                c.conId for c in (qualified_futs or [])
                if getattr(c, 'conId', 0) > 0
            ))
        except Exception as e:
            print(f"[IB][WARN] {sym}: generic FUT qualify failed: {e}")
            canonical_ids = []

        if not canonical_ids:
            print(f"[IB] {sym}: falling back to reqContractDetails FOP probe...")
            generic_fop          = Contract()
            generic_fop.symbol   = sym
            generic_fop.secType  = 'FOP'
            generic_fop.exchange = exch
            generic_fop.currency = curr
            sample_cds = await req_contract_details(
                ib, generic_fop,
                label=f"{sym} FOP underConId probe",
                timeout=30.0,
            )
            canonical_ids = sorted(set(
                cd.underConId for cd in (sample_cds or [])
                if getattr(cd, 'underConId', 0) > 0
            ))

        if canonical_ids:
            print(f"[IB] {sym}: retrying reqSecDefOptParams with canonical "
                  f"underConIds: {canonical_ids[:5]}...")
            for under_id in canonical_ids[:5]:
                chains_for_id = await _req_sec_def_opt_params(
                    ib, sym, exch, 'FUT', under_id
                )
                for chain in chains_for_id:
                    key = (chain.tradingClass, frozenset(chain.expirations))
                    if key not in seen_chain_keys:
                        seen_chain_keys.add(key)
                        all_chains.append(chain)
                if all_chains:
                    print(f"[IB] {sym}: fallback succeeded with "
                          f"underConId={under_id} — "
                          f"{len(all_chains)} chains found")
                    break
                await asyncio.sleep(0.3)
        else:
            print(f"[IB][WARN] {sym}: could not determine canonical underConId.")

    if not all_chains:
        print(f"[IB][WARN] {sym}: reqSecDefOptParams returned no chains.")
        return []

    from collections import defaultdict
    tc_exp: dict = defaultdict(set)
    tc_str: dict = defaultdict(set)
    for c in all_chains:
        tc_exp[c.tradingClass].update(c.expirations)
        tc_str[c.tradingClass].update(c.strikes)
    tc_summary = {tc: len(tc_exp[tc]) * len(tc_str[tc]) * 2 for tc in tc_exp}
    print(f"[IB] {sym} chains (pre-filter): "
          f"{dict(sorted(tc_summary.items(), key=lambda x: -x[1]))}")

    if preferred is not None:
        all_chains = [c for c in all_chains if c.tradingClass in preferred]
        print(f"[IB] {sym}: {len(all_chains)} chains after "
              f"preferred_trading_classes={preferred} filter")

    if not all_chains:
        print(f"[IB][WARN] {sym}: no chains after filter.")
        return []

    chain_specs: list[ChainSpec] = []

    for chain in all_chains:
        valid_expiries = set()
        for expiry in chain.expirations:
            exp_dt = parse_expiry_date(expiry)
            if exp_dt is not None and exp_dt < now:
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
            und_con_id    = fut_list[0].conId,
            und_symbol    = fut_list[0].localSymbol,
        ))

    total_combos = sum(
        len(s.expirations) * len(s.strikes) * 2 for s in chain_specs
    )
    print(f"[IB] {sym} FOP: {len(chain_specs)} chain specs cached "
          f"({total_combos} theoretical contracts)")
    return chain_specs


# ── Equity option chain discovery ────────────────────────────────────────────

async def discover_equity_options(ib: IB, instrument_cfg: dict,
                                   details_cache: dict) -> tuple[list, list]:
    sym  = instrument_cfg['symbol']
    exch = instrument_cfg.get('exchange', 'SMART')
    curr = instrument_cfg['currency']
    now  = datetime.now(timezone.utc)

    stk          = Contract()
    stk.symbol   = sym
    stk.secType  = 'STK'
    stk.exchange = exch
    stk.currency = curr

    stk_cds = await req_contract_details(ib, stk, label=f"{sym} STK", timeout=60.0)
    if not stk_cds:
        print(f"[IB][WARN] {sym}: STK not found.")
        return [], []

    underlying = [stk_cds[0].contract]
    und_con_id = underlying[0].conId
    print(f"[IB] {sym}: STK conId={und_con_id}")

    print(f"[IB] {sym}: reqSecDefOptParams...")
    chains = await _req_sec_def_opt_params(ib, sym, '', 'STK', und_con_id)
    if not chains:
        print(f"[IB][WARN] {sym}: no chains returned.")
        return underlying, []

    from collections import defaultdict
    chain_info = {f"{c.exchange}/{c.tradingClass}":
                  len(c.expirations)*len(c.strikes)*2 for c in chains}
    print(f"[IB] {sym} OPT chains: "
          f"{dict(sorted(chain_info.items(), key=lambda x: -x[1]))}")

    primary = [c for c in chains
               if c.exchange == 'SMART' and c.tradingClass == sym]
    if not primary:
        primary = [c for c in chains if c.exchange == 'SMART']
    if not primary:
        primary = chains

    chain_specs: list[ChainSpec] = []
    for chain in primary:
        valid_expiries = set()
        for expiry in chain.expirations:
            exp_dt = parse_expiry_date(expiry)
            if exp_dt is not None and exp_dt < now:
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

    total_combos = sum(len(s.expirations)*len(s.strikes)*2 for s in chain_specs)
    print(f"[IB] {sym} OPT: {len(chain_specs)} chain specs cached "
          f"({total_combos} theoretical contracts)")
    return underlying, chain_specs


# ── Scan-time contract qualification ─────────────────────────────────────────

async def qualify_chain_for_scan(ib: IB,
                                  chain_specs: list,
                                  underlying_price: float | None,
                                  moneyness_band: float,
                                  details_cache: dict) -> list:
    """
    Qualify options within the moneyness band.
    Contracts already in details_cache are reused — no IB round-trip.
    Only new contracts in the band are sent to qualifyContractsAsync.
    """
    from ib_insync import Contract as IBContract
    from types import SimpleNamespace

    _existing_key_to_conid: dict[tuple, int] = {
        (cd.contract.symbol,
         cd.contract.lastTradeDateOrContractMonth,
         cd.contract.strike,
         cd.contract.right): conid
        for conid, cd in details_cache.items()
        if hasattr(cd, 'contract')
    }

    raw: list               = []
    already_qualified: list = []
    n_skipped_invalid       = 0
    n_cache_hits            = 0

    if underlying_price and underlying_price > 0:
        lo = underlying_price * (1 - moneyness_band)
        hi = underlying_price * (1 + moneyness_band)
    else:
        lo, hi = 0.0, float('inf')

    now = datetime.now(timezone.utc)

    for spec in chain_specs:
        for expiry in sorted(spec.expirations):
            exp_dt = parse_expiry_date(expiry)
            if exp_dt is None or exp_dt < now:
                continue
            for strike in sorted(spec.strikes):
                if not (lo <= strike <= hi):
                    continue
                for right in ('C', 'P'):
                    cache_key = (spec.symbol, expiry, strike, right)

                    if _is_invalid_cached(spec.symbol, expiry, strike, right):
                        n_skipped_invalid += 1
                        continue

                    existing_conid = _existing_key_to_conid.get(cache_key)
                    if existing_conid is not None:
                        already_qualified.append(details_cache[existing_conid].contract)
                        n_cache_hits += 1
                        continue

                    ct = IBContract()
                    ct.symbol        = spec.symbol
                    ct.secType       = spec.sec_type
                    ct.exchange      = spec.exchange
                    ct.currency      = spec.currency
                    ct.lastTradeDateOrContractMonth = expiry
                    ct.strike        = strike
                    ct.right         = right
                    ct.multiplier    = spec.multiplier
                    ct.tradingClass  = spec.trading_class
                    raw.append((ct, spec.und_con_id))

    und_price_str = f"{underlying_price:.4g}" if underlying_price else 'N/A'
    print(f"[IB] qualify_chain_for_scan: {len(raw)} to qualify, "
          f"{n_cache_hits} reused, {n_skipped_invalid} invalid "
          f"(±{moneyness_band*100:.0f}% of {und_price_str})")

    if not raw:
        return already_qualified

    contracts   = [ct for ct, _ in raw]
    und_con_ids = {id(ct): uid for ct, uid in raw}

    contract_keys: dict[int, tuple] = {
        id(ct): (ct.symbol,
                 ct.lastTradeDateOrContractMonth,
                 ct.strike,
                 ct.right)
        for ct in contracts
    }

    qualified = await _qualify_contracts_batched(ib, contracts, label='scan')

    qualified_ids = {ct.conId for ct in qualified if getattr(ct, 'conId', 0) > 0}
    n_newly_cached = 0
    for ct in contracts:
        if getattr(ct, 'conId', 0) not in qualified_ids:
            key = contract_keys.get(id(ct))
            if key:
                _mark_invalid(*key)
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
                await asyncio.sleep(0.3)
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
    genericTickList='588' → Futures Open Interest (tick 86).
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
                genericTickList='588',
                snapshot=False,
                regulatorySnapshot=False,
            )
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if self.oi() is not None:
                    break
                await asyncio.sleep(0.3)
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
