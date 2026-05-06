"""
data/ib_client.py
-----------------
All IB Gateway / TWS interactions.

Responsibilities:
  - connect / disconnect
  - contract discovery (futures, FOP chains, equity option chains)
  - snapshot market data (batched, rate-safe)
  - optional tick-by-tick streaming (Option A)
  - underlying price resolution (undPrice field or STK fallback)

Design principles:
  - Every IB call is wrapped in try/except; errors are logged, never raised
    to the caller (caller gets empty list / None instead)
  - Batching and staggered sleeps prevent IB pacing violations
  - No business logic here; this module only fetches and hands back data
"""

import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from ib_insync import IB, Contract, Future

import options_scanner.config as cfg
from options_scanner.data.utils import parse_expiry_date, safe_mid


# ── Failed-contract cache ────────────────────────────────────────────────────
# Tracks (symbol, expiry, strike, right) tuples that returned Error 200 from
# qualifyContractsAsync.  These are skipped in qualify_chain_for_scan for
# QUALIFY_ERROR_TTL_SEC seconds, avoiding repeated console spam and wasted
# gateway requests.  The cache is in-memory only — it resets on restart,
# which is fine because a fresh session re-probes after the TTL.
#
# TTL is read from config: QUALIFY_ERROR_TTL_SEC (default 3600 = 1 hour).

_invalid_contract_cache: dict[tuple, float] = {}   # key -> expiry wall-clock time

# Path to the on-disk cache — populated lazily on first _mark_invalid call
_INVALID_CACHE_PATH: 'Path | None' = None


def _invalid_cache_path() -> 'Path':
    from pathlib import Path
    global _INVALID_CACHE_PATH
    if _INVALID_CACHE_PATH is None:
        _INVALID_CACHE_PATH = Path(getattr(cfg, 'CACHE_DIR', './cache')) / 'qualify_invalid.json'
    return _INVALID_CACHE_PATH


def _load_invalid_cache() -> None:
    """
    Load the persisted invalid-contract cache from disk at startup.
    Entries whose TTL has already elapsed are silently discarded.
    Called once by connect() so the first scan benefits immediately.
    """
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
    """Persist the invalid-contract cache to disk (called after new entries added)."""
    import json
    path = _invalid_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        # Only write entries that are still live
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
    """
    Connect to IB Gateway / TWS.
    Returns True on success, False on failure.
    """
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
    """
    Safe wrapper around reqContractDetailsAsync with timeout.
    Returns empty list on any failure or timeout.
    Large chains (CL=25K+, SI=28K+) can take 30-50s — timeout prevents hang.
    """
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
    """
    Discover futures contracts for a FOP instrument.

    Window: 62 days (matching the option chain discovery window).
    MAX_EXPIRY_DAYS (30d) is for scan filtering, not futures discovery.

    Trading class filter: instrument_cfg['futures_trading_class'] if set,
    otherwise accept the first/primary class IB returns.
    This prevents SI mini contracts (SILK6, tradingClass='SIL') from being
    returned instead of standard SI contracts (tradingClass='SI').
    """
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
    cutoff = now + timedelta(days=62)   # match 2-month option chain window

    # Determine which tradingClass to use
    if futures_trading_class:
        preferred_tc = futures_trading_class
    else:
        # Use the most common tradingClass (by count) as the primary series
        from collections import Counter
        tc_counts = Counter(cd.contract.tradingClass for cd in cds)
        preferred_tc = tc_counts.most_common(1)[0][0]

    print(f"[IB] {sym} futures: using tradingClass='{preferred_tc}' "
          f"({sum(1 for cd in cds if cd.contract.tradingClass == preferred_tc)} contracts)")

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
        # Fallback: nearest 2 contracts of preferred class regardless of window
        all_tc = sorted(
            (cd.contract for cd in cds if cd.contract.tradingClass == preferred_tc),
            key=lambda c: c.lastTradeDateOrContractMonth,
        )
        qualifying = all_tc[:2]
        print(f"[IB][WARN] No {sym} futures within 62d for class "
              f"'{preferred_tc}'; using nearest {len(qualifying)}.")

    # Qualify contracts to ensure conIds are primary/canonical.
    # reqContractDetails can return non-primary conIds that Gateway does not
    # have FOP chain data for (Gateway log: "Missing per exchange strikes").
    # qualifyContracts resolves to the primary registered conId.
    print(f"[IB] {sym}: qualifying {len(qualifying)} futures contracts...")
    try:
        qualified = await ib.qualifyContractsAsync(*qualifying)
        qualifying = [c for c in qualified if getattr(c, 'conId', 0) > 0]
        print(f"[IB] {sym}: futures qualified: "
              f"{[c.localSymbol for c in qualifying]}")
    except Exception as e:
        print(f"[IB][WARN] {sym}: futures qualify failed ({e}), "
              f"using unqualified conIds")

    print(f"[IB] {sym}: {len(qualifying)} futures in 62-day window: "
          f"{[c.localSymbol for c in qualifying]}")
    return qualifying


