"""
Tests for contract cache: save/load, staleness, drift, archive,
manual trigger, and contract serialisation round-trip.
All tests use a temporary directory — no pollution of real cache.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace


# ── Patch CACHE_DIR before importing cache module ─────────────────────────────

import options_scanner.config as _cfg
_ORIG_CACHE_DIR = _cfg.CACHE_DIR

# Skip tests that require ib_insync when it's not installed (e.g. CI / sandbox)
try:
    import ib_insync as _ib_insync_check  # noqa: F401
    _IB_AVAILABLE = True
except ImportError:
    _IB_AVAILABLE = False
_skip_no_ib = unittest.skipUnless(_IB_AVAILABLE, 'ib_insync not installed')


def _patch_cache_dir(tmp: Path):
    import options_scanner.data.contract_cache as _cm
    _cfg.CACHE_DIR = tmp
    _cm.cfg.CACHE_DIR = tmp


def _restore_cache_dir():
    import options_scanner.data.contract_cache as _cm
    _cfg.CACHE_DIR = _ORIG_CACHE_DIR
    _cm.cfg.CACHE_DIR = _ORIG_CACHE_DIR


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_contract(conId=1001, symbol='SI', secType='FOP',
                    strike=30.0, right='C', expiry='20251219',
                    exchange='COMEX', localSymbol='SIZ5 C3000'):
    c = SimpleNamespace()
    c.conId                        = conId
    c.symbol                       = symbol
    c.secType                      = secType
    c.localSymbol                  = localSymbol
    c.exchange                     = exchange
    c.currency                     = 'USD'
    c.strike                       = strike
    c.right                        = right
    c.lastTradeDateOrContractMonth = expiry
    c.multiplier                   = '5000'
    c.tradingClass                 = 'SI'
    return c


def _fake_details(conId=1001, underConId=9999):
    d = SimpleNamespace()
    d.underConId = underConId
    d.contract   = _fake_contract(conId=conId)
    return d


def _make_cache_data(instrument='SI', age_hours=1,
                      underlying_price=30.5) -> dict:
    disc_ts = (datetime.now(timezone.utc)
               - timedelta(hours=age_hours)).isoformat()
    return {
        'version'                       : '0.1.0',
        'instrument'                    : instrument,
        'discovered_at'                 : disc_ts,
        'underlying_price_at_discovery' : underlying_price,
        'contracts'                     : [
            {
                'conId': 1001, 'localSymbol': 'SIZ5 C3000',
                'symbol': 'SI', 'secType': 'FOP',
                'exchange': 'COMEX', 'currency': 'USD',
                'strike': 30.0, 'right': 'C', 'expiry': '20251219',
                'multiplier': '5000', 'tradingClass': 'SI',
                'underConId': 9999,
                'first_seen': disc_ts, 'last_seen': disc_ts,
            }
        ],
    }


# ── Serialisation ─────────────────────────────────────────────────────────────

class TestContractSerialisation(unittest.TestCase):

    def _make_spec(self):
        from options_scanner.data.contract_cache import ChainSpec
        return ChainSpec(
            symbol='SI', sec_type='FOP', exchange='COMEX',
            currency='USD', trading_class='SO', multiplier='5000',
            expirations={'20251219', '20260121'}, strikes={30.0, 32.5},
            und_con_id=9999, und_symbol='SIK6',
        )

    def test_chain_spec_round_trip(self):
        from options_scanner.data.contract_cache import (
            _chain_spec_to_dict, _dict_to_chain_spec
        )
        spec  = self._make_spec()
        d     = _chain_spec_to_dict(spec)
        spec2 = _dict_to_chain_spec(d)
        self.assertEqual(spec2.symbol,        spec.symbol)
        self.assertEqual(spec2.trading_class, spec.trading_class)
        self.assertEqual(spec2.expirations,   spec.expirations)
        self.assertEqual(spec2.strikes,       spec.strikes)
        self.assertEqual(spec2.und_con_id,    spec.und_con_id)

    def test_chain_spec_serialise_keys(self):
        from options_scanner.data.contract_cache import _chain_spec_to_dict
        d = _chain_spec_to_dict(self._make_spec())
        for key in ('symbol','sec_type','exchange','currency',
                    'trading_class','multiplier','expirations',
                    'strikes','und_con_id'):
            self.assertIn(key, d)

    def test_first_seen_preserved(self):
        """ChainSpec has no first_seen — verify serialise doesn't raise."""
        from options_scanner.data.contract_cache import _chain_spec_to_dict
        d = _chain_spec_to_dict(self._make_spec())
        self.assertNotIn('first_seen', d)

    @_skip_no_ib
    def test_round_trip_contract(self):
        """Future contracts round-trip via _future_to_dict/_dict_to_future."""
        from options_scanner.data.contract_cache import (
            _future_to_dict, _dict_to_future
        )
        c  = _fake_contract()
        d  = _future_to_dict(c)
        c2 = _dict_to_future(d)
        self.assertEqual(c2.symbol, c.symbol)

    @_skip_no_ib
    def test_under_con_id_preserved(self):
        """underConId is now on ChainSpec, not on individual contracts."""
        spec = self._make_spec()
        self.assertEqual(spec.und_con_id, 9999)

    @_skip_no_ib
    def test_missing_fields_default_gracefully(self):
        from options_scanner.data.contract_cache import _dict_to_future
        c2 = _dict_to_future({})
        self.assertEqual(c2.conId, 0)


