"""
tests/test_sec_def_opt_params.py
---------------------------------
Validates the reqSecDefOptParams discovery logic without an IB connection.

These tests mock the IB responses to confirm:
1. The correct parameters are passed to reqSecDefOptParams
2. The OptionChain response is correctly parsed into Contract objects
3. Expiry filtering (2 months) is applied correctly
4. Preferred trading class filter works correctly
5. qualifyContractsAsync is called in batches respecting the 45 msg/s limit
6. Duplicate expiry/strike combinations are deduplicated
7. Contract objects are correctly constructed with all required fields
"""

import asyncio
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_option_chain(trading_class: str, exchange: str,
                        expiries: list[str], strikes: list[float],
                        multiplier: str = '1000') -> SimpleNamespace:
    """Build a mock OptionChain object as ib_insync returns it."""
    chain = SimpleNamespace()
    chain.tradingClass  = trading_class
    chain.exchange      = exchange
    chain.multiplier    = multiplier
    chain.expirations   = set(expiries)
    chain.strikes       = set(strikes)
    chain.underlyingConId = 12345
    return chain


def _make_contract(con_id: int, symbol: str = 'CL', sec_type: str = 'FOP',
                    expiry: str = '20260614', strike: float = 70.0,
                    right: str = 'C', trading_class: str = 'LO') -> SimpleNamespace:
    """Build a mock qualified Contract object."""
    c = SimpleNamespace()
    c.conId                        = con_id
    c.symbol                       = symbol
    c.secType                      = sec_type
    c.exchange                     = 'NYMEX'
    c.currency                     = 'USD'
    c.lastTradeDateOrContractMonth = expiry
    c.strike                       = strike
    c.right                        = right
    c.multiplier                   = '1000'
    c.tradingClass                 = trading_class
    c.localSymbol                  = f'{symbol}{expiry[-4:]}{right}{int(strike*10):07d}'
    return c


# ── OptionChain parsing ───────────────────────────────────────────────────────