# ── ChainSpec: stores option chain parameters for deferred qualification ─────
#
# Instead of qualifying thousands of contracts at cache-build time, we store
# the chain parameters (expiries, strikes, tradingClass, multiplier) returned
# by reqSecDefOptParams. Contracts are built and qualified at scan time on the
# small ±20% moneyness subset (~100-400 contracts), keeping qualification fast
# and Gateway buffer usage minimal.
#
# This eliminates:
#   - Gateway output buffer overflow (was: 4598 simultaneous qualify responses)
#   - 8-minute cache build time for TSLA (now: <30s)
#   - 51% "invalid combo" qualification failures polluting logs

from dataclasses import dataclass, field as dc_field

@dataclass
class ChainSpec:
    """
    Option chain parameters returned by reqSecDefOptParams.
    Stored in cache; used to build + qualify contracts at scan time.
    """
    symbol         : str
    sec_type       : str            # 'FOP' or 'OPT'
    exchange       : str
    currency       : str
    trading_class  : str
    multiplier     : str
    expirations    : set            # set of 'YYYYMMDD' strings
    strikes        : set            # set of float
    und_con_id     : int            # conId of the underlying
    und_symbol     : str = ''       # for reference / logging


# ── FOP chain discovery via reqSecDefOptParams ───────────────────────────────
#
# reqSecDefOptParams(symbol, futFopExchange, underlyingSecType, underlyingConId)
# is the IB-recommended API for option chain discovery. Unlike reqContractDetails
# it has NO throttling limitation (per IB docs since API v9.72).
#
# Key facts confirmed from IB documentation and ib_insync source:
#   - underlyingConId is the conId of the SPECIFIC futures contract
#   - Each futures conId returns chains scoped to that future's expiry window
#   - To get 2 months of chains we call it for the front 2 futures
#   - qualifyContractsAsync(*contracts) fires all calls via asyncio.gather
#     simultaneously — we MUST batch to respect the 50 msg/s hard limit
#   - Batch size 50, sleep 1.1s between batches => ~45 msg/s (safely under limit)
#   - snapshot=True is incompatible with genericTickList (separate concern)
#
# Rate limit: 50 messages/second (hard limit, TWS API docs).

_QUALIFY_BATCH_SIZE  = 25     # contracts per qualifyContractsAsync call
# Reduced from 50 to 25: IB Gateway has a ~100KB output buffer per client.
# Firing 50 simultaneous reqContractDetailsAsync calls overflows this buffer,
# causing Gateway to drop responses ("Output exceeded limit, removed first half").
# 25 concurrent calls produce less simultaneous output.
# _QUALIFY_BATCH_SLEEP is read from cfg.QUALIFY_BATCH_SLEEP at runtime (see below)


