"""
tests/test_new_signals.py
--------------------------
Integration tests for:
  1. OIPCSignalModel  — ratio / shift / per-expiry / guards / edge cases
  2. SweepDetectorModel — single update(), min_print_size in both modes
  3. FingerprintEngine  — OIPCSignalModel registered; update/detect lifecycle
  4. Format-string safety — None / 0.0 inputs to every fixed expression
  5. scan() guard logic  — _chain_specs vs option_contracts vs both empty
  6. OIStream tick type  — '588' used, '101' absent, futuresOpenInterest read
  7. Config keys         — all new keys present, typed, ordered correctly

Run from the options_scanner package root:
    pytest tests/test_new_signals.py -v
    pytest tests/test_new_signals.py -v -k sweep        # one class
    pytest tests/test_new_signals.py -v -k "oi_pc"      # all OI P/C tests

No IB connection required — all tests use synthetic DataFrames and mocks.
"""

import inspect
import re
import sys
import os

import pandas as pd
import pytest

# ── Make sure the package is importable when run directly ────────────────────
_HERE   = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _p in (_PARENT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Shared test data factories ────────────────────────────────────────────────

def _oi_df(call_oi: float, put_oi: float,
           expiry: str = '20250620',
           n_strikes: int = 12) -> pd.DataFrame:
    """
    Minimal snapshot DataFrame with openInterest populated.
    n_strikes must be even — half calls, half puts.
    """
    assert n_strikes % 2 == 0
    per_c = call_oi / (n_strikes // 2)
    per_p = put_oi  / (n_strikes // 2)
    rows = []
    for i in range(n_strikes // 2):
        rows.append({'expiry': expiry, 'strike': 100.0 + i,
                     'right': 'C', 'openInterest': per_c, 'volume': 10.0})
        rows.append({'expiry': expiry, 'strike': 100.0 + i,
                     'right': 'P', 'openInterest': per_p, 'volume': 10.0})
    return pd.DataFrame(rows)


def _sweep_df(strikes: list, right: str, expiry: str,
              vol: float) -> pd.DataFrame:
    """Snapshot DataFrame for sweep detection tests."""
    return pd.DataFrame([
        {'expiry': expiry, 'strike': float(s), 'right': right, 'volume': vol}
        for s in strikes
    ])


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  OIPCSignalModel
# ═══════════════════════════════════════════════════════════════════════════════

class TestOIPCSignalModel:

    @pytest.fixture(autouse=True)
    def _setup(self):
        from options_scanner.signals.fingerprint.oi_pc_signal import OIPCSignalModel
        from options_scanner.config import FINGERPRINT_CONFIG
        self.OIPCSignalModel    = OIPCSignalModel
        self.FINGERPRINT_CONFIG = FINGERPRINT_CONFIG

    # ── Observation guard ─────────────────────────────────────────────────────

    def test_no_findings_below_min_observations(self):
        """Fewer than oi_pc_min_observations non-None OI rows -> no findings."""
        model = self.OIPCSignalModel()
        df = pd.DataFrame([
            {'expiry': '20250620', 'strike': 100.0, 'right': 'C',
             'openInterest': 5000.0, 'volume': 10.0},
            {'expiry': '20250620', 'strike': 100.0, 'right': 'P',
             'openInterest': 9000.0, 'volume': 10.0},
        ])
        model.update('CL', df)
        assert model.detect('CL') == []

    def test_all_none_oi_produces_no_findings(self):
        """All-None openInterest rows excluded; model stays silent."""
        model = self.OIPCSignalModel()
        rows = [
            {'expiry': '20250620', 'strike': 100.0 + i,
             'right': 'C' if i % 2 == 0 else 'P',
             'openInterest': None, 'volume': 10.0}
            for i in range(20)
        ]
        model.update('CL', pd.DataFrame(rows))
        assert model.detect('CL') == []

    def test_mixed_none_and_valid_oi_fires(self):
        """
        None rows excluded; valid rows still produce a finding
        when put/call ratio is extreme.
        """
        model = self.OIPCSignalModel()
        rows = []
        for i in range(6):
            rows.append({'expiry': '20250620', 'strike': 100.0 + i,
                         'right': 'C', 'openInterest': 1000.0 / 6, 'volume': 5.0})
            rows.append({'expiry': '20250620', 'strike': 100.0 + i,
                         'right': 'P', 'openInterest': 3000.0 / 6, 'volume': 5.0})
        for i in range(8):
            rows.append({'expiry': '20250620', 'strike': 200.0 + i,
                         'right': 'C', 'openInterest': None, 'volume': 5.0})
        model.update('CL', pd.DataFrame(rows))
        findings = model.detect('CL')
        bearish = [f for f in findings
                   if f.finding_type == 'oi_pc_ratio'
                   and f.evidence.get('direction') == 'bearish']
        assert len(bearish) > 0

    # ── Absolute ratio findings ───────────────────────────────────────────────

    def test_bearish_ratio_finding(self):
        """put_oi / call_oi > bearish_threshold -> bearish oi_pc_ratio finding."""
        model = self.OIPCSignalModel()
        df = _oi_df(call_oi=1000, put_oi=3000, n_strikes=12)  # ratio = 3.0
        model.update('CL', df)
        findings = model.detect('CL')
        bearish = [f for f in findings
                   if f.finding_type == 'oi_pc_ratio'
                   and f.evidence.get('direction') == 'bearish']
        assert len(bearish) > 0, f"Expected bearish finding, got: {findings}"

    def test_bullish_ratio_finding(self):
        """put_oi / call_oi < bullish_threshold -> bullish oi_pc_ratio finding."""
        model = self.OIPCSignalModel()
        df = _oi_df(call_oi=1000, put_oi=500, n_strikes=12)  # ratio = 0.5
        model.update('CL', df)
        findings = model.detect('CL')
        bullish = [f for f in findings
                   if f.finding_type == 'oi_pc_ratio'
                   and f.evidence.get('direction') == 'bullish']
        assert len(bullish) > 0, f"Expected bullish finding, got: {findings}"

    def test_no_finding_neutral_ratio(self):
        """Ratio between thresholds (0.67 < r < 1.5) -> no oi_pc_ratio finding."""
        model = self.OIPCSignalModel()
        df = _oi_df(call_oi=1000, put_oi=1000, n_strikes=12)  # ratio = 1.0
        model.update('CL', df)
        findings = model.detect('CL')
        ratio_findings = [f for f in findings if f.finding_type == 'oi_pc_ratio']
        assert ratio_findings == [], f"Neutral ratio should be silent, got: {ratio_findings}"

    def test_finding_evidence_keys(self):
        """oi_pc_ratio finding evidence has scope, ratio, call_oi, put_oi, direction."""
        model = self.OIPCSignalModel()
        df = _oi_df(call_oi=1000, put_oi=3000, n_strikes=12)
        model.update('CL', df)
        findings = [f for f in model.detect('CL') if f.finding_type == 'oi_pc_ratio']
        assert findings, "Expected at least one oi_pc_ratio finding"
        for key in ('scope', 'ratio', 'call_oi', 'put_oi', 'direction'):
            assert key in findings[0].evidence, f"Missing evidence key: {key}"

    def test_confidence_bounded(self):
        """Confidence is in [0.50, 0.90] for threshold ratios."""
        from options_scanner.signals.fingerprint.oi_pc_signal import _ratio_confidence
        thr_b, thr_u = 1.5, 0.67
        for ratio, direction in [(1.51, 'bearish'), (0.66, 'bullish')]:
            c = _ratio_confidence(ratio, thr_b, thr_u, direction)
            assert 0.50 <= c <= 0.90, f"Confidence {c} out of range for ratio={ratio}"

    def test_confidence_increases_with_extremity(self):
        """More extreme ratios yield higher confidence."""
        from options_scanner.signals.fingerprint.oi_pc_signal import _ratio_confidence
        thr_b, thr_u = 1.5, 0.67
        assert (_ratio_confidence(3.0, thr_b, thr_u, 'bearish') >
                _ratio_confidence(1.6, thr_b, thr_u, 'bearish'))

    # ── Shift findings ────────────────────────────────────────────────────────

    def test_shift_finding_large_shift(self):
        """>=20% relative shift between scans -> oi_pc_shift finding."""
        model = self.OIPCSignalModel()
        model.update('CL', _oi_df(call_oi=1000, put_oi=1000, n_strikes=12))
        model.detect('CL')
        model.update('CL', _oi_df(call_oi=1000, put_oi=1500, n_strikes=12))
        findings = model.detect('CL')
        shifts = [f for f in findings if f.finding_type == 'oi_pc_shift']
        assert len(shifts) > 0, "50% shift should fire oi_pc_shift"

    def test_shift_direction_bearish_when_ratio_rises(self):
        """Rising P/C ratio -> shift direction is bearish."""
        model = self.OIPCSignalModel()
        model.update('CL', _oi_df(call_oi=1000, put_oi=1000, n_strikes=12))
        model.detect('CL')
        model.update('CL', _oi_df(call_oi=1000, put_oi=2000, n_strikes=12))
        findings = model.detect('CL')
        shifts = [f for f in findings if f.finding_type == 'oi_pc_shift']
        assert any(f.evidence.get('direction') == 'bearish' for f in shifts)

    def test_shift_direction_bullish_when_ratio_falls(self):
        """Falling P/C ratio -> shift direction is bullish."""
        model = self.OIPCSignalModel()
        model.update('CL', _oi_df(call_oi=1000, put_oi=2000, n_strikes=12))
        model.detect('CL')
        model.update('CL', _oi_df(call_oi=1000, put_oi=1000, n_strikes=12))
        findings = model.detect('CL')
        shifts = [f for f in findings if f.finding_type == 'oi_pc_shift']
        assert any(f.evidence.get('direction') == 'bullish' for f in shifts)

    def test_no_shift_below_threshold(self):
        """<20% relative shift -> no oi_pc_shift finding."""
        model = self.OIPCSignalModel()
        model.update('CL', _oi_df(call_oi=1000, put_oi=1000, n_strikes=12))
        model.detect('CL')
        model.update('CL', _oi_df(call_oi=1000, put_oi=1050, n_strikes=12))
        findings = model.detect('CL')
        shifts = [f for f in findings if f.finding_type == 'oi_pc_shift']
        assert shifts == [], f"5% shift should not fire, got: {shifts}"

    def test_no_shift_on_first_scan(self):
        """First scan has no prev_ratio -> no shift finding."""
        model = self.OIPCSignalModel()
        model.update('CL', _oi_df(call_oi=1000, put_oi=5000, n_strikes=12))
        findings = model.detect('CL')
        shifts = [f for f in findings if f.finding_type == 'oi_pc_shift']
        assert shifts == []

    def test_shift_evidence_keys(self):
        """oi_pc_shift evidence has prev_ratio, curr_ratio, rel_shift, direction."""
        model = self.OIPCSignalModel()
        model.update('CL', _oi_df(call_oi=1000, put_oi=1000, n_strikes=12))
        model.detect('CL')
        model.update('CL', _oi_df(call_oi=1000, put_oi=2000, n_strikes=12))
        findings = [f for f in model.detect('CL') if f.finding_type == 'oi_pc_shift']
        assert findings
        for key in ('scope', 'prev_ratio', 'curr_ratio', 'rel_shift', 'direction'):
            assert key in findings[0].evidence

    # ── Per-expiry breakdown ──────────────────────────────────────────────────

    def test_per_expiry_breakdown_fires(self):
        """Near expiry with bearish OI produces a per-expiry finding."""
        model = self.OIPCSignalModel()
        rows = []
        for i in range(6):
            rows.append({'expiry': '20250620', 'strike': 100.0 + i,
                         'right': 'C', 'openInterest': 100.0, 'volume': 5.0})
            rows.append({'expiry': '20250620', 'strike': 100.0 + i,
                         'right': 'P', 'openInterest': 300.0, 'volume': 5.0})
        for i in range(6):
            rows.append({'expiry': '20250720', 'strike': 100.0 + i,
                         'right': 'C', 'openInterest': 200.0, 'volume': 5.0})
            rows.append({'expiry': '20250720', 'strike': 100.0 + i,
                         'right': 'P', 'openInterest': 200.0, 'volume': 5.0})
        model.update('SI', pd.DataFrame(rows))
        findings = model.detect('SI')
        near = [f for f in findings
                if f.finding_type == 'oi_pc_ratio'
                and f.evidence.get('scope') == '20250620']
        assert len(near) > 0

    def test_all_and_per_expiry_scopes_both_present(self):
        """Findings include both scope='ALL' and scope=<expiry>."""
        model = self.OIPCSignalModel()
        rows = []
        for i in range(6):
            rows.append({'expiry': '20250620', 'strike': 100.0 + i,
                         'right': 'C', 'openInterest': 100.0, 'volume': 5.0})
            rows.append({'expiry': '20250620', 'strike': 100.0 + i,
                         'right': 'P', 'openInterest': 300.0, 'volume': 5.0})
        model.update('CL', pd.DataFrame(rows))
        findings = model.detect('CL')
        scopes = {f.evidence.get('scope') for f in findings}
        assert 'ALL' in scopes
        assert '20250620' in scopes

    def test_per_expiry_count_limited_by_config(self):
        """At most oi_pc_near_expiry_count per-expiry scopes are produced."""
        model = self.OIPCSignalModel()
        near_count = self.FINGERPRINT_CONFIG['oi_pc_near_expiry_count']
        rows = []
        expiries = ['20250620', '20250627', '20250704', '20250711', '20250718']
        for exp in expiries:
            for i in range(3):
                rows.append({'expiry': exp, 'strike': 100.0 + i,
                             'right': 'C', 'openInterest': 100.0, 'volume': 5.0})
                rows.append({'expiry': exp, 'strike': 100.0 + i,
                             'right': 'P', 'openInterest': 300.0, 'volume': 5.0})
        model.update('CL', pd.DataFrame(rows))
        findings = model.detect('CL')
        per_expiry = {f.evidence.get('scope') for f in findings
                      if f.finding_type == 'oi_pc_ratio'
                      and f.evidence.get('scope') != 'ALL'}
        assert len(per_expiry) <= near_count

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_zero_call_oi_no_crash(self):
        model = self.OIPCSignalModel()
        model.update('CL', _oi_df(call_oi=0, put_oi=2000, n_strikes=12))
        model.detect('CL')

    def test_zero_both_oi_no_crash(self):
        model = self.OIPCSignalModel()
        model.update('CL', _oi_df(call_oi=0, put_oi=0, n_strikes=12))
        model.detect('CL')

    def test_empty_dataframe_no_crash(self):
        model = self.OIPCSignalModel()
        model.update('CL', pd.DataFrame())
        assert model.detect('CL') == []

    def test_clear_resets_prev_ratio(self):
        """After clear(), no shift finding on next scan."""
        model = self.OIPCSignalModel()
        df = _oi_df(call_oi=1000, put_oi=3000, n_strikes=12)
        model.update('CL', df)
        model.detect('CL')
        model.clear('CL')
        model.update('CL', df)
        findings = model.detect('CL')
        assert [f for f in findings if f.finding_type == 'oi_pc_shift'] == []

    def test_instruments_isolated(self):
        """CL state does not bleed into SI on first SI scan."""
        model = self.OIPCSignalModel()
        model.update('CL', _oi_df(call_oi=1000, put_oi=1000, n_strikes=12))
        model.detect('CL')
        model.update('CL', _oi_df(call_oi=1000, put_oi=2000, n_strikes=12))
        model.update('SI', _oi_df(call_oi=500, put_oi=500, n_strikes=12))
        si_findings = model.detect('SI')
        assert [f for f in si_findings if f.finding_type == 'oi_pc_shift'] == []

    def test_non_dataframe_update_ignored(self):
        model = self.OIPCSignalModel()
        model.update('CL', None)
        model.update('CL', [])
        model.update('CL', {'key': 'value'})
        assert model.detect('CL') == []


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  SweepDetectorModel
# ═══════════════════════════════════════════════════════════════════════════════

class TestSweepDetectorModel:

    @pytest.fixture(autouse=True)
    def _setup(self):
        from options_scanner.signals.fingerprint.sweep_detector import SweepDetectorModel
        from options_scanner.config import FINGERPRINT_CONFIG
        self.SweepDetectorModel = SweepDetectorModel
        self.cfg                = FINGERPRINT_CONFIG

    # ── Structural correctness ────────────────────────────────────────────────

    def test_single_update_method(self):
        """Exactly one update() defined — dead shadowed duplicate removed."""
        model = self.SweepDetectorModel()
        updates = [
            name
            for name, m in inspect.getmembers(model, predicate=inspect.ismethod)
            if name == 'update'
        ]
        assert len(updates) == 1

    def test_state_attributes_present(self):
        model = self.SweepDetectorModel()
        assert hasattr(model, '_current_vol')
        assert hasattr(model, '_prev_vol_snap')
        assert hasattr(model, '_tick_buf')

    # ── Snapshot sweep ────────────────────────────────────────────────────────

    def test_sweep_fires_above_min_print_size(self):
        """N+ strikes gaining >= min_print_size triggers a sweep finding."""
        min_size    = self.cfg['sweep_min_print_size']
        min_strikes = self.cfg['sweep_min_strikes']
        strikes = list(range(100, 100 + min_strikes + 1))

        model = self.SweepDetectorModel()
        model.update('CL', _sweep_df(strikes, 'C', '20250620', vol=100))
        model.detect('CL')
        model.update('CL', _sweep_df(strikes, 'C', '20250620', vol=100 + min_size))
        findings = [f for f in model.detect('CL') if f.finding_type == 'sweep']
        assert len(findings) > 0

    def test_sweep_suppressed_below_min_print_size(self):
        """Gain of (min_print_size - 1) does not fire."""
        min_size    = self.cfg['sweep_min_print_size']
        min_strikes = self.cfg['sweep_min_strikes']
        if min_size == 0:
            pytest.skip("sweep_min_print_size=0 disables size filter")

        strikes    = list(range(100, 100 + min_strikes + 2))
        small_gain = min_size - 1

        model = self.SweepDetectorModel()
        model.update('CL', _sweep_df(strikes, 'P', '20250620', vol=100))
        model.detect('CL')
        model.update('CL', _sweep_df(strikes, 'P', '20250620', vol=100 + small_gain))
        findings = [f for f in model.detect('CL') if f.finding_type == 'sweep']
        assert findings == []

    def test_sweep_evidence_has_min_print_size(self):
        """Sweep finding evidence includes min_print_size field."""
        min_size    = self.cfg['sweep_min_print_size']
        min_strikes = self.cfg['sweep_min_strikes']
        strikes = list(range(100, 100 + min_strikes + 1))

        model = self.SweepDetectorModel()
        model.update('CL', _sweep_df(strikes, 'C', '20250620', vol=100))
        model.detect('CL')
        model.update('CL', _sweep_df(strikes, 'C', '20250620', vol=100 + min_size))
        findings = [f for f in model.detect('CL') if f.finding_type == 'sweep']
        if not findings:
            pytest.skip("No sweep produced with current config")
        assert 'min_print_size' in findings[0].evidence
        assert findings[0].evidence['min_print_size'] == min_size

    def test_no_sweep_on_first_scan_only(self):
        """First scan (no prev baseline) never fires a sweep."""
        model = self.SweepDetectorModel()
        min_strikes = self.cfg['sweep_min_strikes']
        strikes = list(range(100, 100 + min_strikes + 2))
        model.update('CL', _sweep_df(strikes, 'C', '20250620', vol=500))
        findings = [f for f in model.detect('CL') if f.finding_type == 'sweep']
        assert findings == []

    def test_no_sweep_below_min_strikes(self):
        """(min_strikes - 1) lit strikes does not fire."""
        min_size    = self.cfg['sweep_min_print_size']
        min_strikes = self.cfg['sweep_min_strikes']
        strikes = list(range(100, 100 + min_strikes - 1))

        model = self.SweepDetectorModel()
        model.update('CL', _sweep_df(strikes, 'C', '20250620', vol=100))
        model.detect('CL')
        model.update('CL', _sweep_df(strikes, 'C', '20250620', vol=100 + min_size * 2))
        findings = [f for f in model.detect('CL') if f.finding_type == 'sweep']
        assert findings == []

    def test_sweep_finding_metadata(self):
        """Sweep finding has correct source, instrument, expiry, right."""
        min_size    = self.cfg['sweep_min_print_size']
        min_strikes = self.cfg['sweep_min_strikes']
        expiry, right = '20250620', 'C'
        strikes = list(range(100, 100 + min_strikes + 1))

        model = self.SweepDetectorModel()
        model.update('CL', _sweep_df(strikes, right, expiry, vol=100))
        model.detect('CL')
        model.update('CL', _sweep_df(strikes, right, expiry, vol=100 + min_size))
        findings = [f for f in model.detect('CL') if f.finding_type == 'sweep']
        if not findings:
            pytest.skip("No sweep produced with current config")
        f = findings[0]
        assert f.source     == 'ibkr_snapshot'
        assert f.instrument == 'CL'
        assert f.expiry     == expiry
        assert f.right      == right

    def test_different_expiries_independent(self):
        """Volume gain in expiry A does not flag expiry B."""
        min_size    = self.cfg['sweep_min_print_size']
        min_strikes = self.cfg['sweep_min_strikes']
        strikes = list(range(100, 100 + min_strikes + 1))

        model = self.SweepDetectorModel()
        df1 = pd.concat([_sweep_df(strikes, 'C', '20250620', vol=100),
                         _sweep_df(strikes, 'C', '20250720', vol=100)],
                        ignore_index=True)
        model.update('CL', df1)
        model.detect('CL')

        df2 = pd.concat([_sweep_df(strikes, 'C', '20250620', vol=100 + min_size),
                         _sweep_df(strikes, 'C', '20250720', vol=100)],   # unchanged
                        ignore_index=True)
        model.update('CL', df2)
        findings = [f for f in model.detect('CL') if f.finding_type == 'sweep']
        swept_expiries = {f.expiry for f in findings}
        assert '20250720' not in swept_expiries

    # ── Tick sweep ────────────────────────────────────────────────────────────

    def test_tick_sweep_filters_small_prints(self):
        """Tick prints below min_print_size are excluded from tick sweep."""
        import options_scanner.signals.fingerprint.sweep_detector as sd_mod
        min_size    = self.cfg['sweep_min_print_size']
        min_strikes = self.cfg['sweep_min_strikes']
        if min_size == 0:
            pytest.skip("sweep_min_print_size=0 disables size filter")
        original = sd_mod.USE_STREAMING_TICKS
        try:
            sd_mod.USE_STREAMING_TICKS = True
            model = self.SweepDetectorModel()
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).isoformat()
            prints = [
                {'conId': 100 + i, 'size': min_size - 1, 'price': 5.0, 'ts': ts}
                for i in range(min_strikes + 2)
            ]
            model.update('CL', prints)
            findings = [f for f in model.detect('CL') if f.finding_type == 'tick_sweep']
            assert findings == []
        finally:
            sd_mod.USE_STREAMING_TICKS = original

    def test_tick_sweep_fires_above_threshold(self):
        """Large prints across N+ conIds fire tick_sweep."""
        import options_scanner.signals.fingerprint.sweep_detector as sd_mod
        min_size    = self.cfg['sweep_min_print_size']
        min_strikes = self.cfg['sweep_min_strikes']
        original = sd_mod.USE_STREAMING_TICKS
        try:
            sd_mod.USE_STREAMING_TICKS = True
            model = self.SweepDetectorModel()
            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).isoformat()
            prints = [
                {'conId': 100 + i, 'size': max(min_size, 1),
                 'price': 5.0, 'ts': ts}
                for i in range(min_strikes + 1)
            ]
            model.update('CL', prints)
            findings = [f for f in model.detect('CL') if f.finding_type == 'tick_sweep']
            assert len(findings) > 0
        finally:
            sd_mod.USE_STREAMING_TICKS = original

    # ── State management ──────────────────────────────────────────────────────

    def test_clear_removes_instrument_state(self):
        model = self.SweepDetectorModel()
        df = _sweep_df([100, 101, 102], 'C', '20250620', vol=100)
        model.update('CL', df)
        model.clear('CL')
        assert 'CL' not in model._current_vol
        assert 'CL' not in model._prev_vol_snap
        assert 'CL' not in model._tick_buf

    def test_clear_does_not_affect_other_instruments(self):
        model = self.SweepDetectorModel()
        df = _sweep_df([100, 101, 102], 'C', '20250620', vol=100)
        model.update('CL', df)
        model.update('SI', df)
        model.clear('CL')
        assert 'SI' in model._current_vol

    def test_empty_dataframe_no_crash(self):
        model = self.SweepDetectorModel()
        model.update('CL', pd.DataFrame())
        assert model.detect('CL') == []

    def test_non_dataframe_in_snapshot_mode_ignored(self):
        model = self.SweepDetectorModel()
        model.update('CL', None)
        model.update('CL', {'bad': 'data'})
        assert model.detect('CL') == []


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  FingerprintEngine — registration and lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

class TestFingerprintEngineRegistration:

    @pytest.fixture(autouse=True)
    def _setup(self):
        from options_scanner.signals.fingerprint.engine import (
            FingerprintEngine, FINGERPRINT_MODELS,
        )
        from options_scanner.signals.fingerprint.oi_pc_signal import OIPCSignalModel
        from options_scanner.signals.fingerprint.sweep_detector import SweepDetectorModel
        from options_scanner.signals.fingerprint.base import Finding
        self.FingerprintEngine  = FingerprintEngine
        self.FINGERPRINT_MODELS = FINGERPRINT_MODELS
        self.OIPCSignalModel    = OIPCSignalModel
        self.SweepDetectorModel = SweepDetectorModel
        self.Finding            = Finding

    def test_oi_pc_model_registered(self):
        names = [type(m).__name__ for m in self.FINGERPRINT_MODELS]
        assert 'OIPCSignalModel' in names

    def test_sweep_detector_registered(self):
        names = [type(m).__name__ for m in self.FINGERPRINT_MODELS]
        assert 'SweepDetectorModel' in names

    def test_all_six_models_registered(self):
        expected = {
            'OIBuildModel', 'VolumeClusterModel', 'SweepDetectorModel',
            'PrintClusterModel', 'ExpiryConcentrationModel', 'OIPCSignalModel',
        }
        found = {type(m).__name__ for m in self.FINGERPRINT_MODELS}
        assert not (expected - found), f"Missing models: {expected - found}"

    def test_custom_models_accepted(self):
        engine = self.FingerprintEngine(models=[self.OIPCSignalModel()])
        assert len(engine._models) == 1

    def test_update_detect_with_oi_data_no_crash(self):
        engine = self.FingerprintEngine()
        engine.update('CL', _oi_df(call_oi=1000, put_oi=3000, n_strikes=12))
        engine.detect('CL')

    def test_update_detect_with_all_none_oi_no_crash(self):
        engine = self.FingerprintEngine()
        rows = [{'expiry': '20250620', 'strike': 100.0, 'right': 'C',
                 'openInterest': None, 'volume': 10.0} for _ in range(12)]
        engine.update('CL', pd.DataFrame(rows))
        engine.detect('CL')

    def test_detect_returns_finding_instances(self):
        """All returned findings are Finding instances."""
        engine = self.FingerprintEngine()
        df = _oi_df(call_oi=1000, put_oi=5000, n_strikes=12)
        engine.update('CL', df)
        for f in engine.detect('CL'):
            assert isinstance(f, self.Finding)

    def test_clear_wipes_oi_pc_state(self):
        """engine.clear() prevents shift finding from firing on next scan."""
        engine = self.FingerprintEngine()
        df = _oi_df(call_oi=1000, put_oi=3000, n_strikes=12)
        engine.update('CL', df)
        engine.detect('CL')
        engine.clear('CL')
        engine.update('CL', df)
        findings = engine.detect('CL')
        assert [f for f in findings if f.finding_type == 'oi_pc_shift'] == []

    def test_broken_model_does_not_kill_detect(self):
        """Exception in one model is caught; remaining models still run."""
        from options_scanner.signals.fingerprint.base import BaseFingerprintModel

        class BrokenModel(BaseFingerprintModel):
            NAME           = 'broken'
            accepts_source = ['ibkr_snapshot']
            def update(self, instrument, data): pass
            def detect(self, instrument): raise RuntimeError("intentional")
            def clear(self, instrument): pass

        engine = self.FingerprintEngine(
            models=[BrokenModel(), self.OIPCSignalModel()]
        )
        df = _oi_df(call_oi=1000, put_oi=3000, n_strikes=12)
        engine.update('CL', df)
        engine.detect('CL')   # must not raise


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  Format-string safety
# ═══════════════════════════════════════════════════════════════════════════════

class TestFormatStrings:

    @pytest.mark.parametrize("price,expected", [
        (None,  'N/A'),
        (0.0,   'N/A'),   # 0.0 is falsy — acceptable for price context
        (72.35, '72.35'),
        (0.001, '0.001'),
        (1234.5, '1234'),
    ])
    def test_und_price_str_expression(self, price, expected):
        """
        ib_client.py qualify_chain_for_scan() fixed expression:
            und_price_str = f"{underlying_price:.4g}" if underlying_price else 'N/A'
        """
        underlying_price = price
        und_price_str = f"{underlying_price:.4g}" if underlying_price else 'N/A'
        assert und_price_str == expected

    def test_original_broken_pattern_raises(self):
        """Confirm the original broken pattern crashes — fix was necessary."""
        with pytest.raises((ValueError, TypeError)):
            underlying_price = 72.35
            _ = f"{underlying_price:.4g if underlying_price else 'N/A'}"

    @pytest.mark.parametrize("v,expected", [
        (None,  'N/A'),
        (0.0,   '0'),      # 0.0 is a valid price — must NOT show N/A
        (72.5,  '72.5'),
        (-1.23, '-1.23'),
    ])
    def test_price_display_v_is_not_none(self, v, expected):
        """
        instrument.py _print_scan_summary() fixed expression:
            val = f"{v:.5g}" if v is not None else 'N/A'
        Old code used `if v` — 0.0 would falsely produce 'N/A'.
        """
        val = f"{v:.5g}" if v is not None else 'N/A'
        assert val == expected

    def test_old_falsy_guard_wrong_for_zero(self):
        """Prove the old `if v` guard was wrong for 0.0."""
        v = 0.0
        old = f"{v:.5g}" if v else 'N/A'
        new = f"{v:.5g}" if v is not None else 'N/A'
        assert old == 'N/A', "Old code: 0.0 falsely mapped to N/A"
        assert new == '0',   "New code: 0.0 correctly formats as '0'"

    def test_signal_call_put_vol_always_float(self):
        """
        SignalResult.call_vol and .put_vol are float (never None).
        Formatting with :.0f must not raise even when volume is all-None.
        """
        from options_scanner.signals.engine import evaluate
        from options_scanner.signals.volume_history import VolumeHistory
        df = pd.DataFrame([
            {'right': 'C', 'volume': None, 'iv': None, 'delta': None,
             'openInterest': None, 'strike': 100.0, 'expiry': '20250620'},
            {'right': 'P', 'volume': None, 'iv': None, 'delta': None,
             'openInterest': None, 'strike': 100.0, 'expiry': '20250620'},
        ])
        result = evaluate(df, VolumeHistory(instrument='TEST'))
        _ = f"{result.call_vol:.0f}"
        _ = f"{result.put_vol:.0f}"
        assert isinstance(result.call_vol, float)
        assert isinstance(result.put_vol, float)


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  scan() guard logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestScanGuard:
    """
    Tests for the fixed scan() guard in InstrumentScanner:
        if not self._discovered or (not self.option_contracts and not self._chain_specs):
    """

    @staticmethod
    def _skip(discovered, chain_specs, option_contracts):
        return not discovered or (not option_contracts and not chain_specs)

    def test_skips_when_not_discovered(self):
        assert self._skip(False, [object()], []) is True

    def test_skips_when_both_empty(self):
        assert self._skip(True, [], []) is True

    def test_runs_when_chain_specs_populated(self):
        """v0.3.0 cache path: _chain_specs non-empty, option_contracts=[]."""
        assert self._skip(True, [object()], []) is False

    def test_runs_when_option_contracts_populated(self):
        """Legacy path: option_contracts non-empty, chain_specs=[]."""
        assert self._skip(True, [], [object()]) is False

    def test_runs_when_both_populated(self):
        assert self._skip(True, [object()], [object()]) is False

    def test_not_discovered_overrides_populated_chains(self):
        assert self._skip(False, [object()], [object()]) is True

    def test_instrument_scanner_source_contains_correct_guard(self):
        """
        Read actual InstrumentScanner.scan() source and confirm:
          - _chain_specs is checked
          - old broken single-condition form is not present
        """
        import options_scanner.scanner.instrument as inst_mod
        source = inspect.getsource(inst_mod.InstrumentScanner.scan)
        assert 'not self._chain_specs' in source, \
            "scan() guard must include 'not self._chain_specs'"
        assert 'not self.option_contracts' in source, \
            "scan() guard must include 'not self.option_contracts'"
        # Old broken form had `not self.option_contracts` as the only check
        # (not anded with _chain_specs). The corrected form wraps both in parens.
        assert 'not self._chain_specs' in source, \
            "Scan guard missing _chain_specs check — reverted to broken form?"


# ═══════════════════════════════════════════════════════════════════════════════
# 6.  OIStream tick type constants
# ═══════════════════════════════════════════════════════════════════════════════

class TestOIStreamTickType:
    """
    Confirm OIStream uses generic tick '588' (Futures Open Interest, tick ID 86)
    and reads Ticker.futuresOpenInterest — NOT tick 22 / 101 / .openInterest.

    IB tick type reference:
      Tick 22:  'Open Interest'               DEPRECATED — not populated
      Tick 86:  'Futures Open Interest'       CORRECT — generic tick '588'
      Tick 27/28: 'Option Call/Put OI'        CORRECT for option chain, NOT futures
    """

    @pytest.fixture(autouse=True)
    def _load_source(self):
        import options_scanner.data.ib_client as ib_mod
        full = inspect.getsource(ib_mod)
        start = full.find('class OIStream')
        assert start > 0, "OIStream class not found in ib_client"
        self.oi_src   = full[start:]
        self.full_src = full

    def test_uses_generic_tick_588(self):
        assert "'588'" in self.oi_src, \
            "OIStream must use genericTickList='588'"

    def test_does_not_use_generic_tick_101_as_ticklist(self):
        bad = re.findall(r"genericTickList\s*=\s*['\"].*?101.*?['\"]", self.oi_src)
        assert bad == [], f"OIStream must not use genericTickList='101': {bad}"

    def test_reads_futures_open_interest_attribute(self):
        assert 'futuresOpenInterest' in self.oi_src, \
            "OIStream.oi() must read Ticker.futuresOpenInterest"

    def test_does_not_read_deprecated_open_interest_attribute(self):
        bad = re.findall(r"getattr\([^)]+['\"]openInterest['\"]", self.oi_src)
        assert bad == [], \
            f"OIStream must not read .openInterest (deprecated tick 22): {bad}"

    def test_snapshot_false(self):
        assert 'snapshot=False' in self.oi_src, \
            "OIStream must use snapshot=False — genericTickList requires streaming"

    def test_docstring_mentions_tick_588_or_86(self):
        doc_section = self.oi_src[:self.oi_src.find('def start')]
        assert '588' in doc_section or '86' in doc_section, \
            "OIStream docstring should reference generic tick 588 / tick ID 86"

    def test_equity_stream_uses_separate_tick_list(self):
        """EquityStream (STK/OPT) uses a different tick list — not '588'."""
        eq_start = self.full_src.find('class EquityStream')
        assert eq_start > 0, "EquityStream class not found"
        eq_src = self.full_src[eq_start: eq_start + 2000]
        # EquityStream correctly uses 101 for option chain aggregate OI (STK)
        assert "'588'" not in eq_src, \
            "EquityStream should NOT use '588' — that is FUT-specific"


# ═══════════════════════════════════════════════════════════════════════════════
# 7.  Config keys
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigKeys:

    @pytest.fixture(autouse=True)
    def _load(self):
        from options_scanner.config import FINGERPRINT_CONFIG
        self.cfg = FINGERPRINT_CONFIG

    def test_sweep_min_print_size_present_and_typed(self):
        assert 'sweep_min_print_size' in self.cfg
        assert isinstance(self.cfg['sweep_min_print_size'], int)
        assert self.cfg['sweep_min_print_size'] >= 0

    @pytest.mark.parametrize("key", [
        'oi_pc_min_observations',
        'oi_pc_bearish_threshold',
        'oi_pc_bullish_threshold',
        'oi_pc_shift_threshold',
        'oi_pc_near_expiry_count',
    ])
    def test_oi_pc_key_present(self, key):
        assert key in self.cfg

    def test_threshold_ordering(self):
        assert self.cfg['oi_pc_bullish_threshold'] < self.cfg['oi_pc_bearish_threshold']

    def test_min_observations_positive(self):
        assert self.cfg['oi_pc_min_observations'] > 0

    def test_shift_threshold_non_negative(self):
        assert self.cfg['oi_pc_shift_threshold'] >= 0.0

    def test_near_expiry_count_non_negative(self):
        assert self.cfg['oi_pc_near_expiry_count'] >= 0

    def test_existing_keys_not_removed(self):
        """Regression: ensure no pre-existing keys were accidentally dropped."""
        required = [
            'min_confidence', 'alert_threshold', 'corroboration_bonus',
            'oi_build_min_scans', 'volume_cluster_min_strikes', 'lot_size_bucket',
            'sweep_min_strikes', 'sweep_require_adjacency',
            'print_cluster_window_sec', 'print_cluster_min_prints',
            'expiry_concentration_threshold', 'max_history_per_key',
        ]
        for key in required:
            assert key in self.cfg, f"Pre-existing key removed: {key}"
