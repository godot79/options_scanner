"""
tests/test_stream_event_loop.py
--------------------------------
Tests for the ib.sleep() fix in OIStream.start() and EquityStream.start().

ROOT CAUSE (grounded in docs and ib_insync source):

  1. asyncio.sleep() does NOT pump the ib_insync message loop.
     Only ib.sleep() drains the IB socket buffer and dispatches incoming ticks.
     Source: ib_insync author erdewit, GitHub issue #229 —
       "time.sleep must be ib.sleep"

  2. All ib_insync Ticker fields initialise to float('nan'), NOT None or 0.
     Source: ib_insync Ticker dataclass definition (ib_insync/ticker.py).

  3. Default price ticks (bid=1, ask=2, last=4, close=9) are delivered on
     ANY non-snapshot reqMktData call with NO genericTickList entry needed.
     Source: IB TWS API tick_types.html — "Generic tick required" column = '-'
     for all four default price ticks.

  4. IB sends bid=-1, ask=-1 as sentinels outside market hours (not nan).
     Source: ib_insync issue #471.

CONSEQUENCE:
  OIStream.start() and EquityStream.start() both had `await asyncio.sleep(0.3)`
  in their wait loops.  IB delivered price ticks to the socket immediately
  for active 24hr futures (CL, SI), but the loop never processed them.
  Ticker fields stayed nan for the full 5-second deadline.
  price() returned None. First scan fired and skipped.

FIX:
  Replace `await asyncio.sleep(0.3)` with `await self._ib.sleep(0.3)`
  in both wait loops. No other changes.

Run: pytest tests/test_stream_event_loop.py -v
No IB connection required.
"""

import inspect
import math
import sys
import os
from types import SimpleNamespace
from typing import Optional

import pytest