async def _req_sec_def_opt_params(ib: IB, sym: str, fop_exchange: str,
                                   und_sec_type: str,
                                   und_con_id: int) -> list:
    """
    Async wrapper around ib.reqSecDefOptParamsAsync.
    Returns list of ib_insync.OptionChain objects.

    futFopExchange behaviour per IB docs:
      - For STK: always pass '' (empty string) — IB example: reqSecDefOptParams(0,"IBM","","STK",8314)
      - For FOP: pass the specific exchange (e.g. 'NYMEX', 'COMEX').
        If that returns empty, retry with '' as fallback (some instruments
        require empty string even for FOP).

    No client-side exchange filtering — we trust IB to scope the result.
    Trading class filtering is applied downstream in discover_fop_chain.
    """
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
        # Retry with empty string — some FOP instruments require this
        print(f"[IB] {sym}: no chains with exchange='{fop_exchange}', "
              f"retrying with ''...")
        chains = await _call('')
    return chains


async def _qualify_contracts_batched(ib: IB, contracts: list,
                                      label: str = '') -> list:
    """
    Qualify Contract objects in batches of _QUALIFY_BATCH_SIZE.

    qualifyContractsAsync(*contracts) fires all reqContractDetailsAsync calls
    concurrently via asyncio.gather. Sending 25K contracts in one call would
    fire 25K simultaneous requests, violating the 50 msg/s hard limit.
    We batch to 50 contracts per call with 1.1s sleep between batches.

    Effective rate: 50 msgs / 1.1s ≈ 45 msg/s — safely under the 50 msg/s limit.
    Sleep occurs BETWEEN batches only (not after the last one).

    Returns only qualified contracts with conId > 0.
    """
    qualified  = []
    n_unqualified = 0
    n_timeouts    = 0
    total      = len(contracts)
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

        # Sleep between batches only — not after the last one
        if i + _QUALIFY_BATCH_SIZE < total:
            await asyncio.sleep(cfg.QUALIFY_BATCH_SLEEP)

    # Summary line so we can distinguish "invalid combos" from "Gateway drops"
    print(f"[IB] {label}: qualify complete — "
          f"{len(qualified)} valid, {n_unqualified} invalid combos, "
          f"{n_timeouts} batch timeouts, "
          f"{total - len(qualified) - n_unqualified - n_timeouts*_QUALIFY_BATCH_SIZE} other")
    return qualified


