"""Tests for data utils: year_fraction, parse_expiry_date, safe_mid."""
import unittest
from datetime import datetime, timezone
from options_scanner.data.utils import year_fraction, parse_expiry_date, safe_mid

NOW = datetime(2025,1,1,0,0,0,tzinfo=timezone.utc)

class TestYearFraction(unittest.TestCase):
    def test_one_year_ahead(self):
        self.assertAlmostEqual(year_fraction('20260101',NOW),1.0,delta=0.003)
    def test_six_months(self):
        T=year_fraction('20250701',NOW); self.assertGreater(T,0.45); self.assertLess(T,0.55)
    def test_yyyymm_accepted(self):
        self.assertGreater(year_fraction('202601',NOW),0)
    def test_expired_zero(self):
        self.assertEqual(year_fraction('20240101',NOW),0.0)
    def test_same_day_zero(self):
        self.assertEqual(year_fraction('20250101',NOW),0.0)
    def test_invalid_zero(self):
        self.assertEqual(year_fraction('INVALID',NOW),0.0)
    def test_always_nonneg(self):
        self.assertGreaterEqual(year_fraction('20000101',NOW),0.0)

class TestParseExpiryDate(unittest.TestCase):
    def test_yyyymmdd(self):
        dt=parse_expiry_date('20250620')
        self.assertIsNotNone(dt); self.assertEqual(dt.year,2025); self.assertEqual(dt.month,6); self.assertEqual(dt.day,20)
    def test_yyyymm(self):
        dt=parse_expiry_date('202506')
        self.assertIsNotNone(dt); self.assertEqual(dt.year,2025); self.assertEqual(dt.month,6)
    def test_invalid_none(self):
        self.assertIsNone(parse_expiry_date('GARBAGE'))
        self.assertIsNone(parse_expiry_date(''))
        self.assertIsNone(parse_expiry_date('99'))
    def test_prefix_used(self):
        dt=parse_expiry_date('20250620extra'); self.assertIsNotNone(dt); self.assertEqual(dt.day,20)

class TestSafeMid(unittest.TestCase):
    def test_both_valid(self):
        self.assertAlmostEqual(safe_mid(1.0,1.4),1.2)
    def test_only_bid(self):
        self.assertEqual(safe_mid(1.5,None),1.5); self.assertEqual(safe_mid(1.5,0.0),1.5)
    def test_only_ask(self):
        self.assertEqual(safe_mid(None,2.0),2.0); self.assertEqual(safe_mid(0.0,2.0),2.0)
    def test_both_none(self):
        self.assertIsNone(safe_mid(None,None))
    def test_both_zero(self):
        self.assertIsNone(safe_mid(0.0,0.0))
    def test_crossed_market(self):
        self.assertAlmostEqual(safe_mid(2.0,1.0),1.5)
    def test_negative_bid_ignored(self):
        self.assertEqual(safe_mid(-1.0,2.0),2.0)
    def test_negative_ask_ignored(self):
        self.assertEqual(safe_mid(1.5,-1.0),1.5)

if __name__=='__main__': unittest.main()
