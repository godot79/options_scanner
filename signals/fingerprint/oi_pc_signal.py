"""
signals/fingerprint/oi_pc_signal.py
-------------------------------------
Detects directional open interest positioning via the put/call OI ratio
across the moneyness band and for the nearest N expiries.

OI is structurally different from volume:
  - Volume resets daily and reflects intraday flow.
  - OI accumulates across sessions and reflects net positioning.
  - A rising put OI / call OI ratio over multiple scans suggests increasing
    downside protection or directional put accumulation.

Data source:
  The model accepts a pd.DataFrame with an 'openInterest' column, identical
  in shape to the snapshot DataFrame used by all other fingerprint models.
  OI arrives via:
    - Persistent streaming (genericTickList includes tick 101) for OPT/STK
    - FOP: open_interest stream opened per underlying at scanner startup
      (see instrument.py OIStream).  IB does not populate OI on snapshots
      (snapshot=True is incompatible with genericTickList).
  Rows where openInterest is None are silently skipped; findings are
  suppressed until oi_pc_min_observations non-None rows are present.

Two finding types are emitted:
  'oi_pc_ratio'   — absolute ratio exceeds bullish/bearish threshold
  'oi_pc_shift'   — ratio has moved by >= oi_pc_shift_threshold from last scan

Both are computed for:
  (a) the full moneyness band (macro picture)
  (b) each of the nearest oi_pc_near_expiry_count expiries (actionable view)

Config keys (all in FINGERPRINT_CONFIG):
  oi_pc_min_observations   : int   — min non-None OI rows before computing
  oi_pc_bearish_threshold  : float — put_oi / call_oi > this => bearish
  oi_pc_bullish_threshold  : float — put_oi / call_oi < this => bullish
  oi_pc_shift_threshold    : float — relative shift fraction to flag (e.g. 0.20)
  oi_pc_near_expiry_count  : int   — how many nearest expiries to break down
"""

from collections import defaultdict
from typing import Any, Optional

import pandas as pd

from options_scanner.config import FINGERPRINT_CONFIG
from options_scanner.signals.fingerprint.base import BaseFingerprintModel, Finding