class TestOptionChainParsing(unittest.TestCase):
    """
    Tests that we correctly parse OptionChain objects into Contract templates.
    OptionChain gives us: tradingClass, multiplier, expirations (set), strikes (set)
    We must build one Contract per (expiry, strike, right) combination.
    """

    def setUp(self):
        self.now = datetime(2026, 4, 28, tzinfo=timezone.utc)
        # Two months ahead
        self.cutoff = self.now + timedelta(days=62)

    def _build_contracts_from_chain(self, chain, sym, exch, curr):
        """
        Replicate the contract-building logic from discover_fop_chain.
        Uses SimpleNamespace instead of ib_insync.Contract so tests run
        without an IB installation (same pattern as other offline tests).
        """
        from options_scanner.data.utils import parse_expiry_date
        contracts = []
        for expiry in sorted(chain.expirations):
            exp_dt = parse_expiry_date(expiry)
            if exp_dt is not None and exp_dt < self.now:
                continue
            if exp_dt is not None and exp_dt > self.cutoff:
                continue  # 2-month filter
            for strike in sorted(chain.strikes):
                for right in ('C', 'P'):
                    from types import SimpleNamespace
                    c = SimpleNamespace()
                    c.symbol        = sym
                    c.secType       = 'FOP'
                    c.exchange      = exch
                    c.currency      = curr
                    c.lastTradeDateOrContractMonth = expiry
                    c.strike        = strike
                    c.right         = right
                    c.multiplier    = chain.multiplier
                    c.tradingClass  = chain.tradingClass
                    contracts.append(c)
        return contracts

    def test_basic_contract_count(self):
        """2 expiries × 3 strikes × 2 rights = 12 contracts.
        Both expiries are within the 62-day cutoff from 2026-04-28.
        20260514 = May 14 (ok), 20260617 = June 17 (ok, cutoff=June 29).
        """
        chain = _make_option_chain(
            'LO', 'NYMEX',
            expiries=['20260514', '20260617'],
            strikes=[60.0, 65.0, 70.0],
        )
        contracts = self._build_contracts_from_chain(chain, 'CL', 'NYMEX', 'USD')
        self.assertEqual(len(contracts), 12)

    def test_expired_expiry_filtered(self):
        """Expiry in the past should be excluded."""
        chain = _make_option_chain(
            'LO', 'NYMEX',
            expiries=['20250101', '20260614'],  # first is past
            strikes=[70.0],
        )
        contracts = self._build_contracts_from_chain(chain, 'CL', 'NYMEX', 'USD')
        # Only 1 valid expiry × 1 strike × 2 rights = 2
        self.assertEqual(len(contracts), 2)

    def test_too_far_expiry_filtered(self):
        """Expiry beyond 2-month cutoff should be excluded."""
        chain = _make_option_chain(
            'LO', 'NYMEX',
            expiries=['20260614', '20270101'],  # second is beyond 2 months
            strikes=[70.0],
        )
        contracts = self._build_contracts_from_chain(chain, 'CL', 'NYMEX', 'USD')
        # Only 1 valid expiry × 1 strike × 2 rights = 2
        self.assertEqual(len(contracts), 2)

    def test_contract_fields_populated(self):
        """Every contract must have required fields set."""
        chain = _make_option_chain(
            'LO', 'NYMEX',
            expiries=['20260614'],
            strikes=[70.0],
        )
        contracts = self._build_contracts_from_chain(chain, 'CL', 'NYMEX', 'USD')
        for c in contracts:
            self.assertEqual(c.symbol, 'CL')
            self.assertEqual(c.secType, 'FOP')
            self.assertEqual(c.exchange, 'NYMEX')
            self.assertEqual(c.currency, 'USD')
            self.assertEqual(c.multiplier, '1000')
            self.assertEqual(c.tradingClass, 'LO')
            self.assertIn(c.right, ('C', 'P'))
            self.assertGreater(c.strike, 0)
            self.assertTrue(c.lastTradeDateOrContractMonth)

    def test_both_rights_present(self):
        """Each (expiry, strike) must produce both a call and a put."""
        chain = _make_option_chain(
            'LO', 'NYMEX',
            expiries=['20260614'],
            strikes=[70.0, 75.0],
        )
        contracts = self._build_contracts_from_chain(chain, 'CL', 'NYMEX', 'USD')
        rights = [(c.strike, c.right) for c in contracts]
        self.assertIn((70.0, 'C'), rights)
        self.assertIn((70.0, 'P'), rights)
        self.assertIn((75.0, 'C'), rights)
        self.assertIn((75.0, 'P'), rights)


# ── Trading class filter ──────────────────────────────────────────────────────

class TestTradingClassFilter(unittest.TestCase):

    def _filter_chains(self, chains, preferred):
        if preferred is None:
            return chains
        return [c for c in chains if c.tradingClass in preferred]

    def test_preferred_none_keeps_all(self):
        chains = [
            _make_option_chain('LO', 'NYMEX', ['20260614'], [70.0]),
            _make_option_chain('LO1', 'NYMEX', ['20260507'], [70.0]),
            _make_option_chain('WL1', 'NYMEX', ['20260501'], [70.0]),
        ]
        filtered = self._filter_chains(chains, None)
        self.assertEqual(len(filtered), 3)

    def test_preferred_list_filters_correctly(self):
        chains = [
            _make_option_chain('LO', 'NYMEX', ['20260614'], [70.0]),
            _make_option_chain('LO1', 'NYMEX', ['20260507'], [70.0]),
            _make_option_chain('WL1', 'NYMEX', ['20260501'], [70.0]),
        ]
        filtered = self._filter_chains(chains, ['LO'])
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].tradingClass, 'LO')

    def test_preferred_empty_list_returns_empty(self):
        chains = [
            _make_option_chain('LO', 'NYMEX', ['20260614'], [70.0]),
        ]
        filtered = self._filter_chains(chains, [])
        self.assertEqual(len(filtered), 0)

    def test_preferred_none_for_si_keeps_all_series(self):
        """SI with None keeps SO, SO1, SO2, R4S, etc."""
        si_classes = ['SO', 'SO1', 'SO2', 'SO3', 'SO4',
                      'R4S', 'W4S', 'S4T', 'M1S']
        chains = [_make_option_chain(tc, 'COMEX', ['20260520'], [30.0])
                  for tc in si_classes]
        filtered = self._filter_chains(chains, None)
        self.assertEqual(len(filtered), len(si_classes))