async def discover_fop_chain(ib: IB, instrument_cfg: dict,
                              details_cache: dict,
                              futures: list | None = None) -> list:
    """
    Discover the FOP option chain for the front 2 futures using reqSecDefOptParams.

    Process:
    1. Get the front 2 futures within MAX_EXPIRY_DAYS (or use pre-fetched list)
    2. For each future, call reqSecDefOptParams(sym, exchange, 'FUT', conId)
       — this returns (expirations, strikes) per tradingClass for that future
    3. Deduplicate chains by (tradingClass, frozenset(expirations))
    4. Filter to preferred_trading_classes if configured
    5. Filter expiries to within 2 months (62 days) of today
    6. Build Contract objects for every (expiry, strike, right, tradingClass)
    7. Qualify in batches of 50 at 45 msg/s to populate conIds
    8. Build details_cache entries (underConId = the future's conId)

    Why 2 futures: reqSecDefOptParams scopes results to the passed underlyingConId.
    One future covers one expiry window. Two front-month futures give us the
    full 2-month chain as requested.

    futures: pre-fetched list of Future Contract objects (avoids a duplicate
             IB request when contract_cache already fetched them).
    """
    sym       = instrument_cfg['symbol']
    exch      = instrument_cfg['exchange']
    curr      = instrument_cfg['currency']
    preferred = instrument_cfg.get('preferred_trading_classes', None)
    now       = datetime.now(timezone.utc)
    # No cutoff — full chain cached; contracts pruned only when expired

    # ── Step 1: get front 2 futures ───────────────────────────────────────────
    if futures:
        fut_list = sorted(futures,
                           key=lambda c: c.lastTradeDateOrContractMonth)[:2]
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
        )[:2]
        print(f"[IB] {sym}: using front 2 futures: "
              f"{[f.localSymbol for f in fut_list]}")

    # ── Step 2: reqSecDefOptParams for each future ────────────────────────────
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
        await asyncio.sleep(0.3)  # light pacing between futures

    # ── Fallback: reqSecDefOptParams returned nothing for all futures ─────────
    # This happens for some instruments (e.g. SI/COMEX) where IB's Gateway
    # does not have FOP chain data registered against the dated futures conIds.
    #
    # Root cause (confirmed from IBKR article "Handling Options Chains" and
    # Gateway logs): reqSecDefOptParams requires the CANONICAL underlying conId,
    # not a dated futures contract conId. For SI, SIK6 (conId=712566019) is a
    # dated contract; the canonical SI underlying conId is different.
    #
    # Fix: qualify a generic (undated) FUT contract to get the canonical conId,
    # then retry reqSecDefOptParams with that. This mirrors what the IBKR Web API
    # article describes: /secdef/search returns the canonical underConid, which
    # is then used for strike/expiry lookups — not the dated futures conId.
    if not all_chains:
        print(f"[IB] {sym}: reqSecDefOptParams found nothing via dated futures conIds.")
        print(f"[IB] {sym}: trying canonical underlying conId via generic FUT qualify...")

        # Qualify a generic (undated) FUT to get the canonical underlying conId
        generic_fut          = Contract()
        generic_fut.symbol   = sym
        generic_fut.secType  = 'FUT'
        generic_fut.exchange = exch
        generic_fut.currency = curr
        # No lastTradeDateOrContractMonth — let IB resolve to canonical contract
        try:
            qualified_futs = await ib.qualifyContractsAsync(generic_fut)
            canonical_ids = sorted(set(
                c.conId for c in (qualified_futs or [])
                if getattr(c, 'conId', 0) > 0
            ))
        except Exception as e:
            print(f"[IB][WARN] {sym}: generic FUT qualify failed: {e}")
            canonical_ids = []

        # Also probe via reqContractDetails on a generic FOP — IB returns
        # underConId in ContractDetails which is always the canonical ID
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

    # ── Step 3: log trading class summary ────────────────────────────────────
    from collections import defaultdict
    tc_exp: dict = defaultdict(set)
    tc_str: dict = defaultdict(set)
    for c in all_chains:
        tc_exp[c.tradingClass].update(c.expirations)
        tc_str[c.tradingClass].update(c.strikes)
    tc_summary = {tc: len(tc_exp[tc]) * len(tc_str[tc]) * 2 for tc in tc_exp}
    print(f"[IB] {sym} chains (pre-filter): "
          f"{dict(sorted(tc_summary.items(), key=lambda x: -x[1]))}")

    # ── Step 4: filter to preferred trading classes ───────────────────────────
    if preferred is not None:
        all_chains = [c for c in all_chains if c.tradingClass in preferred]
        print(f"[IB] {sym}: {len(all_chains)} chains after "
              f"preferred_trading_classes={preferred} filter")

    if not all_chains:
        print(f"[IB][WARN] {sym}: no chains after filter.")
        return []

    # ── Step 5: build ChainSpec objects (no qualification needed) ────────────
    # Store the FULL chain — all expiries that have not yet expired.
    # No moneyness or time-window filter here: contracts only leave the cache
    # when their expiry date has passed.  Moneyness filtering happens at scan
    # time inside qualify_chain_for_scan().
    chain_specs: list[ChainSpec] = []

    for chain in all_chains:
        # Only exclude expiries that are already in the past
        valid_expiries = set()
        for expiry in chain.expirations:
            exp_dt = parse_expiry_date(expiry)
            if exp_dt is not None and exp_dt < now:
                continue   # already expired — skip
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
          f"({total_combos} theoretical contracts across all non-expired expiries, "
          f"qualify at scan time on ±20% subset)")
    return chain_specs


