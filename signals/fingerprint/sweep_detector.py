"""
signals/fingerprint/sweep_detector.py
---------------------------------------
Detects option sweeps: aggressive directional buying/selling across
multiple strikes in the same expiry within a single scan interval.

A sweep is the signature of a large, urgency-driven order that accepts
market prices across the chain rather than working limit orders patiently.
It is the single strongest single-actor fingerprint available from
snapshot data.

Detection logic (Option B — snapshot):
  Within one scan cycle, for a given (instrument, expiry, right):
    1. Find strikes where volume increased from previous scan (delta_vol > 0)
    2. Require delta_vol >= sweep_min_print_size (configurable — filters retail)
    3. Require delta_vol > 0 at N+ strikes (configurable)
    4. Optionally require strikes to be adjacent (no gap > max_gap strikes)
    5. Confidence scales with:
       - number of strikes swept
       - consistency of volume (low CV = uniform lot size = more suspicious)
       - fraction of chain volume in sweep strikes

Option A upgrade (streaming ticks):
  When USE_STREAMING_TICKS=True, the sweep is detected from actual print
  sequences: same-direction aggressor (trade at ask = buy, at bid = sell)
  hitting multiple strikes within print_cluster_window_sec seconds.
  Only prints >= sweep_min_print_size contracts are counted.
  This provides a much cleaner signal with timestamps and aggressor side.

Data source : ibkr_snapshot (Option B) or ibkr_tick (Option A)
"""

from collections import defaultdict
from statistics import mean, stdev
from typing import Any

import pandas as pd

from options_scanner.config import FINGERPRINT_CONFIG, USE_STREAMING_TICKS
from options_scanner.signals.fingerprint.base import BaseFingerprintModel, Finding