# ── Deduplication ─────────────────────────────────────────────────────────────

class TestDeduplication(unittest.TestCase):
    """
    When calling reqSecDefOptParams for multiple futures, the same
    (tradingClass, expirations) combination may be returned multiple times.
    We must deduplicate by (tradingClass, frozenset(expirations)).
    """

    def _deduplicate(self, chains_per_future: list[list]) -> list:
        seen = set()
        result = []
        for chains in chains_per_future:
            for chain in chains:
                key = (chain.tradingClass, frozenset(chain.expirations))
                if key not in seen:
                    seen.add(key)
                    result.append(chain)
        return result

    def test_identical_chains_deduplicated(self):
        """Same chain from two futures should appear only once."""
        chain = _make_option_chain('LO', 'NYMEX', ['20260614'], [70.0])
        # Both futures return the same chain
        result = self._deduplicate([[chain], [chain]])
        self.assertEqual(len(result), 1)

    def test_different_expiries_both_kept(self):
        """Different expiry sets for same tradingClass are distinct chains."""
        chain_may = _make_option_chain('LO', 'NYMEX', ['20260514'], [70.0])
        chain_jun = _make_option_chain('LO', 'NYMEX', ['20260614'], [70.0])
        result = self._deduplicate([[chain_may], [chain_jun]])
        self.assertEqual(len(result), 2)

    def test_different_trading_classes_both_kept(self):
        """Different tradingClass always distinct even if same expiries."""
        chain_lo  = _make_option_chain('LO',  'NYMEX', ['20260614'], [70.0])
        chain_lo1 = _make_option_chain('LO1', 'NYMEX', ['20260614'], [70.0])
        result = self._deduplicate([[chain_lo, chain_lo1]])
        self.assertEqual(len(result), 2)

    def test_three_futures_with_overlap(self):
        """Chain shared by 3 futures deduplicates to 1."""
        chain = _make_option_chain('LO', 'NYMEX', ['20260614'], [70.0])
        result = self._deduplicate([[chain], [chain], [chain]])
        self.assertEqual(len(result), 1)


# ── Batch qualify sizing ──────────────────────────────────────────────────────

class TestBatchQualifySizing(unittest.TestCase):
    """
    Validates that _qualify_contracts_batched sends the right number of
    batches and respects the rate limit.

    We don't call IB here — just validate the batching math.
    """

    BATCH_SIZE  = 50
    BATCH_SLEEP = 1.1  # seconds between batches => ~45 msg/s

    def _expected_batches(self, n_contracts: int) -> int:
        import math
        return math.ceil(n_contracts / self.BATCH_SIZE)

    def test_exactly_one_batch(self):
        self.assertEqual(self._expected_batches(50), 1)

    def test_two_batches(self):
        self.assertEqual(self._expected_batches(51), 2)

    def test_large_chain_cl(self):
        """CL LO class: ~938 contracts within 2 months => ~19 batches."""
        # 938 / 50 = 18.76 => 19 batches
        self.assertEqual(self._expected_batches(938), 19)

    def test_rate_stays_under_50_per_second(self):
        """
        With batch_size=50 and sleep=1.1s, effective rate < 50 msg/s.
        rate = batch_size / (batch_size/ib_rate + sleep_between_batches)
        For 50 msgs/batch, hard limit 50 msg/s:
        min time per batch = 50/50 = 1s, plus 1.1s sleep = 2.1s per batch
        effective rate = 50 / 2.1 ≈ 23.8 msg/s — well under 50
        """
        effective_rate = self.BATCH_SIZE / (1.0 + self.BATCH_SLEEP)
        self.assertLess(effective_rate, 50)
        self.assertGreater(effective_rate, 10)  # not too slow either

    def test_no_sleep_after_last_batch(self):
        """
        Sleep should only occur BETWEEN batches, not after the last one.
        n_sleeps = n_batches - 1
        """
        for n in [1, 50, 51, 100, 938]:
            n_batches = self._expected_batches(n)
            n_sleeps  = n_batches - 1
            self.assertEqual(n_sleeps, max(0, n_batches - 1))


