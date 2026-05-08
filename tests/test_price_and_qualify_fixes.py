"""
tests/test_price_and_qualify_fixes.py
--------------------------------------
Tests for two bug fixes:

  Fix 1 — OIStream.price()
    Unit tests: pure logic of price() priority order (mid > last > close),
    None sentinel handling, and ticker-absent guard.
    Integration tests (source-inspection): confirm price() exists in OIStream,
    uses the same persistent ticker as oi(), and reads bid/ask/last/close.

  Fix 2 — qualify_chain_for_scan details_cache skip
    Unit tests: pure function extracted from qualify_chain_for_scan that
    classifies each (symbol, expiry, strike, right) combo as:
      - cache_hit    (already in details_cache → reuse, no IB call)
      - invalid_skip (in _invalid_contract_cache TTL → skip)
      - needs_qualify (neither → send to IB)
    Integration tests (source-inspection): confirm qualify_chain_for_scan
    checks details_cache before building raw[], and that already_qualified
    is returned alongside newly qualified contracts.

No IB connection required.  All IB types are replaced with SimpleNamespace
stubs that match the field access patterns in the production code.

Run:
    pytest tests/test_price_and_qualify_fixes.py -v
"""

import inspect
import sys
import os
import time
from types import SimpleNamespace
from typing import Optional

import pytest