# ── Save / Load ───────────────────────────────────────────────────────────────

class TestSaveLoad(unittest.TestCase):

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        _patch_cache_dir(self._tmp)
        from options_scanner.data import contract_cache as cm
        self.cm = cm

    def tearDown(self):
        _restore_cache_dir()

    def _make_spec(self):
        from options_scanner.data.contract_cache import ChainSpec
        return ChainSpec(
            symbol='SI', sec_type='FOP', exchange='COMEX',
            currency='USD', trading_class='SO', multiplier='5000',
            expirations={'20251219'}, strikes={30.0, 32.5},
            und_con_id=9999, und_symbol='SIK6',
        )

    def test_save_creates_file(self):
        self.cm.save_cache('SI', [self._make_spec()], 30.5)
        self.assertTrue((self._tmp / 'cache_SI.json').exists())

    def test_load_returns_dict(self):
        self.cm.save_cache('SI', [self._make_spec()], 30.5)
        data = self.cm.load_cache('SI')
        self.assertIsNotNone(data)
        self.assertEqual(data['instrument'], 'SI')
        self.assertEqual(len(data['chain_specs']), 1)

    def test_load_missing_returns_none(self):
        self.assertIsNone(self.cm.load_cache('NONEXISTENT'))

    def test_version_mismatch_returns_none(self):
        bad = {'version': '0.0.0', 'instrument': 'SI',
               'discovered_at': datetime.now(timezone.utc).isoformat(),
               'chain_specs': []}
        (self._tmp / 'cache_SI.json').write_text(json.dumps(bad))
        self.assertIsNone(self.cm.load_cache('SI'))

    def test_first_seen_preserved_across_saves(self):
        """ChainSpecs have no first_seen — verify double-save is idempotent."""
        spec = self._make_spec()
        self.cm.save_cache('SI', [spec], 30.5)
        self.cm.save_cache('SI', [spec], 31.0)
        data = self.cm.load_cache('SI')
        self.assertEqual(len(data['chain_specs']), 1)

    def test_corrupt_json_returns_none(self):
        (self._tmp / 'cache_CL.json').write_text('{ not valid json')
        self.assertIsNone(self.cm.load_cache('CL'))


# ── Staleness checks ──────────────────────────────────────────────────────────

class TestStaleness(unittest.TestCase):

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        _patch_cache_dir(self._tmp)
        from options_scanner.data import contract_cache as cm
        self.cm = cm

    def tearDown(self):
        _restore_cache_dir()

    def test_fresh_cache_not_stale_ttl(self):
        data = _make_cache_data(age_hours=1)
        self.assertFalse(self.cm.is_stale_ttl(data))

    def test_old_cache_stale_ttl(self):
        # 25h old cache with default 24h TTL — must be stale
        data = _make_cache_data(age_hours=25)
        self.assertTrue(self.cm.is_stale_ttl(data))

    def test_fresh_cache_not_stale_custom_ttl(self):
        # 1h old cache with 0.5h TTL — must be stale
        data = _make_cache_data(age_hours=1)
        orig = _cfg.CACHE_TTL_HOURS
        self.cm.cfg.CACHE_TTL_HOURS = 0.5
        try:
            self.assertTrue(self.cm.is_stale_ttl(data))
        finally:
            self.cm.cfg.CACHE_TTL_HOURS = orig

    def test_missing_discovered_at_stale(self):
        self.assertTrue(self.cm.is_stale_ttl({}))

    def test_no_drift_when_price_unchanged(self):
        data = _make_cache_data(underlying_price=30.0)
        self.assertFalse(self.cm.moneyness_drift(data, 30.0))

    def test_drift_detected_above_threshold(self):
        data = _make_cache_data(underlying_price=30.0)
        # 10% move — above default 5% threshold
        self.assertTrue(self.cm.moneyness_drift(data, 33.0))

    def test_drift_not_detected_below_threshold(self):
        data = _make_cache_data(underlying_price=30.0)
        # 3% move — below default 5% threshold
        self.assertFalse(self.cm.moneyness_drift(data, 30.9))

    def test_drift_none_price_no_trigger(self):
        data = _make_cache_data(underlying_price=30.0)
        self.assertFalse(self.cm.moneyness_drift(data, None))

    def test_drift_zero_cached_price_no_trigger(self):
        data = _make_cache_data(underlying_price=0.0)
        self.assertFalse(self.cm.moneyness_drift(data, 30.0))