class OIPCSignalModel(BaseFingerprintModel):

    NAME           = 'oi_pc_signal'
    accepts_source = ['ibkr_snapshot']

    def __init__(self):
        # Last computed ratio per (instrument, scope_key).
        # scope_key: 'ALL' for full band, expiry string for per-expiry.
        self._prev_ratio: dict[str, dict[str, float]] = defaultdict(dict)
        # Current snapshot DataFrame per instrument (replaced each update)
        self._current: dict[str, pd.DataFrame] = {}

    # ── Update ────────────────────────────────────────────────────────────────

    def update(self, instrument: str, data: Any) -> None:
        """data: pd.DataFrame with columns expiry, right, openInterest"""
        if not isinstance(data, pd.DataFrame) or data.empty:
            return
        self._current[instrument] = data.copy()

    # ── Detect ────────────────────────────────────────────────────────────────

    def detect(self, instrument: str) -> list[Finding]:
        df = self._current.get(instrument)
        if df is None or df.empty:
            return []

        min_obs         = FINGERPRINT_CONFIG['oi_pc_min_observations']
        bearish_thr     = FINGERPRINT_CONFIG['oi_pc_bearish_threshold']
        bullish_thr     = FINGERPRINT_CONFIG['oi_pc_bullish_threshold']
        shift_thr       = FINGERPRINT_CONFIG['oi_pc_shift_threshold']
        near_exp_count  = FINGERPRINT_CONFIG['oi_pc_near_expiry_count']

        # Work only with rows that have a valid OI value
        oi_df = df[df['openInterest'].notna() & (df['openInterest'] >= 0)].copy()

        if len(oi_df) < min_obs:
            # Not enough OI data yet — IB stream hasn't populated for this session
            return []

        findings: list[Finding] = []

        # (a) Full moneyness band
        findings.extend(
            self._evaluate_scope(
                instrument  = instrument,
                scope_df    = oi_df,
                scope_key   = 'ALL',
                scope_label = 'full moneyness band',
                bearish_thr = bearish_thr,
                bullish_thr = bullish_thr,
                shift_thr   = shift_thr,
            )
        )

        # (b) Nearest N expiries
        if near_exp_count > 0:
            sorted_expiries = sorted(oi_df['expiry'].dropna().unique())
            for expiry in sorted_expiries[:near_exp_count]:
                exp_df = oi_df[oi_df['expiry'] == expiry]
                if len(exp_df) < 2:   # need at least one C and one P row
                    continue
                findings.extend(
                    self._evaluate_scope(
                        instrument  = instrument,
                        scope_df    = exp_df,
                        scope_key   = str(expiry),
                        scope_label = f'expiry {expiry}',
                        bearish_thr = bearish_thr,
                        bullish_thr = bullish_thr,
                        shift_thr   = shift_thr,
                    )
                )

        # Roll prev ratio for shift detection on next scan
        # (done after evaluate_scope calls so _prev_ratio still holds last scan)
        # Note: _evaluate_scope updates _prev_ratio internally after reading it.

        return findings

    # ── Scope evaluator ───────────────────────────────────────────────────────

    def _evaluate_scope(self,
                        instrument  : str,
                        scope_df    : pd.DataFrame,
                        scope_key   : str,
                        scope_label : str,
                        bearish_thr : float,
                        bullish_thr : float,
                        shift_thr   : float) -> list[Finding]:
        """
        Compute OI P/C ratio for one scope (full band or single expiry).
        Emit findings for absolute threshold and shift, then update prev_ratio.
        """
        call_oi = float(
            scope_df[scope_df['right'] == 'C']['openInterest'].sum()
        )
        put_oi  = float(
            scope_df[scope_df['right'] == 'P']['openInterest'].sum()
        )

        if call_oi <= 0 and put_oi <= 0:
            return []

        ratio: Optional[float] = (put_oi / call_oi) if call_oi > 0 else None

        findings: list[Finding] = []
        prev_ratio = self._prev_ratio[instrument].get(scope_key)

        # ── Absolute threshold finding ────────────────────────────────────────
        direction = _ratio_direction(ratio, bearish_thr, bullish_thr)
        if direction is not None:
            confidence = _ratio_confidence(ratio, bearish_thr, bullish_thr, direction)
            findings.append(Finding(
                confidence   = confidence,
                source       = 'ibkr_snapshot',
                instrument   = instrument,
                model        = self.NAME,
                finding_type = 'oi_pc_ratio',
                note         = (
                    f"OI P/C ratio {ratio:.3f} ({scope_label}) — "
                    f"{direction} signal "
                    f"[call OI={call_oi:.0f}, put OI={put_oi:.0f}]"
                ),
                evidence     = {
                    'scope'    : scope_key,
                    'ratio'    : round(ratio, 4) if ratio is not None else None,
                    'call_oi'  : call_oi,
                    'put_oi'   : put_oi,
                    'direction': direction,
                },
            ))

        # ── Shift finding ─────────────────────────────────────────────────────
        if (shift_thr > 0.0
                and prev_ratio is not None
                and ratio is not None
                and prev_ratio > 0):
            rel_shift = (ratio - prev_ratio) / prev_ratio
            if abs(rel_shift) >= shift_thr:
                shift_dir = 'bearish' if rel_shift > 0 else 'bullish'
                confidence = min(0.5 + 0.4 * (abs(rel_shift) - shift_thr), 0.90)
                confidence = round(confidence, 4)
                findings.append(Finding(
                    confidence   = confidence,
                    source       = 'ibkr_snapshot',
                    instrument   = instrument,
                    model        = self.NAME,
                    finding_type = 'oi_pc_shift',
                    note         = (
                        f"OI P/C ratio shifted {rel_shift:+.1%} ({scope_label}): "
                        f"{prev_ratio:.3f} -> {ratio:.3f} — "
                        f"{shift_dir} shift signal"
                    ),
                    evidence     = {
                        'scope'      : scope_key,
                        'prev_ratio' : round(prev_ratio, 4),
                        'curr_ratio' : round(ratio, 4),
                        'rel_shift'  : round(rel_shift, 4),
                        'direction'  : shift_dir,
                    },
                ))

        # Update prev_ratio for next scan
        if ratio is not None:
            self._prev_ratio[instrument][scope_key] = ratio

        return findings

    def clear(self, instrument: str) -> None:
        self._current.pop(instrument, None)
        self._prev_ratio.pop(instrument, None)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ratio_direction(ratio     : Optional[float],
                     bearish_thr: float,
                     bullish_thr: float) -> Optional[str]:
    """Return 'bearish', 'bullish', or None based on absolute ratio."""
    if ratio is None:
        return None
    if ratio > bearish_thr:
        return 'bearish'
    if ratio < bullish_thr:
        return 'bullish'
    return None


def _ratio_confidence(ratio      : float,
                      bearish_thr: float,
                      bullish_thr: float,
                      direction  : str) -> float:
    """
    Confidence scales linearly from 0.50 at threshold to 0.90 at 2x threshold.
    """
    if direction == 'bearish' and bearish_thr > 0:
        excess = (ratio - bearish_thr) / bearish_thr
    elif direction == 'bullish' and bullish_thr > 0:
        excess = (bullish_thr - ratio) / bullish_thr
    else:
        excess = 0.0
    confidence = 0.50 + 0.40 * min(excess, 1.0)
    return round(confidence, 4)