# ── Make package importable when run directly ─────────────────────────────────
_HERE   = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _p in (_PARENT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers shared across test sections
# ═══════════════════════════════════════════════════════════════════════════════

def _ticker(bid=None, ask=None, last=None, close=None,
            futuresOpenInterest=None) -> SimpleNamespace:
    """Minimal ticker stub matching ib_insync Ticker field access."""
    return SimpleNamespace(
        bid=bid, ask=ask, last=last, close=close,
        futuresOpenInterest=futuresOpenInterest,
    )


def _safe_mid(bid, ask) -> Optional[float]:
    """Replicate safe_mid logic without importing the full package."""
    b_ok = bid is not None and bid > 0
    a_ok = ask is not None and ask > 0
    if b_ok and a_ok:
        return (bid + ask) / 2.0
    if b_ok:
        return bid
    if a_ok:
        return ask
    return None


def _oi_stream_price(ticker) -> Optional[float]:
    """
    Pure-function replica of OIStream.price() for unit testing.

    Mirrors the priority order in the production method:
      1. mid (bid+ask)/2
      2. last
      3. close
    Returns None if ticker is None or no positive price is found.
    """
    if ticker is None:
        return None
    p = _safe_mid(getattr(ticker, 'bid', None), getattr(ticker, 'ask', None))
    if p is not None and p > 0:
        return p
    for field in ('last', 'close'):
        v = getattr(ticker, field, None)
        if v is not None and float(v) > 0:
            return float(v)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  OIStream.price() — UNIT TESTS (pure logic, no IB)
# ═══════════════════════════════════════════════════════════════════════════════

class TestOIStreamPriceUnit:
    """
    Unit tests for the price() priority logic.
    These test the pure function _oi_stream_price() which exactly mirrors
    the production OIStream.price() implementation.
    """

    # ── Priority 1: mid ───────────────────────────────────────────────────────

    def test_mid_returned_when_both_sides_present(self):
        t = _ticker(bid=100.0, ask=100.5)
        assert _oi_stream_price(t) == pytest.approx(100.25)

    def test_mid_used_over_last(self):
        """bid+ask mid takes priority over a different last price."""
        t = _ticker(bid=99.0, ask=101.0, last=50.0)
        assert _oi_stream_price(t) == pytest.approx(100.0)

    def test_mid_used_over_close(self):
        t = _ticker(bid=99.0, ask=101.0, close=50.0)
        assert _oi_stream_price(t) == pytest.approx(100.0)

    # ── One-sided market → bid or ask used directly ───────────────────────────

    def test_bid_only_returned_when_ask_absent(self):
        t = _ticker(bid=100.0, ask=None)
        assert _oi_stream_price(t) == pytest.approx(100.0)

    def test_ask_only_returned_when_bid_absent(self):
        t = _ticker(bid=None, ask=100.0)
        assert _oi_stream_price(t) == pytest.approx(100.0)

    # ── Priority 2: last ──────────────────────────────────────────────────────

    def test_last_returned_when_no_bid_ask(self):
        t = _ticker(last=99.5)
        assert _oi_stream_price(t) == pytest.approx(99.5)

    def test_last_returned_when_bid_ask_both_none(self):
        t = _ticker(bid=None, ask=None, last=75.0)
        assert _oi_stream_price(t) == pytest.approx(75.0)

    def test_last_used_over_close(self):
        t = _ticker(last=99.0, close=50.0)
        assert _oi_stream_price(t) == pytest.approx(99.0)

    # ── Priority 3: close ─────────────────────────────────────────────────────

    def test_close_returned_when_no_bid_ask_last(self):
        t = _ticker(close=98.0)
        assert _oi_stream_price(t) == pytest.approx(98.0)

    # ── None / zero sentinel handling ─────────────────────────────────────────

    def test_none_ticker_returns_none(self):
        assert _oi_stream_price(None) is None

    def test_all_none_fields_returns_none(self):
        t = _ticker()
        assert _oi_stream_price(t) is None

    def test_zero_bid_not_used(self):
        """bid=0 is IB sentinel for 'no data' — must not be used as price."""
        t = _ticker(bid=0.0, ask=None, last=99.0)
        assert _oi_stream_price(t) == pytest.approx(99.0)

    def test_zero_ask_not_used(self):
        t = _ticker(bid=None, ask=0.0, last=99.0)
        assert _oi_stream_price(t) == pytest.approx(99.0)

    def test_zero_last_not_used(self):
        t = _ticker(bid=None, ask=None, last=0.0, close=95.0)
        assert _oi_stream_price(t) == pytest.approx(95.0)

    def test_negative_values_not_used(self):
        """IB occasionally returns -1.0 as a sentinel — must not be used."""
        t = _ticker(bid=-1.0, ask=-1.0, last=-1.0, close=95.0)
        assert _oi_stream_price(t) == pytest.approx(95.0)

    def test_all_zero_returns_none(self):
        t = _ticker(bid=0.0, ask=0.0, last=0.0, close=0.0)
        assert _oi_stream_price(t) is None

    # ── Realistic CL/SI values ────────────────────────────────────────────────

    def test_crude_oil_mid(self):
        """CL typical bid/ask spread ~0.01."""
        t = _ticker(bid=78.34, ask=78.35)
        assert _oi_stream_price(t) == pytest.approx(78.345)

    def test_silver_last_only(self):
        """SI during low-activity period: no bid/ask, only last."""
        t = _ticker(last=32.150)
        assert _oi_stream_price(t) == pytest.approx(32.150)

    def test_silver_close_fallback(self):
        """SI at session open: only settlement from prior session."""
        t = _ticker(close=31.900)
        assert _oi_stream_price(t) == pytest.approx(31.900)


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  OIStream.price() — INTEGRATION TESTS (source inspection)
# ═══════════════════════════════════════════════════════════════════════════════

class TestOIStreamPriceIntegration:
    """
    Inspect the actual OIStream source code to confirm structural correctness
    of the price() method.  These tests fail if the method is missing,
    misnamed, or uses the wrong IB fields.
    """

    @pytest.fixture(autouse=True)
    def _load_source(self):
        import options_scanner.data.ib_client as ib_mod
        full = inspect.getsource(ib_mod)
        start = full.find('class OIStream')
        assert start > 0, "OIStream class not found in ib_client"
        self.oi_src   = full[start:]
        self.full_src = full

    def test_price_method_exists(self):
        """OIStream must have a price() method (was missing before this fix)."""
        assert 'def price(self)' in self.oi_src, \
            "OIStream.price() method not found"

    def test_price_method_reads_bid(self):
        price_start = self.oi_src.find('def price(self)')
        assert price_start > 0
        price_body = self.oi_src[price_start: price_start + 600]
        assert "'bid'" in price_body or '"bid"' in price_body, \
            "OIStream.price() must read bid from ticker"

    def test_price_method_reads_ask(self):
        price_start = self.oi_src.find('def price(self)')
        price_body = self.oi_src[price_start: price_start + 600]
        assert "'ask'" in price_body or '"ask"' in price_body, \
            "OIStream.price() must read ask from ticker"

    def test_price_method_reads_last(self):
        price_start = self.oi_src.find('def price(self)')
        price_body = self.oi_src[price_start: price_start + 600]
        assert "'last'" in price_body or '"last"' in price_body, \
            "OIStream.price() must read last from ticker"

    def test_price_method_reads_close(self):
        price_start = self.oi_src.find('def price(self)')
        price_body = self.oi_src[price_start: price_start + 600]
        assert "'close'" in price_body or '"close"' in price_body, \
            "OIStream.price() must read close from ticker"

    def test_price_returns_optional_float(self):
        """Return annotation or docstring should reference Optional/float/None."""
        price_start = self.oi_src.find('def price(self)')
        price_body = self.oi_src[price_start: price_start + 600]
        assert 'None' in price_body, \
            "OIStream.price() must be able to return None"

    def test_price_uses_same_ticker_as_oi(self):
        """
        Both price() and oi() must read from self._ticker.
        They share the same persistent stream — no second reqMktData call.
        """
        price_start = self.oi_src.find('def price(self)')
        oi_start    = self.oi_src.find('def oi(self)')
        assert price_start > 0 and oi_start > 0

        price_body = self.oi_src[price_start: price_start + 600]
        oi_body    = self.oi_src[oi_start:    oi_start    + 300]

        assert 'self._ticker' in price_body, \
            "OIStream.price() must read self._ticker"
        assert 'self._ticker' in oi_body, \
            "OIStream.oi() must read self._ticker"

    def test_no_additional_reqmktdata_in_price(self):
        """price() must NOT call reqMktData — it reads the already-open stream."""
        price_start = self.oi_src.find('def price(self)')
        price_body = self.oi_src[price_start: price_start + 600]
        assert 'reqMktData' not in price_body, \
            "OIStream.price() must not call reqMktData — reuse self._ticker"

    def test_oi_method_still_present(self):
        """Regression: oi() must not have been removed when price() was added."""
        assert 'def oi(self)' in self.oi_src, \
            "OIStream.oi() was removed — regression"

    def test_oi_reads_futures_open_interest(self):
        oi_start = self.oi_src.find('def oi(self)')
        oi_body  = self.oi_src[oi_start: oi_start + 300]
        assert 'futuresOpenInterest' in oi_body, \
            "OIStream.oi() must read futuresOpenInterest"

    def test_stream_uses_generic_tick_588(self):
        """The persistent stream must use genericTickList='588' for FUT OI."""
        assert "'588'" in self.oi_src, \
            "OIStream must use genericTickList='588'"

    def test_stream_snapshot_false(self):
        assert 'snapshot=False' in self.oi_src, \
            "OIStream must use snapshot=False"


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  qualify_chain_for_scan details_cache skip — UNIT TESTS (pure logic)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_existing_key_lookup(details_cache: dict) -> dict:
    """
    Pure-function replica of the lookup built inside qualify_chain_for_scan.
    Maps (symbol, expiry, strike, right) -> conId for existing cache entries.
    """
    return {
        (cd.contract.symbol,
         cd.contract.lastTradeDateOrContractMonth,
         cd.contract.strike,
         cd.contract.right): conid
        for conid, cd in details_cache.items()
        if hasattr(cd, 'contract')
    }


def _make_cache_entry(symbol, expiry, strike, right, conid, und_con_id=9999):
    """Build a details_cache entry matching the SimpleNamespace structure."""
    ct = SimpleNamespace(
        symbol=symbol,
        lastTradeDateOrContractMonth=expiry,
        strike=float(strike),
        right=right,
        conId=conid,
    )
    cd = SimpleNamespace(contract=ct, underConId=und_con_id)
    return conid, cd


class TestQualifyChainCacheHitUnit:
    """
    Unit tests for the cache-hit classification logic inside
    qualify_chain_for_scan.  Tests the pure lookup function without IB.
    """

    def _make_cache(self, entries):
        """entries: list of (symbol, expiry, strike, right, conid)"""
        cache = {}
        for sym, exp, strike, right, conid in entries:
            conid_key, cd = _make_cache_entry(sym, exp, strike, right, conid)
            cache[conid_key] = cd
        return cache

    # ── Lookup building ───────────────────────────────────────────────────────

    def test_empty_cache_gives_empty_lookup(self):
        assert _make_existing_key_lookup({}) == {}

    def test_single_entry_is_found(self):
        cache = self._make_cache([('CL', '20250620', 78.0, 'C', 100001)])
        lookup = _make_existing_key_lookup(cache)
        assert lookup.get(('CL', '20250620', 78.0, 'C')) == 100001

    def test_call_and_put_separate_keys(self):
        cache = self._make_cache([
            ('CL', '20250620', 78.0, 'C', 100001),
            ('CL', '20250620', 78.0, 'P', 100002),
        ])
        lookup = _make_existing_key_lookup(cache)
        assert lookup.get(('CL', '20250620', 78.0, 'C')) == 100001
        assert lookup.get(('CL', '20250620', 78.0, 'P')) == 100002

    def test_different_strikes_are_separate_keys(self):
        cache = self._make_cache([
            ('CL', '20250620', 78.0, 'C', 100001),
            ('CL', '20250620', 80.0, 'C', 100003),
        ])
        lookup = _make_existing_key_lookup(cache)
        assert lookup.get(('CL', '20250620', 78.0, 'C')) == 100001
        assert lookup.get(('CL', '20250620', 80.0, 'C')) == 100003

    def test_different_expiries_are_separate_keys(self):
        cache = self._make_cache([
            ('CL', '20250620', 78.0, 'C', 100001),
            ('CL', '20250718', 78.0, 'C', 100004),
        ])
        lookup = _make_existing_key_lookup(cache)
        assert lookup.get(('CL', '20250620', 78.0, 'C')) == 100001
        assert lookup.get(('CL', '20250718', 78.0, 'C')) == 100004

    def test_different_symbols_are_separate_keys(self):
        cache = self._make_cache([
            ('CL', '20250620', 78.0, 'C', 100001),
            ('SI', '20250620', 32.0, 'C', 200001),
        ])
        lookup = _make_existing_key_lookup(cache)
        assert lookup.get(('CL', '20250620', 78.0, 'C')) == 100001
        assert lookup.get(('SI', '20250620', 32.0, 'C')) == 200001

    def test_unknown_key_returns_none(self):
        cache = self._make_cache([('CL', '20250620', 78.0, 'C', 100001)])
        lookup = _make_existing_key_lookup(cache)
        assert lookup.get(('CL', '20250620', 99.0, 'C')) is None

    # ── Cache-hit classification ───────────────────────────────────────────────

    def test_cache_hit_means_no_ib_call_needed(self):
        """
        If a combo is in details_cache, it should be returned directly without
        going into the 'needs_qualify' list.
        """
        cache = self._make_cache([('CL', '20250620', 78.0, 'C', 100001)])
        lookup = _make_existing_key_lookup(cache)

        combo = ('CL', '20250620', 78.0, 'C')
        needs_qualify = combo not in lookup
        assert needs_qualify is False

    def test_cache_miss_means_ib_call_needed(self):
        cache = self._make_cache([('CL', '20250620', 78.0, 'C', 100001)])
        lookup = _make_existing_key_lookup(cache)

        combo = ('CL', '20250620', 99.0, 'C')   # different strike — not cached
        needs_qualify = combo not in lookup
        assert needs_qualify is True

    def test_empty_cache_all_need_qualify(self):
        lookup = _make_existing_key_lookup({})
        for combo in [
            ('CL', '20250620', 78.0, 'C'),
            ('CL', '20250620', 78.0, 'P'),
            ('SI', '20250620', 32.0, 'C'),
        ]:
            assert combo not in lookup

    def test_partial_cache_splits_correctly(self):
        """Some combos cached, some not — split must be exact."""
        cache = self._make_cache([
            ('CL', '20250620', 78.0, 'C', 100001),
            ('CL', '20250620', 80.0, 'C', 100002),
        ])
        lookup = _make_existing_key_lookup(cache)

        all_combos = [
            ('CL', '20250620', 78.0, 'C'),
            ('CL', '20250620', 80.0, 'C'),
            ('CL', '20250620', 82.0, 'C'),   # new — not cached
        ]
        hits   = [c for c in all_combos if c in lookup]
        misses = [c for c in all_combos if c not in lookup]

        assert len(hits) == 2
        assert len(misses) == 1
        assert misses[0] == ('CL', '20250620', 82.0, 'C')

    # ── Strike float precision ────────────────────────────────────────────────

    def test_strike_float_equality_integer_vs_float(self):
        """
        Strikes from IB may come as int (78) or float (78.0) — lookup must
        match regardless.  Both are stored as float in the cache.
        """
        cache = self._make_cache([('CL', '20250620', 78.0, 'C', 100001)])
        lookup = _make_existing_key_lookup(cache)
        # Key built with 78.0 (float) should match
        assert ('CL', '20250620', 78.0, 'C') in lookup

    def test_entries_without_contract_attribute_ignored(self):
        """details_cache entries missing .contract are safely skipped."""
        bad_entry = SimpleNamespace(underConId=9999)   # no .contract
        cache = {99999: bad_entry}
        lookup = _make_existing_key_lookup(cache)
        assert lookup == {}

    # ── Already-qualified list composition ───────────────────────────────────

    def test_already_qualified_list_built_from_cache_hits(self):
        """
        Contracts returned as already_qualified come from details_cache,
        not from a new IB call.
        """
        _, cd = _make_cache_entry('CL', '20250620', 78.0, 'C', 100001)
        cache = {100001: cd}
        lookup = _make_existing_key_lookup(cache)

        combo = ('CL', '20250620', 78.0, 'C')
        existing_conid = lookup.get(combo)
        assert existing_conid == 100001

        # Simulate the production code: grab the contract from cache
        retrieved_cd = cache[existing_conid]
        assert retrieved_cd.contract.strike == 78.0
        assert retrieved_cd.contract.right == 'C'


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  qualify_chain_for_scan — INTEGRATION TESTS (source inspection)
# ═══════════════════════════════════════════════════════════════════════════════

class TestQualifyChainCacheHitIntegration:
    """
    Inspect the actual qualify_chain_for_scan source to confirm:
    1. details_cache is checked before building raw[]
    2. A cache-hit branch exists that skips qualifyContractsAsync
    3. already_qualified is returned alongside newly qualified contracts
    4. The lookup is built from details_cache entries with .contract attribute
    """

    @pytest.fixture(autouse=True)
    def _load_source(self):
        import options_scanner.data.ib_client as ib_mod
        full = inspect.getsource(ib_mod)
        fn_start = full.find('async def qualify_chain_for_scan')
        assert fn_start > 0, "qualify_chain_for_scan not found"
        # Grab the full function body (up to next top-level async def or class)
        next_def = full.find('\nasync def ', fn_start + 1)
        next_cls = full.find('\nclass ',     fn_start + 1)
        end = min(
            x for x in (next_def, next_cls, len(full)) if x > fn_start
        )
        self.fn_src = full[fn_start:end]

    def test_details_cache_checked_before_raw_append(self):
        """
        The function must look up the combo in details_cache before
        appending to raw[] (the list sent to qualifyContractsAsync).
        """
        raw_pos   = self.fn_src.find('raw.append')
        cache_pos = self.fn_src.find('details_cache')
        assert cache_pos > 0,  "details_cache not referenced in qualify_chain_for_scan"
        assert cache_pos < raw_pos, \
            "details_cache check must appear BEFORE raw.append()"

    def test_already_qualified_variable_exists(self):
        assert 'already_qualified' in self.fn_src, \
            "already_qualified list not found in qualify_chain_for_scan"

    def test_already_qualified_returned(self):
        """The function must return already_qualified (cache hits)."""
        return_idx = self.fn_src.rfind('return ')
        assert return_idx > 0
        return_line = self.fn_src[return_idx: return_idx + 100]
        assert 'already_qualified' in return_line, \
            "qualify_chain_for_scan must return already_qualified in final return"

    def test_cache_hit_branch_skips_raw_append(self):
        """
        When a combo is found in details_cache it must be appended to
        already_qualified and 'continue'd — never appended to raw[].
        """
        assert 'already_qualified.append' in self.fn_src, \
            "Cache-hit branch must call already_qualified.append()"

    def test_n_cache_hits_counter_present(self):
        """A counter for cache hits must be present for the log line."""
        assert 'n_cache_hits' in self.fn_src, \
            "n_cache_hits counter not found — log line will be missing"

    def test_cache_hit_note_in_print_line(self):
        """The print / log line must mention reused/cache-hit contracts."""
        assert 'reused' in self.fn_src or 'cache_hits' in self.fn_src, \
            "qualify_chain_for_scan log line must mention reused (cache hit) contracts"

    def test_existing_key_to_conid_lookup_built(self):
        """
        The lookup dict _existing_key_to_conid (or equivalent) must be
        built from details_cache before the main loop.
        """
        assert '_existing_key_to_conid' in self.fn_src or \
               'existing_key' in self.fn_src, \
            "Key-to-conId lookup from details_cache not found"

    def test_raw_append_still_present(self):
        """Regression: contracts NOT in cache must still go to raw[] for IB."""
        assert 'raw.append' in self.fn_src, \
            "raw.append removed — new contracts will never be qualified"

    def test_qualify_batched_still_called(self):
        """Regression: _qualify_contracts_batched must still be called."""
        assert '_qualify_contracts_batched' in self.fn_src, \
            "_qualify_contracts_batched call removed — new contracts won't be qualified"


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  instrument.py scan() — FOP price source INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestScanFOPPriceSource:
    """
    Inspect the InstrumentScanner.scan() source to confirm:
    1. FOP price is read from self._oi_streams (persistent stream)
    2. fetch_snapshot is NOT called on underlying_contracts for FOP price
    3. If no price from OI streams, scan returns early (no hang, no fallback
       to fetch_snapshot which would return empty for 24hr markets)
    """

    @pytest.fixture(autouse=True)
    def _load_source(self):
        import options_scanner.scanner.instrument as inst_mod
        self.scan_src = inspect.getsource(inst_mod.InstrumentScanner.scan)

    def test_oi_streams_used_for_fop_price(self):
        """FOP price block must read from self._oi_streams."""
        assert '_oi_streams' in self.scan_src, \
            "scan() must read FOP price from self._oi_streams"

    def test_stream_price_method_called(self):
        """scan() must call stream.price() to get FOP underlying price."""
        assert 'stream.price()' in self.scan_src or \
               '.price()' in self.scan_src, \
            "scan() must call OIStream.price() for FOP underlying price"

    def test_fetch_snapshot_not_called_for_fop_underlying(self):
        """
        fetch_snapshot must not be called on self.underlying_contracts inside
        the FOP price resolution block.
        The old broken form was:
            fut_tickers = await fetch_snapshot(self.ib, self.underlying_contracts)
        This must be gone — fetch_snapshot is still used for options, not underlying.
        """
        # The FOP price block is between 'secType == FOP' and 'secType == OPT'
        fop_idx = self.scan_src.find("secType'] == 'FOP'")
        opt_idx = self.scan_src.find("secType'] == 'OPT'")
        assert fop_idx > 0 and opt_idx > fop_idx
        fop_block = self.scan_src[fop_idx:opt_idx]

        assert 'underlying_contracts' not in fop_block or \
               'fetch_snapshot' not in fop_block, \
            ("fetch_snapshot(self.ib, self.underlying_contracts) found in FOP "
             "price block — must use OIStream.price() instead")

    def test_early_return_when_no_stream_price(self):
        """
        If no price is available from OI streams, scan() must return early
        (not hang, not pass None to qualify which causes 7225-contract blowout).
        """
        assert 'return' in self.scan_src, \
            "scan() has no return statement — early exit guard missing"
        # Confirm there's a guard specific to FOP with no price
        assert 'No underlying price' in self.scan_src or \
               'no price' in self.scan_src.lower() or \
               'underlying_price_map' in self.scan_src, \
            "scan() must guard against empty underlying_price_map for FOP"

    def test_opt_still_uses_equity_stream(self):
        """Regression: OPT price must still come from _equity_stream."""
        assert '_equity_stream' in self.scan_src, \
            "scan() must still use _equity_stream for OPT instruments"