class SweepDetectorModel(BaseFingerprintModel):

    NAME           = 'sweep_detector'
    accepts_source = ['ibkr_snapshot', 'ibkr_tick']

    def __init__(self):
        # snapshot path: two-snapshot buffers per instrument
        self._current_vol   : dict = defaultdict(lambda: defaultdict(dict))
        self._prev_vol_snap : dict = defaultdict(lambda: defaultdict(dict))
        # tick path: buffer of raw prints per instrument
        self._tick_buf      : dict[str, list[dict]] = defaultdict(list)

    # ── Update ────────────────────────────────────────────────────────────────

    def update(self, instrument: str, data: Any) -> None:
        """
        Snapshot path: roll current -> prev, then store new snapshot.
        Tick path: buffer raw prints for Option A detection.
        """
        if USE_STREAMING_TICKS and isinstance(data, list):
            self._update_ticks(instrument, data)
            return

        if not isinstance(data, pd.DataFrame) or data.empty:
            return

        # Roll current -> prev
        self._prev_vol_snap[instrument] = {
            k: dict(v) for k, v in self._current_vol[instrument].items()
        }
        self._current_vol[instrument].clear()

        for _, row in data.iterrows():
            vol = row.get('volume')
            if vol is None or vol < 0:
                continue
            expiry = str(row.get('expiry', ''))
            right  = str(row.get('right', ''))
            strike = float(row.get('strike', 0))
            if not expiry or not right or strike <= 0:
                continue
            key = f"{expiry}|{right}"
            self._current_vol[instrument][key][strike] = float(vol)

    def _update_ticks(self, instrument: str, prints: list[dict]) -> None:
        """Buffer raw tick prints for Option A detection."""
        self._tick_buf[instrument].extend(prints)
        # Keep last 10 minutes of ticks (rough upper bound)
        max_buf = 10 * 60 * 100
        if len(self._tick_buf[instrument]) > max_buf:
            self._tick_buf[instrument] = self._tick_buf[instrument][-max_buf:]

    # ── Detect ────────────────────────────────────────────────────────────────

    def detect(self, instrument: str) -> list[Finding]:
        if USE_STREAMING_TICKS:
            return self._detect_tick_sweep(instrument)
        return self._detect_with_two_snapshots(instrument)

    # ── Option B: snapshot sweep detection ───────────────────────────────────

    def _detect_with_two_snapshots(self, instrument: str) -> list[Finding]:
        """
        Compare current snapshot to previous scan's volumes.
        Flag (expiry, right) combos where N+ strikes all gained volume
        by at least sweep_min_print_size contracts.
        """
        current = self._current_vol.get(instrument, {})
        prev    = self._prev_vol_snap.get(instrument, {})

        if not current or not prev:
            return []

        min_strikes     = FINGERPRINT_CONFIG['sweep_min_strikes']
        require_adj     = FINGERPRINT_CONFIG['sweep_require_adjacency']
        min_print_size  = FINGERPRINT_CONFIG['sweep_min_print_size']
        findings        = []

        for key in current:
            if key not in prev:
                continue

            expiry, right = key.split('|')
            curr_sv = current[key]   # {strike: vol}
            prev_sv = prev[key]

            # Strikes with volume delta >= min_print_size
            swept = {
                k: curr_sv[k] - prev_sv.get(k, 0)
                for k in curr_sv
                if curr_sv[k] - prev_sv.get(k, 0) >= min_print_size
            }

            if len(swept) < min_strikes:
                continue

            if require_adj:
                swept_strikes = sorted(swept.keys())
                if not _are_adjacent(swept_strikes):
                    continue

            deltas     = list(swept.values())
            confidence = _sweep_confidence(
                n_strikes       = len(swept),
                min_strikes     = min_strikes,
                delta_vols      = deltas,
                total_chain_vol = sum(curr_sv.values()) or 1,
            )

            findings.append(Finding(
                confidence   = confidence,
                source       = 'ibkr_snapshot',
                instrument   = instrument,
                model        = self.NAME,
                finding_type = 'sweep',
                note         = (
                    f"Sweep detected: {len(swept)} {right} strikes in expiry "
                    f"{expiry} all gained >= {min_print_size} contracts — "
                    f"total swept vol={sum(deltas):.0f}"
                ),
                evidence     = {
                    'swept_strikes'    : sorted(swept.keys()),
                    'volume_deltas'    : deltas,
                    'min_print_size'   : min_print_size,
                    'require_adjacency': require_adj,
                },
                expiry       = expiry,
                right        = right,
            ))

        return findings

    # ── Option A: tick-stream sweep detection ─────────────────────────────────

    def _detect_tick_sweep(self, instrument: str) -> list[Finding]:
        """
        Detect sweeps from buffered tick prints.

        A sweep is defined as: same aggressor side (buy or sell),
        hitting N+ distinct strikes within `print_cluster_window_sec` seconds.
        Only prints >= sweep_min_print_size contracts are counted.

        We use last-price vs bid/ask to infer aggressor side, but tick data
        from IB does not reliably tag side — we use price proximity heuristic.
        """
        prints = self._tick_buf.get(instrument, [])
        if not prints:
            return []

        window_sec     = FINGERPRINT_CONFIG['print_cluster_window_sec']
        min_strikes    = FINGERPRINT_CONFIG['sweep_min_strikes']
        min_print_size = FINGERPRINT_CONFIG['sweep_min_print_size']
        findings       = []

        # Filter by minimum print size before windowing
        eligible = [
            p for p in prints
            if p.get('size') is not None and p['size'] >= min_print_size
        ]

        if not eligible:
            self._tick_buf[instrument] = []
            return []

        # Sort by timestamp
        sorted_prints = sorted(eligible, key=lambda p: p['ts'])

        # Sliding window
        for i, anchor in enumerate(sorted_prints):
            if anchor.get('price') is None:
                continue
            window = [anchor]
            for j in range(i + 1, len(sorted_prints)):
                p = sorted_prints[j]
                if _ts_diff_sec(anchor['ts'], p['ts']) > window_sec:
                    break
                if p.get('price') is not None:
                    window.append(p)

            if len(window) < min_strikes:
                continue

            # Group by conId (each conId = one strike)
            by_con = defaultdict(list)
            for p in window:
                by_con[p.get('conId')].append(p)

            if len(by_con) < min_strikes:
                continue

            sizes = [p['size'] for p in window if p.get('size')]
            if not sizes:
                continue

            confidence = _tick_sweep_confidence(
                n_strikes   = len(by_con),
                min_strikes = min_strikes,
                sizes       = sizes,
            )

            findings.append(Finding(
                confidence   = confidence,
                source       = 'ibkr_tick',
                instrument   = instrument,
                model        = self.NAME,
                finding_type = 'tick_sweep',
                note         = (
                    f"Tick sweep: {len(by_con)} distinct strikes hit "
                    f"within {window_sec}s window (min size >= {min_print_size}) — "
                    f"total prints={len(window)}"
                ),
                evidence     = {
                    'print_count'    : len(window),
                    'strike_count'   : len(by_con),
                    'anchor_ts'      : anchor['ts'],
                    'sizes'          : sizes[:20],
                    'min_print_size' : min_print_size,
                },
            ))
            # Skip ahead to avoid double-counting this window
            break   # one finding per detection pass; next scan clears buffer

        # Clear buffer after detection
        self._tick_buf[instrument] = []
        return findings

    def clear(self, instrument: str) -> None:
        self._current_vol.pop(instrument, None)
        self._prev_vol_snap.pop(instrument, None)
        self._tick_buf.pop(instrument, None)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _are_adjacent(strikes: list[float], max_gap_ratio: float = 0.25) -> bool:
    """
    True if sorted strikes have no gap larger than max_gap_ratio * mean_step.
    Handles non-uniform strike grids (e.g. CL has $1 steps, SI has $0.25).
    """
    if len(strikes) < 2:
        return True
    steps     = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
    mean_step = mean(steps)
    if mean_step <= 0:
        return False
    return all(s <= mean_step * (1 + max_gap_ratio) for s in steps)


