"""
signals/fingerprint/expiry_concentration.py
---------------------------------------------
Detects unusual concentration of volume in a single expiry.

Normal market activity distributes volume across expiries roughly in
proportion to open interest.  A sudden spike where one expiry accounts
for an anomalously large share of total chain volume suggests targeted
positioning — event-driven (earnings, macro data) or informed flow.

Concentration is measured as:
  expiry_volume / total_chain_volume

Threshold is configurable (default: 0.70 = 70% of all volume in one expiry).

Data source : ibkr_snapshot
Confidence  : scales with concentration ratio above threshold
"""

from collections import defaultdict
from typing import Any

import pandas as pd

from options_scanner.config import FINGERPRINT_CONFIG
from options_scanner.signals.fingerprint.base import BaseFingerprintModel, Finding


class ExpiryConcentrationModel(BaseFingerprintModel):

    NAME           = 'expiry_concentration'
    accepts_source = ['ibkr_snapshot']

    def __init__(self):
        # current[instrument] = DataFrame from last update
        self._current: dict[str, pd.DataFrame] = {}

    def update(self, instrument: str, data: Any) -> None:
        """data: pd.DataFrame with expiry, right, volume columns"""
        if isinstance(data, pd.DataFrame) and not data.empty:
            self._current[instrument] = data.copy()

    def detect(self, instrument: str) -> list[Finding]:
        df = self._current.get(instrument)
        if df is None or df.empty:
            return []

        threshold = FINGERPRINT_CONFIG['expiry_concentration_threshold']
        findings  = []

        # Aggregate volume by expiry and right
        df_vol = df.copy()
        df_vol['volume'] = df_vol['volume'].fillna(0)

        total_vol = df_vol['volume'].sum()
        if total_vol <= 0:
            return []

        # Check each (expiry, right) combination
        grouped = (
            df_vol.groupby(['expiry', 'right'])['volume']
            .sum()
            .reset_index()
        )

        for _, row in grouped.iterrows():
            exp_vol     = row['volume']
            expiry      = row['expiry']
            right       = row['right']
            concentration = exp_vol / total_vol

            if concentration < threshold:
                continue

            # Confidence: 0.5 at threshold, scales to 0.85 at 100%
            confidence = 0.5 + 0.35 * (
                (concentration - threshold) / (1.0 - threshold + 1e-9)
            )
            confidence = round(min(confidence, 0.85), 4)

            findings.append(Finding(
                confidence   = confidence,
                source       = 'ibkr_snapshot',
                instrument   = instrument,
                model        = self.NAME,
                finding_type = 'expiry_concentration',
                note         = (
                    f"Expiry concentration: {right} {expiry} accounts for "
                    f"{concentration*100:.1f}% of total chain volume "
                    f"({exp_vol:.0f} / {total_vol:.0f}) — "
                    f"possible event-driven positioning"
                ),
                evidence     = {
                    'expiry'       : expiry,
                    'right'        : right,
                    'expiry_vol'   : float(exp_vol),
                    'total_vol'    : float(total_vol),
                    'concentration': round(concentration, 4),
                },
                expiry       = str(expiry),
                right        = str(right),
            ))

        return findings

    def clear(self, instrument: str) -> None:
        self._current.pop(instrument, None)