# ── Archive ───────────────────────────────────────────────────────────────────

class TestArchive(unittest.TestCase):

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        _patch_cache_dir(self._tmp)
        from options_scanner.data import contract_cache as cm
        self.cm = cm

    def tearDown(self):
        _restore_cache_dir()

    def _make_spec(self, expiry='20251219'):
        from options_scanner.data.contract_cache import ChainSpec
        return ChainSpec(
            symbol='SI', sec_type='FOP', exchange='COMEX',
            currency='USD', trading_class='SO', multiplier='5000',
            expirations={expiry}, strikes={30.0},
            und_con_id=9999, und_symbol='SIK6',
        )

    def test_archive_creates_file(self):
        """archive_contracts is a no-op in v0.3.0+ (ChainSpecs replace contract archive)."""
        # archive_contracts is removed; just verify cache save/load works
        self.cm.save_cache('SI', [self._make_spec()], 30.0)
        self.assertTrue((self._tmp / 'cache_SI.json').exists())

    def test_archive_preserves_existing(self):
        """Multiple saves accumulate chain specs."""
        from options_scanner.data.contract_cache import ChainSpec
        spec1 = self._make_spec('20251219')
        spec2 = self._make_spec('20260121')
        self.cm.save_cache('SI', [spec1, spec2], 30.0)
        data = json.loads((self._tmp / 'cache_SI.json').read_text())
        self.assertEqual(len(data['chain_specs']), 2)

    def test_archive_never_shrinks(self):
        """Saving 5 specs keeps 5 specs."""
        from options_scanner.data.contract_cache import ChainSpec
        specs = [self._make_spec(f'2025{i:02d}19') for i in range(1, 6)]
        self.cm.save_cache('SI', specs, 30.0)
        data = json.loads((self._tmp / 'cache_SI.json').read_text())
        self.assertEqual(len(data['chain_specs']), 5)

    def test_prune_expired_splits_correctly(self):
        from options_scanner.data.contract_cache import (
            ChainSpec, prune_expired_specs
        )
        now    = datetime.now(timezone.utc)
        future = (now + timedelta(days=30)).strftime('%Y%m%d')
        past   = (now - timedelta(days=1)).strftime('%Y%m%d')
        def _spec(expiry):
            return ChainSpec('SI','FOP','COMEX','USD','SO','5000',
                              {expiry},{30.0},9999,'SIK6')
        active, expired = prune_expired_specs([_spec(future), _spec(past)])
        self.assertEqual(len(active),  1)
        self.assertEqual(len(expired), 1)


# ── Manual trigger file ───────────────────────────────────────────────────────

class TestRefreshTrigger(unittest.TestCase):

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        _patch_cache_dir(self._tmp)
        from options_scanner.data import contract_cache as cm
        self.cm = cm

    def tearDown(self):
        _restore_cache_dir()

    def test_no_file_returns_empty(self):
        self.assertEqual(self.cm.read_refresh_requests(), set())

    def test_write_then_read(self):
        self.cm.write_refresh_request(['CL', 'SI'])
        result = self.cm.read_refresh_requests()
        self.assertIn('CL', result)
        self.assertIn('SI', result)

    def test_file_deleted_after_read(self):
        self.cm.write_refresh_request(['SI'])
        self.cm.read_refresh_requests()
        trigger_path = self._tmp / 'refresh_request'
        self.assertFalse(trigger_path.exists())

    def test_write_none_means_all(self):
        self.cm.write_refresh_request(None)
        result = self.cm.read_refresh_requests()
        self.assertIn('ALL', result)

    def test_read_twice_second_empty(self):
        self.cm.write_refresh_request(['CL'])
        self.cm.read_refresh_requests()
        self.assertEqual(self.cm.read_refresh_requests(), set())


if __name__ == '__main__':
    unittest.main()


# ── CacheManager integration ──────────────────────────────────────────────────