# ── Equity option chain discovery ────────────────────────────────────────────

async def discover_equity_options(ib: IB, instrument_cfg: dict,
                                   details_cache: dict) -> tuple[list, list]:
    """
    Discover equity underlying + option chain using reqSecDefOptParams.

    Process:
    1. Qualify the STK contract to get underlyingConId
    2. reqSecDefOptParams(sym, '', 'STK', conId) — no throttling
    3. Select SMART/primary tradingClass chain
    4. Filter to 2-month expiry window
    5. Build Contract objects, qualify in batches of 50 at 45 msg/s
    """
    sym  = instrument_cfg['symbol']
    exch = instrument_cfg.get('exchange', 'SMART')
    curr = instrument_cfg['currency']
    now = datetime.now(timezone.utc)
    # No cutoff — full chain cached; contracts pruned only when expired

    # ── Step 1: qualify STK ───────────────────────────────────────────────────
    stk          = Contract()
    stk.symbol   = sym
    stk.secType  = 'STK'
    stk.exchange = exch
    stk.currency = curr

    stk_cds = await req_contract_details(ib, stk, label=f"{sym} STK",
                                          timeout=60.0)
    if not stk_cds:
        print(f"[IB][WARN] {sym}: STK not found.")
        return [], []

    underlying = [stk_cds[0].contract]
    und_con_id = underlying[0].conId
    print(f"[IB] {sym}: STK conId={und_con_id}")

    # ── Step 2: reqSecDefOptParams ────────────────────────────────────────────
    print(f"[IB] {sym}: reqSecDefOptParams...")
    chains = await _req_sec_def_opt_params(ib, sym, '', 'STK', und_con_id)
    if not chains:
        print(f"[IB][WARN] {sym}: no chains returned.")
        return underlying, []

    # Log all chains
    from collections import defaultdict
    chain_info = {f"{c.exchange}/{c.tradingClass}":
                  len(c.expirations)*len(c.strikes)*2 for c in chains}
    print(f"[IB] {sym} OPT chains: "
          f"{dict(sorted(chain_info.items(), key=lambda x: -x[1]))}")

    # Select: SMART exchange + tradingClass == symbol (primary chain)
    primary = [c for c in chains
               if c.exchange == 'SMART' and c.tradingClass == sym]
    if not primary:
        primary = [c for c in chains if c.exchange == 'SMART']
    if not primary:
        primary = chains
    print(f"[IB] {sym}: using {len(primary)} chain(s) for build")

    # ── Step 3: build ChainSpec objects (no qualification at cache time) ──────
    # Store the FULL chain — all non-expired expiries.
    # No moneyness or time-window filter here: contracts only leave the cache
    # when their expiry date has passed.  Moneyness filtering happens at scan
    # time inside qualify_chain_for_scan().
    chain_specs: list[ChainSpec] = []

    for chain in primary:
        valid_expiries = set()
        for expiry in chain.expirations:
            exp_dt = parse_expiry_date(expiry)
            if exp_dt is not None and exp_dt < now:
                continue   # already expired — skip
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
    return underlying, chain_specs


# ── Scan-time contract qualification ─────────────────────────────────────────

