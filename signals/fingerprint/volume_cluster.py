"""
signals/fingerprint/volume_cluster.py
---------------------------------------
Detects the same lot-size bucket appearing at multiple strikes simultaneously,
with filters to distinguish institutional flow from retail background noise.

Hypothesis: a single actor distributing size across strikes to reduce
market impact will leave a "lot-size fingerprint" — the same rounded
volume appearing at several strikes in the same scan cycle.

Retail noise filter: retail traders also use round lots, but their activity
is spread across many expiries and both directions (C+P) simultaneously.
Institutional flow concentrates in:
  - One expiry (or at most two adjacent ones)
  - One direction (calls OR puts, not both)
  - A focused strike band

Confidence scoring:
  - Base: strike count above threshold
  - Bonus: high expiry concentration (most volume in ≤2 expiries)
  - Bonus: directional (calls only OR puts only)
  - Penalty: spread across many expiries and both directions (retail pattern)

Data source : ibkr_snapshot
"""

from collections import defaultdict
from typing import Any

import pandas as pd

from options_scanner.config import FINGERPRINT_CONFIG
from options_scanner.signals.fingerprint.base import BaseFingerprintModel, Finding


class VolumeClusterModel(BaseFingerprintModel):

    NAME           = 'volume_cluster'
    accepts_source = ['ibkr_snapshot']

    def __init__(self):
        # Store last-seen volume per key per instrument for current scan cycle
        self._current: dict[str, dict[str, float]] = defaultdict(dict)

    def update(self, instrument: str, data: Any) -> None:
        """data: pd.DataFrame with expiry, strike, right, volume"""
        if not isinstance(data, pd.DataFrame) or data.empty:
            return

        snapshot: dict[str, float] = {}
        for _, row in data.iterrows():
            vol = row.get('volume')
            if vol is None or vol <= 0:
                continue
            key = f"{row.get('expiry')}|{row.get('strike')}|{row.get('right')}"
            snapshot[key] = float(vol)

        self._current[instrument] = snapshot

    def detect(self, instrument: str) -> list[Finding]:
        snapshot = self._current.get(instrument, {})
        if not snapshot:
            return []

        bucket_size = FINGERPRINT_CONFIG['lot_size_bucket']
        min_strikes = FINGERPRINT_CONFIG['volume_cluster_min_strikes']

        # Bucket volumes and group keys by bucket
        buckets: dict[int, list[str]] = defaultdict(list)
        for key, vol in snapshot.items():
            try:
                if vol is None or not (vol == vol):  # NaN check
                    continue
                fvol = float(vol)
                if not (0 < fvol < 1e12):
                    continue
                bucket = round(fvol / bucket_size) * bucket_size
                if bucket > 0:
                    buckets[bucket].append(key)
            except (TypeError, ValueError, OverflowError):
                continue

        findings = []
        for lot_size, keys in buckets.items():
            if len(keys) < min_strikes:
                continue

            expiries    = sorted({k.split('|')[0] for k in keys})
            rights      = sorted({k.split('|')[2] for k in keys})
            n_expiries  = len(expiries)
            n_rights    = len(rights)
            n_strikes   = len(keys)

            # ── Retail noise filter ───────────────────────────────────────
            # Retail pattern: spread across many expiries AND both directions
            # Skip if spread across >2 expiries AND both C+P present — this
            # is background retail noise, not a single actor
            is_retail_noise = (n_expiries > 2 and n_rights == 2)
            if is_retail_noise:
                continue

            # ── Confidence scoring ────────────────────────────────────────
            # Base: strike count
            base = min(0.50 + 0.05 * (n_strikes - min_strikes), 0.75)

            # Bonus: expiry concentration (fewer expiries = more focused)
            expiry_bonus = max(0.0, 0.10 * (3 - n_expiries) / 2)

            # Bonus: directional (one side only)
            direction_bonus = 0.10 if n_rights == 1 else 0.0

            confidence = min(base + expiry_bonus + direction_bonus, 0.95)

            # Classify the pattern for the note
            if n_rights == 1 and n_expiries == 1:
                pattern = 'focused single-expiry single-direction sweep'
            elif n_rights == 1:
                pattern = 'directional multi-expiry accumulation'
            elif n_expiries <= 2:
                pattern = 'concentrated two-sided positioning'
            else:
                pattern = 'possible single-actor distribution'

            findings.append(Finding(
                confidence   = confidence,
                source       = 'ibkr_snapshot',
                instrument   = instrument,
                model        = self.NAME,
                finding_type = 'volume_cluster',
                note         = (
                    f"Lot-size ~{lot_size} repeated across {n_strikes} strikes "
                    f"(expiries: {expiries}, rights: {rights}) — {pattern}"
                ),
                evidence     = {
                    'lot_size'         : lot_size,
                    'strike_count'     : n_strikes,
                    'expiry_count'     : n_expiries,
                    'direction_count'  : n_rights,
                    'pattern'          : pattern,
                    'keys'             : keys[:20],
                },
            ))

        return findings

    def clear(self, instrument: str) -> None:
        self._current.pop(instrument, None)