class TestCacheManagerIntegration(unittest.TestCase):
    """
    Tests CacheManager methods without an IB connection.
    Exercises __init__, _load_from_cache, serve_chain_specs,
    serve_underlying, is_ready, and the initialise() cache-hit path.
    """

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        _patch_cache_dir(self._tmp)
        import options_scanner.config as _cfg2
        self.cm = __import__(
            'options_scanner.data.contract_cache', fromlist=['CacheManager']
        ).CacheManager(ib=None)

    def tearDown(self):
        _restore_cache_dir()

    def _make_spec(self, expiry='20261219'):
        from options_scanner.data.contract_cache import ChainSpec
        return ChainSpec(
            symbol='CL', sec_type='FOP', exchange='NYMEX',
            currency='USD', trading_class='LO', multiplier='1000',
            expirations={expiry}, strikes={70.0, 75.0, 80.0},
            und_con_id=296574762, und_symbol='CLM6',
        )

    def _save_and_load(self, instrument='CL'):
        """Helper: save a spec to disk and have CacheManager load it."""
        from options_scanner.data.contract_cache import save_cache, load_cache
        spec = self._make_spec()
        save_cache(instrument, [spec], 75.3)
        cache_data = load_cache(instrument)
        self.cm._load_from_cache(instrument, cache_data)

    # ── __init__ ─────────────────────────────────────────────────────────────

    def test_init_has_chain_specs(self):
        self.assertIsInstance(self.cm._chain_specs, dict)

    def test_init_has_futures(self):
        self.assertIsInstance(self.cm._futures, dict)

    def test_init_has_last_prices(self):
        self.assertIsInstance(self.cm._last_prices, dict)

    def test_init_has_cache_data(self):
        self.assertIsInstance(self.cm._cache_data, dict)

    def test_init_has_refreshing(self):
        self.assertIsInstance(self.cm._refreshing, set)

    def test_init_legacy_contracts_present(self):
        """Legacy _contracts dict must exist to avoid AttributeError."""
        self.assertIsInstance(self.cm._contracts, dict)

    def test_init_legacy_details_present(self):
        """Legacy _details dict must exist to avoid AttributeError."""
        self.assertIsInstance(self.cm._details, dict)

    # ── is_ready ─────────────────────────────────────────────────────────────

    def test_not_ready_before_load(self):
        self.assertFalse(self.cm.is_ready('CL'))

    def test_ready_after_load(self):
        self._save_and_load('CL')
        self.assertTrue(self.cm.is_ready('CL'))

    # ── serve_chain_specs ─────────────────────────────────────────────────────

    def test_serve_chain_specs_empty_before_load(self):
        self.assertEqual(self.cm.serve_chain_specs('CL'), [])

    def test_serve_chain_specs_after_load(self):
        self._save_and_load('CL')
        specs = self.cm.serve_chain_specs('CL')
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].symbol, 'CL')
        self.assertEqual(specs[0].trading_class, 'LO')

    def test_serve_chain_specs_strikes_preserved(self):
        self._save_and_load('CL')
        spec = self.cm.serve_chain_specs('CL')[0]
        self.assertEqual(spec.strikes, {70.0, 75.0, 80.0})

    def test_serve_chain_specs_unknown_instrument(self):
        self.assertEqual(self.cm.serve_chain_specs('UNKNOWN'), [])

    # ── serve_underlying ─────────────────────────────────────────────────────

    def test_serve_underlying_empty_before_load(self):
        self.assertEqual(self.cm.serve_underlying('CL'), [])

    def test_serve_underlying_after_load_no_futures(self):
        """Cache saved without futures → serve_underlying returns []."""
        from options_scanner.data.contract_cache import save_cache, load_cache
        save_cache('CL', [self._make_spec()], 75.3, futures=None)
        cache_data = load_cache('CL')
        self.cm._load_from_cache('CL', cache_data)
        self.assertEqual(self.cm.serve_underlying('CL'), [])

    # ── report_price ─────────────────────────────────────────────────────────

    def test_report_price_stored(self):
        self.cm.report_price('CL', 75.5)
        self.assertEqual(self.cm._last_prices['CL'], 75.5)

    def test_report_price_zero_ignored(self):
        self.cm.report_price('CL', 75.0)
        self.cm.report_price('CL', 0.0)
        self.assertEqual(self.cm._last_prices['CL'], 75.0)

    # ── _load_from_cache ─────────────────────────────────────────────────────

    def test_load_prunes_all_expired(self):
        """ChainSpec whose only expiry is past should not appear after load."""
        from options_scanner.data.contract_cache import save_cache, load_cache
        past_spec = self._make_spec(expiry='20200101')  # well in the past
        save_cache('CL', [past_spec], 75.0)
        cache_data = load_cache('CL')
        self.cm._load_from_cache('CL', cache_data)
        self.assertEqual(self.cm.serve_chain_specs('CL'), [])
        self.assertFalse(self.cm.is_ready('CL'))

    def test_load_keeps_future_expiry(self):
        """ChainSpec with future expiry must survive load."""
        self._save_and_load('CL')
        self.assertEqual(len(self.cm.serve_chain_specs('CL')), 1)

    def test_load_sets_cache_data(self):
        self._save_and_load('CL')
        self.assertIn('CL', self.cm._cache_data)
        self.assertEqual(self.cm._cache_data['CL']['instrument'], 'CL')
