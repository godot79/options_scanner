"""
signals/fingerprint/oi_build.py
--------------------------------
Detects monotonically growing open interest over consecutive scans.

A sustained OI build at a specific strike/expiry suggests a large actor
is accumulating a position across multiple executions rather than a single
print — consistent with an institutional directional bet or hedge.

Data source : ibkr_snapshot (OI from daily snapshot)
Confidence  : scales with number of consecutive growing scans
              Base 0.5 at min_scans, +0.05 per additional scan, max 0.9
"""

from collections import defaultdict
from typing import Any

import pandas as pd

from options_scanner.config import FINGERPRINT_CONFIG
from options_scanner.signals.fingerprint.base import BaseFingerprintModel, Finding


class OIBuildModel(BaseFingerprintModel):

    NAME           = 'oi_build'
    accepts_source = ['ibkr_snapshot']

    def __init__(self):
        # history[instrument][key] = [oi_value, oi_value, ...]
        self._history: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    def update(self, instrument: str, data: Any) -> None:
        """data: pd.DataFrame with columns expiry, strike, right, openInterest"""
        if not isinstance(data, pd.DataFrame) or data.empty:
            return

        max_hist = FINGERPRINT_CONFIG['max_history_per_key']

        for _, row in data.iterrows():
            oi = row.get('openInterest')
            if oi is None or oi < 0:
                continue
            key = f"{row.get('expiry')}|{row.get('strike')}|{row.get('right')}"
            buf = self._history[instrument][key]
            buf.append(float(oi))
            if len(buf) > max_hist:
                self._history[instrument][key] = buf[-max_hist:]

    def detect(self, instrument: str) -> list[Finding]:
        min_scans = FINGERPRINT_CONFIG['oi_build_min_scans']
        findings  = []

        for key, series in self._history[instrument].items():
            if len(series) < min_scans:
                continue

            # Check last N scans for strict monotonic increase
            window = series[-min(len(series), min_scans + 5):]
            consec = _longest_monotonic_suffix(window)

            if consec < min_scans:
                continue

            confidence = min(0.5 + 0.05 * (consec - min_scans), 0.90)
            expiry, strike, right = key.split('|')

            findings.append(Finding(
                confidence   = confidence,
                source       = 'ibkr_snapshot',
                instrument   = instrument,
                model        = self.NAME,
                finding_type = 'oi_build',
                note         = (
                    f"OI growing for {consec} consecutive scans at "
                    f"{right} {strike} exp {expiry} — possible accumulation"
                ),
                evidence     = {
                    'oi_series'       : series[-consec:],
                    'consecutive_scans': consec,
                },
                expiry       = expiry,
                strike       = float(strike),
                right        = right,
            ))

        return findings

    def clear(self, instrument: str) -> None:
        self._history[instrument].clear()


def _longest_monotonic_suffix(series: list[float]) -> int:
    """
    Return the length of the longest strictly increasing suffix of `series`.
    Returns 0 if the suffix has no strictly increasing step.
    """
    if len(series) < 2:
        return 0
    count = 1
    for i in range(len(series) - 1, 0, -1):
        if series[i] > series[i - 1]:
            count += 1
        else:
            break
    # A suffix of length 1 means no increasing step was found
    return count if count > 1 else 0
