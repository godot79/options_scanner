"""Tests for CSVLogger — uses tmp_path via tempfile."""
import os, unittest, tempfile, pandas as pd
from types import SimpleNamespace
from options_scanner.signals.fingerprint.base import Finding

I = 'TSLA'

def _sig(composite='BULLISH', pc=0.75):
    return SimpleNamespace(composite=composite,pc_ratio=pc,call_vol=1000.,put_vol=750.,
        factors={'pc_ratio':'bullish'})

def _df():
    return pd.DataFrame({'localSymbol':['A','B'],'expiry':['20230120']*2,'right':['C','P'],
        'strike':[200.,200.],'bid':[5.,4.5],'ask':[5.2,4.7],'volume':[300,150],
        'openInterest':[2000,1500],'liquidity_score':[0.75,0.60],'iv':[0.32,0.35]})

def _finding():
    return Finding(confidence=0.75,source='ibkr_snapshot',instrument=I,
        model='sweep_detector',finding_type='sweep',note='test')


class TestCSVLogger(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        # Patch LOG_DIR in both config and csv_logger modules
        import options_scanner.config as cfg_mod
        import options_scanner.io.csv_logger as log_mod
        from pathlib import Path
        self._orig_cfg = cfg_mod.LOG_DIR
        self._orig_log = log_mod.LOG_DIR
        cfg_mod.LOG_DIR = Path(self._tmpdir)
        log_mod.LOG_DIR = Path(self._tmpdir)
        from options_scanner.io.csv_logger import CSVLogger
        self.logger = CSVLogger()

    def tearDown(self):
        import options_scanner.config as cfg_mod
        import options_scanner.io.csv_logger as log_mod
        cfg_mod.LOG_DIR = self._orig_cfg
        log_mod.LOG_DIR = self._orig_log

    def _data_files(self):
        return [f for f in os.listdir(self._tmpdir)
                if f.startswith(I) and f.endswith('.csv') and '_alerts' not in f]

    def _alert_files(self):
        return [f for f in os.listdir(self._tmpdir)
                if f.startswith(I) and f.endswith('_alerts.csv')]

    # ── scan write ────────────────────────────────────────────────────────────

    def test_creates_file(self):
        self.logger.write_scan(I, _df(), _sig())
        self.assertEqual(len(self._data_files()), 1)

    def test_file_has_rows(self):
        self.logger.write_scan(I, _df(), _sig())
        df = pd.read_csv(os.path.join(self._tmpdir, self._data_files()[0]))
        self.assertEqual(len(df), 2)

    def test_instrument_column(self):
        self.logger.write_scan(I, _df(), _sig())
        df = pd.read_csv(os.path.join(self._tmpdir, self._data_files()[0]))
        self.assertIn('instrument', df.columns)
        self.assertTrue((df['instrument'] == I).all())

    def test_signal_composite_written(self):
        self.logger.write_scan(I, _df(), _sig('BULLISH'))
        df = pd.read_csv(os.path.join(self._tmpdir, self._data_files()[0]))
        self.assertTrue((df['signal_composite'] == 'BULLISH').all())

    def test_appends_on_second_call(self):
        self.logger.write_scan(I, _df(), _sig())
        self.logger.write_scan(I, _df(), _sig())
        df = pd.read_csv(os.path.join(self._tmpdir, self._data_files()[0]))
        self.assertEqual(len(df), 4)

    def test_header_written_once(self):
        self.logger.write_scan(I, _df(), _sig())
        self.logger.write_scan(I, _df(), _sig())
        path    = os.path.join(self._tmpdir, self._data_files()[0])
        with open(path) as _fh:
            content = _fh.read()
        # Count lines that start with known header tokens
        header_lines = [l for l in content.splitlines() if 'localSymbol' in l]
        self.assertEqual(len(header_lines), 1)

    def test_empty_df_no_crash(self):
        self.logger.write_scan(I, pd.DataFrame(), _sig())

    def test_underlying_price_written(self):
        self.logger.write_scan(I, _df(), _sig(), underlying_price=185.5)
        df = pd.read_csv(os.path.join(self._tmpdir, self._data_files()[0]))
        self.assertIn('underlying_price', df.columns)
        self.assertAlmostEqual(df['underlying_price'].iloc[0], 185.5, delta=0.01)

    # ── alert write ───────────────────────────────────────────────────────────

    def test_signal_alert_creates_alert_file(self):
        self.logger.write_signal_alert(I, 'STRONG_BEARISH', _sig(), 'msg')
        self.assertEqual(len(self._alert_files()), 1)

    def test_alert_file_has_alert_type(self):
        self.logger.write_signal_alert(I, 'BULLISH', _sig(), 'msg')
        df = pd.read_csv(os.path.join(self._tmpdir, self._alert_files()[0]))
        self.assertEqual(df['alert_type'].iloc[0], 'SIGNAL')

    def test_fingerprint_alert_written(self):
        self.logger.write_fingerprint_alert(I, _finding(), 'msg')
        df = pd.read_csv(os.path.join(self._tmpdir, self._alert_files()[0]))
        self.assertEqual(df['alert_type'].iloc[0], 'FINGERPRINT')
        self.assertEqual(df['subtype'].iloc[0], 'sweep')

    def test_data_and_alert_different_files(self):
        self.logger.write_scan(I, _df(), _sig())
        self.logger.write_signal_alert(I, 'BULLISH', _sig(), 'msg')
        data_names  = set(self._data_files())
        alert_names = set(self._alert_files())
        # Alert files must not be the same as data files
        self.assertTrue(data_names.isdisjoint(alert_names))

    # ── multi-instrument ──────────────────────────────────────────────────────

    def test_different_instruments_different_files(self):
        self.logger.write_scan('CL',   _df(), _sig())
        self.logger.write_scan('TSLA', _df(), _sig())
        cl_files   = [f for f in os.listdir(self._tmpdir) if f.startswith('CL_')]
        tsla_files = [f for f in os.listdir(self._tmpdir) if f.startswith('TSLA_')]
        self.assertGreater(len(cl_files),   0)
        self.assertGreater(len(tsla_files), 0)
        self.assertTrue(set(cl_files).isdisjoint(set(tsla_files)))

if __name__ == '__main__': unittest.main()