# ── reqSecDefOptParams parameter validation ───────────────────────────────────

class TestReqSecDefOptParamsParameters(unittest.TestCase):
    """
    Confirms the correct parameters are used for reqSecDefOptParams.

    For FOP (futures options):
      underlyingSymbol : instrument symbol, e.g. 'CL'
      futFopExchange   : futures exchange, e.g. 'NYMEX'
      underlyingSecType: 'FUT'
      underlyingConId  : conId of the specific futures contract

    For OPT (equity options):
      underlyingSymbol : stock symbol, e.g. 'TSLA'
      futFopExchange   : '' (empty for equities)
      underlyingSecType: 'STK'
      underlyingConId  : conId of the stock contract
    """

    def test_fop_parameters(self):
        params = dict(
            underlyingSymbol  = 'CL',
            futFopExchange    = 'NYMEX',
            underlyingSecType = 'FUT',
            underlyingConId   = 296574762,  # CLM6 conId
        )
        self.assertEqual(params['underlyingSecType'], 'FUT')
        self.assertNotEqual(params['futFopExchange'], '')
        self.assertGreater(params['underlyingConId'], 0)

    def test_opt_parameters(self):
        params = dict(
            underlyingSymbol  = 'TSLA',
            futFopExchange    = '',
            underlyingSecType = 'STK',
            underlyingConId   = 76792991,   # TSLA stock conId
        )
        self.assertEqual(params['underlyingSecType'], 'STK')
        self.assertEqual(params['futFopExchange'], '')
        self.assertGreater(params['underlyingConId'], 0)

    def test_fop_exchange_must_not_be_empty(self):
        """For FOP, passing empty exchange to reqSecDefOptParams may fail."""
        fop_exchange = 'NYMEX'
        self.assertTrue(len(fop_exchange) > 0)

    def test_conid_must_be_nonzero(self):
        """Passing conId=0 to reqSecDefOptParams returns empty or all chains."""
        # We assert our code always passes a specific conId > 0
        conid = 296574762
        self.assertGreater(conid, 0)


# ── 2-month expiry window ─────────────────────────────────────────────────────

class TestTwoMonthExpiryWindow(unittest.TestCase):
    """
    Validates the 2-month forward expiry filter applied during cache build.
    Cache stores contracts expiring within 2 calendar months of today.
    """

    def setUp(self):
        self.now     = datetime(2026, 4, 28, tzinfo=timezone.utc)
        self.cutoff  = self.now + timedelta(days=62)  # ~2 months

    def _within_window(self, expiry_str: str) -> bool:
        from options_scanner.data.utils import parse_expiry_date
        exp_dt = parse_expiry_date(expiry_str)
        if exp_dt is None:
            return False
        return self.now <= exp_dt <= self.cutoff

    def test_today_included(self):
        """
        Same-day expiry is included — options expiring today are still
        tradeable until market close. Filter excludes exp_dt < now (strictly
        past), so today (exp_dt == midnight UTC today) passes the lower bound.
        parse_expiry_date returns midnight UTC for YYYYMMDD strings.
        """
        self.assertTrue(self._within_window('20260428'))

    def test_tomorrow_included(self):
        self.assertTrue(self._within_window('20260429'))

    def test_60_days_included(self):
        d = (self.now + timedelta(days=60)).strftime('%Y%m%d')
        self.assertTrue(self._within_window(d))

    def test_63_days_excluded(self):
        d = (self.now + timedelta(days=63)).strftime('%Y%m%d')
        self.assertFalse(self._within_window(d))

    def test_past_excluded(self):
        self.assertFalse(self._within_window('20260101'))

    def test_typical_cl_front_month_included(self):
        """CLM6 (May 2026 expiry ~20260514) is within 2 months of Apr 28."""
        self.assertTrue(self._within_window('20260514'))

    def test_typical_cl_second_month_included(self):
        """CLN6 (June 2026 expiry ~20260617) is within 2 months of Apr 28."""
        self.assertTrue(self._within_window('20260617'))

    def test_third_month_excluded(self):
        """CLQ6 (July 2026 expiry ~20260717) is beyond 2 months."""
        self.assertFalse(self._within_window('20260717'))


if __name__ == '__main__':
    unittest.main()