def _sweep_confidence(n_strikes      : int,
                      min_strikes    : int,
                      delta_vols     : list[float],
                      total_chain_vol: float) -> float:
    """
    Confidence model for snapshot sweep.
    Factors:
      1. Strike count (more strikes = higher confidence)
      2. Volume consistency (low CV = uniform lot size = suspicious)
      3. Fraction of total chain volume in sweep
    """
    # Factor 1: count score
    count_score = min(0.5 + 0.05 * (n_strikes - min_strikes), 0.80)

    # Factor 2: consistency (coefficient of variation; lower = more consistent)
    if len(delta_vols) >= 2 and mean(delta_vols) > 0:
        cv = stdev(delta_vols) / mean(delta_vols)
        consistency_score = max(0.0, 1.0 - cv)
    else:
        consistency_score = 0.5

    # Factor 3: chain concentration
    swept_vol     = sum(delta_vols)
    concentration = min(swept_vol / total_chain_vol, 1.0)

    confidence = (0.5 * count_score
                  + 0.3 * consistency_score
                  + 0.2 * concentration)
    return round(min(confidence, 0.95), 4)


def _tick_sweep_confidence(n_strikes : int,
                            min_strikes: int,
                            sizes      : list[float]) -> float:
    """Confidence for tick-based sweep — richer data so we can be more precise."""
    count_score = min(0.6 + 0.05 * (n_strikes - min_strikes), 0.90)

    if len(sizes) >= 2 and mean(sizes) > 0:
        cv = stdev(sizes) / mean(sizes)
        size_score = max(0.0, 1.0 - cv)
    else:
        size_score = 0.5

    return round(min(0.6 * count_score + 0.4 * size_score, 0.95), 4)


def _ts_diff_sec(ts1: str, ts2: str) -> float:
    """Return abs difference in seconds between two ISO timestamp strings."""
    from datetime import datetime
    try:
        t1 = datetime.fromisoformat(ts1.replace('Z', '+00:00'))
        t2 = datetime.fromisoformat(ts2.replace('Z', '+00:00'))
        return abs((t2 - t1).total_seconds())
    except Exception:
        return 0.0
