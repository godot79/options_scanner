"""Tests for AlertManager."""
import unittest
from options_scanner.io.alerts import AlertManager
from options_scanner.config import ALERT_SUPPRESSION_SCANS

I = 'CL'

class TestSuppression(unittest.TestCase):
    def setUp(self): self.m = AlertManager()

    def test_fires_first_call(self):
        self.m.tick(I); self.assertTrue(self.m.should_fire(I,'sig'))

    def test_suppressed_after_mark(self):
        self.m.tick(I); self.m.mark_fired(I,'sig')
        self.assertFalse(self.m.should_fire(I,'sig'))

    def test_fires_after_window(self):
        self.m.tick(I); self.m.mark_fired(I,'sig')
        for _ in range(ALERT_SUPPRESSION_SCANS+1): self.m.tick(I)
        self.assertTrue(self.m.should_fire(I,'sig'))

    def test_not_yet_past_window(self):
        self.m.tick(I); self.m.mark_fired(I,'sig')
        for _ in range(ALERT_SUPPRESSION_SCANS-1): self.m.tick(I)
        self.assertFalse(self.m.should_fire(I,'sig'))

    def test_different_keys_independent(self):
        self.m.tick(I); self.m.mark_fired(I,'sig_a')
        self.assertTrue(self.m.should_fire(I,'sig_b'))

    def test_different_instruments_independent(self):
        self.m.tick('SI'); self.m.tick(I); self.m.mark_fired('SI','sig')
        self.assertTrue(self.m.should_fire(I,'sig'))

    def test_clear_resets(self):
        self.m.tick(I); self.m.mark_fired(I,'sig')
        self.m.clear(I,'sig'); self.assertTrue(self.m.should_fire(I,'sig'))

    def test_clear_all(self):
        self.m.tick(I)
        for k in ('a','b','c'): self.m.mark_fired(I,k)
        self.m.clear_all(I)
        for k in ('a','b','c'): self.assertTrue(self.m.should_fire(I,k))

    def test_clear_nonexistent_no_error(self):
        self.m.clear(I,'nonexistent')  # must not raise

    def test_tick_increments(self):
        self.assertEqual(self.m._scan_n(I),0)
        self.m.tick(I); self.assertEqual(self.m._scan_n(I),1)
        self.m.tick(I); self.assertEqual(self.m._scan_n(I),2)

    def test_fire_before_any_tick(self):
        self.assertTrue(self.m.should_fire(I,'k'))


def _sig(composite='STRONG_BEARISH',pc=1.8):
    from types import SimpleNamespace
    return SimpleNamespace(composite=composite,pc_ratio=pc,call_vol=200.,put_vol=360.,
        factors={'pc_ratio':'bearish','iv_skew':'bearish','delta_weighted_pc':'bearish','volume_anomaly':'bearish'})

def _finding():
    from options_scanner.signals.fingerprint.base import Finding
    return Finding(confidence=0.82,source='ibkr_snapshot',instrument=I,model='sweep_detector',
        finding_type='sweep',note='Test',expiry='20250620',strike=32.5,right='P')


class TestFormatting(unittest.TestCase):
    def test_signal_contains_instrument(self):
        self.assertIn(I, AlertManager.format_signal_alert(I,'STRONG_BEARISH',_sig()))
    def test_signal_contains_composite(self):
        self.assertIn('STRONG_BEARISH', AlertManager.format_signal_alert(I,'STRONG_BEARISH',_sig()))
    def test_signal_contains_pc(self):
        self.assertIn('1.80', AlertManager.format_signal_alert(I,'STRONG_BEARISH',_sig()))
    def test_fp_contains_instrument(self):
        self.assertIn(I, AlertManager.format_fingerprint_alert(_finding()))
    def test_fp_contains_type(self):
        self.assertIn('sweep', AlertManager.format_fingerprint_alert(_finding()))
    def test_fp_contains_confidence(self):
        self.assertIn('0.82', AlertManager.format_fingerprint_alert(_finding()))
    def test_fp_contains_expiry(self):
        self.assertIn('20250620', AlertManager.format_fingerprint_alert(_finding()))
    def test_signal_none_pc_shows_na(self):
        from types import SimpleNamespace
        sig=SimpleNamespace(composite='BEARISH',pc_ratio=None,call_vol=100.,put_vol=200.,
            factors={'pc_ratio':'bearish','iv_skew':'bearish','delta_weighted_pc':None,'volume_anomaly':'bearish'})
        self.assertIn('N/A', AlertManager.format_signal_alert(I,'BEARISH',sig))

if __name__ == '__main__': unittest.main()