async def qualify_chain_for_scan(ib: IB,
                                  chain_specs: list,
                                  underlying_price: float | None,
                                  moneyness_band: float,
                                  details_cache: dict) -> list:
    """
    At scan time: build and qualify the option contracts from cached ChainSpecs
    that fall within the moneyness band around the current underlying price.

    This is the only point where qualifyContractsAsync is called.
    With a ±20% band and typical chains, this produces ~100-400 contracts —
    far fewer than the full theoretical matrix and well within Gateway limits.

    Returns list of qualified Contract objects.
    Populates details_cache[conId] = SimpleNamespace(contract, underConId).
    """
    from ib_insync import Contract as IBContract
    from types import SimpleNamespace

    raw: list = []
    n_skipped_invalid = 0

    for spec in chain_specs:
        # Moneyness filter (skip if no price available — include all)
        if underlying_price and underlying_price > 0:
            lo = underlying_price * (1 - moneyness_band)
            hi = underlying_price * (1 + moneyness_band)
        else:
            lo, hi = 0.0, float('inf')

        now = datetime.now(timezone.utc)
        for expiry in sorted(spec.expirations):
            exp_dt = parse_expiry_date(expiry)
            if exp_dt is None or exp_dt < now:
                continue
            for strike in sorted(spec.strikes):
                if not (lo <= strike <= hi):
                    continue
                for right in ('C', 'P'):
                    # Skip contracts that previously returned Error 200 and
                    # whose TTL has not yet expired.  This avoids re-sending
                    # known-invalid combos (e.g. TSLA LEAPS with 2.5pt strike
                    # spacing) on every scan, eliminating the console spam.
                    if _is_invalid_cached(spec.symbol, expiry, strike, right):
                        n_skipped_invalid += 1
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

    if not raw and n_skipped_invalid == 0:
        return []

    contracts    = [ct for ct, _ in raw]
    und_con_ids  = {id(ct): uid for ct, uid in raw}

    und_price_str = f"{underlying_price:.4g}" if underlying_price else 'N/A'
    skip_note = f", {n_skipped_invalid} skipped (cached invalid)" if n_skipped_invalid else ""
    print(f"[IB] qualify_chain_for_scan: {len(contracts)} contracts "
          f"(±{moneyness_band*100:.0f}% of {und_price_str}{skip_note})")

    if not contracts:
        return []

    # Build a lookup from contract identity to its key tuple so we can
    # learn which contracts failed after the qualify call returns.
    contract_keys: dict[int, tuple] = {
        id(ct): (ct.symbol,
                 ct.lastTradeDateOrContractMonth,
                 ct.strike,
                 ct.right)
        for ct in contracts
    }

    qualified = await _qualify_contracts_batched(
        ib, contracts, label='scan'
    )

    # Learn failures: any contract we sent that did NOT come back qualified
    # (and has no conId > 0) is marked invalid for QUALIFY_ERROR_TTL_SEC.
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

    for ct in qualified:
        cd            = SimpleNamespace()
        cd.contract   = ct
        cd.underConId = und_con_ids.get(id(ct), 0)
        details_cache[ct.conId] = cd

    return qualified


# ── Snapshot market data ──────────────────────────────────────────────────────

async def fetch_snapshot(ib: IB, contracts: list,
                          timeout: float = None) -> dict:
    """
    Fetch snapshot market data for a list of contracts.
    Returns dict: conId -> ticker.

    Sends requests in batches of IB_BATCH_SIZE.  After each batch we wait
    up to per_batch_timeout seconds for that batch to fill before moving on.
    This prevents a huge chain from blocking forever — each batch is
    time-bounded independently.

    snapshot=True is incompatible with genericTickList on IB — generic ticks
    require a persistent non-snapshot stream.  We use empty genericTickList.
    """
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

        # Wait for this batch to fill (time-bounded per batch)
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
        # Small inter-batch pause to respect IB pacing
        await asyncio.sleep(0.3)

    return tickers


# ── Underlying price ──────────────────────────────────────────────────────────

async def resolve_underlying_price(ib: IB,
                                    instrument_cfg: dict,
                                    opt_tickers: dict) -> Optional[float]:
    """
    For equity instruments: try undPrice from any option ticker first,
    then fall back to a fresh STK snapshot.
    NOTE: undPrice is unreliable on IB snapshots — prefer start_equity_stream()
    for a persistent underlying quote that is updated every scan.
    """
    # Try undPrice from option tickers (unreliable but zero cost to check)
    if instrument_cfg.get('use_und_price_field', True):
        for t in opt_tickers.values():
            und = getattr(t, 'undPrice', None)
            if und and float(und) > 0:
                return float(und)

    # STK snapshot fallback
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