_HERE   = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _p in (_PARENT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

nan = float('nan')


# ═══════════════════════════════════════════════════════════════════════════════
# Pure helpers — replicate production logic exactly for unit testing
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_mid(bid, ask) -> Optional[float]:
    """Replica of data/utils.py safe_mid — exact same logic."""
    b_ok = bid is not None and not math.isnan(float(bid)) and float(bid) > 0
    a_ok = ask is not None and not math.isnan(float(ask)) and float(ask) > 0
    if b_ok and a_ok:
        return (float(bid) + float(ask)) / 2.0
    if b_ok:
        return float(bid)
    if a_ok:
        return float(ask)
    return None


def _price_from_ticker(ticker) -> Optional[float]:
    """
    Replica of EquityStream.price() logic.
    Reads bid/ask mid, then last, then close. Rejects nan, None, <=0.
    """
    if ticker is None:
        return None
    bid   = getattr(ticker, 'bid',   nan)
    ask   = getattr(ticker, 'ask',   nan)
    last  = getattr(ticker, 'last',  nan)
    close = getattr(ticker, 'close', nan)

    p = _safe_mid(bid, ask)
    if p is not None and p > 0:
        return p
    for v in (last, close):
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isnan(f) and f > 0:
            return f
    return None


def _oi_from_ticker(ticker) -> Optional[float]:
    """Replica of OIStream.oi() logic."""
    if ticker is None:
        return None
    val = getattr(ticker, 'futuresOpenInterest', None)
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    if f >= 0:
        return f
    return None


def _ticker(**kwargs) -> SimpleNamespace:
    """
    Build a Ticker stub with all price fields defaulting to nan.
    Matches ib_insync Ticker dataclass initial state exactly.
    Source: ib_insync/ticker.py — `bid: float = nan` etc.
    """
    defaults = dict(bid=nan, ask=nan, last=nan, close=nan,
                    futuresOpenInterest=None)
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  SOURCE INSPECTION — ib.sleep() used in both wait loops
# ═══════════════════════════════════════════════════════════════════════════════

class TestIBSleepInWaitLoops:
    """
    Confirm both stream classes use self._ib.sleep() in their deadline wait
    loops, not asyncio.sleep() or time.sleep().

    asyncio.sleep() does not pump the ib_insync message loop.
    Confirmed: ib_insync author (erdewit), GitHub issue #229.
    """

    @pytest.fixture(autouse=True)
    def _load(self):
        import options_scanner.data.ib_client as mod
        src = inspect.getsource(mod)
        oi_start   = src.find('class OIStream')
        eq_start   = src.find('class EquityStream')
        tick_start = src.find('class TickStream')
        assert oi_start > 0,   "OIStream class not found"
        assert eq_start > 0,   "EquityStream class not found"
        assert tick_start > 0, "TickStream class not found"
        self.eq_src = src[eq_start:tick_start]
        self.oi_src = src[oi_start:]

    def _start_body(self, class_src: str) -> str:
        idx = class_src.find('async def start(self)')
        assert idx >= 0, "start() method not found"
        return class_src[idx: idx + 1200]

    # ── OIStream ──────────────────────────────────────────────────────────────

    def test_oi_stream_wait_loop_uses_ib_sleep(self):
        body = self._start_body(self.oi_src)
        assert 'self._ib.sleep' in body, \
            "OIStream.start() wait loop must use self._ib.sleep(0.3)"

    def test_oi_stream_wait_loop_has_no_asyncio_sleep(self):
        body = self._start_body(self.oi_src)
        while_idx = body.find('while time.time()')
        assert while_idx >= 0, "while loop not found in OIStream.start()"
        loop_body = body[while_idx: while_idx + 300]
        assert 'asyncio.sleep' not in loop_body, \
            ("OIStream.start() wait loop contains asyncio.sleep() — "
             "this does not pump the ib_insync message loop; use self._ib.sleep()")

    def test_oi_stream_start_has_no_time_sleep(self):
        body = self._start_body(self.oi_src)
        assert 'time.sleep' not in body, \
            "OIStream.start() must not use time.sleep() — blocks entire event loop"

    # ── EquityStream ──────────────────────────────────────────────────────────

    def test_equity_stream_wait_loop_uses_ib_sleep(self):
        body = self._start_body(self.eq_src)
        assert 'self._ib.sleep' in body, \
            "EquityStream.start() wait loop must use self._ib.sleep(0.3)"

    def test_equity_stream_wait_loop_has_no_asyncio_sleep(self):
        body = self._start_body(self.eq_src)
        while_idx = body.find('while time.time()')
        assert while_idx >= 0, "while loop not found in EquityStream.start()"
        loop_body = body[while_idx: while_idx + 300]
        assert 'asyncio.sleep' not in loop_body, \
            ("EquityStream.start() wait loop contains asyncio.sleep() — "
             "this does not pump the ib_insync message loop; use self._ib.sleep()")

    def test_equity_stream_start_has_no_time_sleep(self):
        body = self._start_body(self.eq_src)
        assert 'time.sleep' not in body, \
            "EquityStream.start() must not use time.sleep()"


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  TICKER NAN HANDLING — ib_insync initial state
# ═══════════════════════════════════════════════════════════════════════════════

class TestTickerNanHandling:
    """
    All ib_insync Ticker fields initialise to float('nan').
    Source: ib_insync/ticker.py Ticker dataclass — `bid: float = nan` etc.

    price() must return None when all fields are nan (no ticks yet),
    and return a price once any valid field is populated.
    """

    def test_all_nan_returns_none(self):
        """Startup state before any tick is received."""
        assert _price_from_ticker(_ticker()) is None

    def test_nan_bid_ask_falls_through_to_last(self):
        assert _price_from_ticker(_ticker(bid=nan, ask=nan, last=78.5)) == pytest.approx(78.5)

    def test_nan_bid_ask_last_falls_through_to_close(self):
        """close (tick 9) = previous session settlement, always sent by IB."""
        assert _price_from_ticker(_ticker(bid=nan, ask=nan, last=nan, close=77.9)) == pytest.approx(77.9)

    def test_all_nan_including_close_returns_none(self):
        assert _price_from_ticker(_ticker(bid=nan, ask=nan, last=nan, close=nan)) is None

    def test_none_ticker_returns_none(self):
        assert _price_from_ticker(None) is None

    # ── IB sentinel -1 for bid/ask outside market hours ──────────────────────
    # Source: ib_insync issue #471 — "bid and ask are both -1 (or nan)"

    def test_negative_one_bid_not_used_as_price(self):
        """IB sends bid=-1 outside market hours as a sentinel."""
        assert _price_from_ticker(_ticker(bid=-1.0, ask=-1.0, last=78.5)) == pytest.approx(78.5)

    def test_negative_one_bid_ask_falls_through_to_last(self):
        assert _price_from_ticker(_ticker(bid=-1.0, ask=-1.0, last=32.1)) == pytest.approx(32.1)

    def test_negative_one_last_falls_through_to_close(self):
        assert _price_from_ticker(_ticker(bid=-1.0, ask=-1.0, last=-1.0, close=31.9)) == pytest.approx(31.9)

    def test_all_sentinel_negative_one_returns_none(self):
        assert _price_from_ticker(_ticker(bid=-1.0, ask=-1.0, last=-1.0, close=-1.0)) is None

    # ── Zero values ───────────────────────────────────────────────────────────

    def test_zero_bid_not_used(self):
        assert _price_from_ticker(_ticker(bid=0.0, ask=nan, last=78.5)) == pytest.approx(78.5)

    def test_zero_last_not_used(self):
        assert _price_from_ticker(_ticker(bid=nan, ask=nan, last=0.0, close=77.0)) == pytest.approx(77.0)

    # ── Priority order: mid > last > close ────────────────────────────────────

    def test_mid_beats_last(self):
        assert _price_from_ticker(_ticker(bid=78.0, ask=78.5, last=50.0)) == pytest.approx(78.25)

    def test_last_beats_close(self):
        assert _price_from_ticker(_ticker(last=78.3, close=50.0)) == pytest.approx(78.3)

    def test_one_sided_bid_used(self):
        assert _price_from_ticker(_ticker(bid=78.0)) == pytest.approx(78.0)

    def test_one_sided_ask_used(self):
        assert _price_from_ticker(_ticker(ask=78.5)) == pytest.approx(78.5)

    # ── Realistic CL / SI values ──────────────────────────────────────────────

    def test_cl_active_session_mid(self):
        assert _price_from_ticker(_ticker(bid=78.34, ask=78.35)) == pytest.approx(78.345)

    def test_cl_no_bid_ask_uses_last(self):
        assert _price_from_ticker(_ticker(last=78.20)) == pytest.approx(78.20)

    def test_si_settlement_close_only(self):
        assert _price_from_ticker(_ticker(close=32.150)) == pytest.approx(32.150)


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  safe_mid NAN HANDLING
# ═══════════════════════════════════════════════════════════════════════════════

class TestSafeMidNanHandling:

    def test_nan_nan_returns_none(self):
        assert _safe_mid(nan, nan) is None

    def test_nan_ask_uses_bid(self):
        assert _safe_mid(78.0, nan) == pytest.approx(78.0)

    def test_nan_bid_uses_ask(self):
        assert _safe_mid(nan, 78.5) == pytest.approx(78.5)

    def test_valid_both_returns_mid(self):
        assert _safe_mid(78.0, 78.5) == pytest.approx(78.25)

    def test_none_none_returns_none(self):
        assert _safe_mid(None, None) is None

    def test_negative_one_bid_not_used(self):
        assert _safe_mid(-1.0, nan) is None

    def test_negative_one_ask_not_used(self):
        assert _safe_mid(nan, -1.0) is None

    def test_zero_bid_not_used(self):
        assert _safe_mid(0.0, nan) is None

    def test_zero_ask_not_used(self):
        assert _safe_mid(nan, 0.0) is None


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  OIStream.oi() — futuresOpenInterest field handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestOIStreamOiMethod:

    def test_none_ticker_returns_none(self):
        assert _oi_from_ticker(None) is None

    def test_none_futures_oi_returns_none(self):
        assert _oi_from_ticker(_ticker(futuresOpenInterest=None)) is None

    def test_nan_futures_oi_returns_none(self):
        assert _oi_from_ticker(_ticker(futuresOpenInterest=nan)) is None

    def test_zero_futures_oi_is_valid(self):
        assert _oi_from_ticker(_ticker(futuresOpenInterest=0.0)) == pytest.approx(0.0)

    def test_positive_futures_oi_returned(self):
        assert _oi_from_ticker(_ticker(futuresOpenInterest=312450.0)) == pytest.approx(312450.0)

    def test_negative_futures_oi_returns_none(self):
        assert _oi_from_ticker(_ticker(futuresOpenInterest=-1.0)) is None


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  SOURCE INSPECTION — OIStream structure unchanged by the fix
# ═══════════════════════════════════════════════════════════════════════════════

class TestOIStreamStructure:
    """
    Confirm OIStream retains correct stream parameters after the fix.
    The fix must not change anything except asyncio.sleep -> self._ib.sleep.
    """

    @pytest.fixture(autouse=True)
    def _load(self):
        import options_scanner.data.ib_client as mod
        src = inspect.getsource(mod)
        start = src.find('class OIStream')
        assert start > 0
        self.oi_src = src[start:]

    def test_uses_generic_tick_588(self):
        assert "'588'" in self.oi_src

    def test_uses_snapshot_false(self):
        assert 'snapshot=False' in self.oi_src

    def test_oi_reads_futures_open_interest(self):
        oi_start = self.oi_src.find('def oi(self)')
        body = self.oi_src[oi_start: oi_start + 300]
        assert 'futuresOpenInterest' in body

    def test_does_not_read_deprecated_open_interest_attribute(self):
        import re
        bad = re.findall(r"getattr\([^)]+['\"]openInterest['\"]", self.oi_src)
        assert bad == [], "OIStream must not read .openInterest (deprecated tick 22)"

    def test_does_not_use_generic_tick_101(self):
        import re
        bad = re.findall(r"genericTickList\s*=\s*['\"].*?101.*?['\"]", self.oi_src)
        assert bad == [], "OIStream must not use genericTickList='101'"