class EquityStream:
    """
    Persistent streaming quote for an equity underlying (STK).

    IB only populates undPrice on option tickers when a live streaming
    quote for the underlying is open simultaneously.  This class opens
    that stream at scanner startup and keeps it alive across scan cycles.

    Usage:
        stream = EquityStream(ib, instrument_cfg)
        await stream.start()           # once at startup
        price  = stream.price()        # called each scan — always instant
        stream.stop()                  # on shutdown
    """

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
            # Persistent (non-snapshot) streaming quote
            self._ticker = self._ib.reqMktData(
                self._contract,
                genericTickList='100,101,104,106',
                snapshot=False,
                regulatorySnapshot=False,
            )
            # Wait briefly for first price to arrive
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
        """Return current mid/last/close from the streaming ticker."""
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


# ── Tick-by-tick streaming (Option A) ────────────────────────────────────────

class TickStream:
    """
    Manages a tick-by-tick stream for one contract.
    Collects prints into a buffer; caller drains the buffer each scan.

    Only active when USE_STREAMING_TICKS=True.
    """

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
        """Return and clear buffered prints."""
        buf          = self._prints[:]
        self._prints = []
        return buf

    def _on_tick(self, ticker, tick_type, *args) -> None:
        try:
            self._prints.append({
                'ts'    : datetime.now(timezone.utc).isoformat(),
                'price' : float(ticker.last) if ticker.last else None,
                'size'  : float(ticker.lastSize) if ticker.lastSize else None,
                'conId' : self._contract.conId,
            })
        except Exception:
            pass


# ── OI streaming for FOP underlyings ─────────────────────────────────────────

class OIStream:
    """
    Persistent streaming quote for a FOP underlying future.

    IB does not populate OI on snapshot=True requests (snapshot is
    incompatible with genericTickList).  This class opens a persistent
    non-snapshot stream per underlying future and makes the latest open
    interest available to the scan loop via oi().

    Correct generic tick for FUTURES open interest:
      genericTickList='588'  →  Futures Open Interest
      Delivered via tickSize() as tick ID 86.
      Accessible in ib_insync as Ticker.futuresOpenInterest.

    Do NOT use tick 101 (Option Call/Put OI — aggregate for the
    underlying's option chain, tick IDs 27/28) or tick 22 (deprecated,
    no longer populated by IB).

    Usage (from InstrumentScanner):
        stream = OIStream(ib, future_contract)
        await stream.start()           # once at startup / discovery
        oi_value = stream.oi()         # called each scan — always instant
        stream.stop()                  # on shutdown

    One OIStream per underlying contract is sufficient for the macro OI
    picture used by OIPCSignalModel.  Option-level OI (per strike/expiry)
    still comes from the option snapshot rows — IB only populates those
    after at least one session with a persistent stream active.
    OIPCSignalModel handles None gracefully via oi_pc_min_observations.
    """

    def __init__(self, ib: IB, contract: Contract):
        self._ib       = ib
        self._contract = contract
        self._ticker   = None

    async def start(self) -> None:
        sym = getattr(self._contract, 'symbol', str(self._contract.conId))
        try:
            # genericTickList '588' = Futures Open Interest (tick 86)
            # snapshot=False required — snapshot is incompatible with genericTickList
            self._ticker = self._ib.reqMktData(
                self._contract,
                genericTickList='588',
                snapshot=False,
                regulatorySnapshot=False,
            )
            # Wait briefly for the first OI tick to arrive
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

    def oi(self) -> Optional[float]:
        """
        Return the latest futures open interest, or None if not yet received.
        Reads Ticker.futuresOpenInterest (tick ID 86, generic tick 588).
        """
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
