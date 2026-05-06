"""
signals/fingerprint/engine.py
------------------------------
Aggregates all fingerprint models into a single interface.

Responsibilities:
  1. Calls update() and detect() on every registered model
  2. Deduplicates findings that refer to the same (instrument, expiry, strike, right)
     and applies a corroboration bonus when multiple models agree
  3. Filters to findings above alert_threshold after bonuses
  4. Persists history to JSON via io.state
  5. Exposes a clean list[Finding] to the caller

Adding a new model: instantiate it in FINGERPRINT_MODELS and nothing else changes.
"""

from collections import defaultdict
from typing import Any

import pandas as pd

from options_scanner.config import FINGERPRINT_CONFIG
from options_scanner.signals.fingerprint.base import BaseFingerprintModel, Finding
from options_scanner.signals.fingerprint.oi_build import OIBuildModel
from options_scanner.signals.fingerprint.volume_cluster import VolumeClusterModel
from options_scanner.signals.fingerprint.sweep_detector import SweepDetectorModel
from options_scanner.signals.fingerprint.print_cluster import PrintClusterModel
from options_scanner.signals.fingerprint.expiry_concentration import ExpiryConcentrationModel

# ── Registry: add new fingerprint models here ─────────────────────────────────
FINGERPRINT_MODELS: list[BaseFingerprintModel] = [
    OIBuildModel(),
    VolumeClusterModel(),
    SweepDetectorModel(),
    PrintClusterModel(),
    ExpiryConcentrationModel(),
]


class FingerprintEngine:
    """
    Runs all registered fingerprint models and aggregates their findings.

    Usage:
        engine = FingerprintEngine()
        engine.update(instrument, snapshot_df, tick_prints)
        findings = engine.detect(instrument)
    """

    def __init__(self, models: list[BaseFingerprintModel] | None = None):
        self._models = models if models is not None else FINGERPRINT_MODELS

    # ── Update ────────────────────────────────────────────────────────────────

    def update(self,
               instrument   : str,
               snapshot_df  : pd.DataFrame,
               tick_prints  : list[dict] | None = None) -> None:
        """
        Feed data to all models.
        snapshot_df : options chain DataFrame from current scan
        tick_prints : list of tick dicts from TickStream.drain() (may be None)
        """
        for model in self._models:
            if 'ibkr_snapshot' in model.accepts_source:
                model.update(instrument, snapshot_df)
            if tick_prints is not None and 'ibkr_tick' in model.accepts_source:
                model.update(instrument, tick_prints)

    # ── Detect ────────────────────────────────────────────────────────────────

    def detect(self, instrument: str) -> list[Finding]:
        """
        Run all models, apply corroboration bonus, filter by threshold.
        Returns findings sorted by confidence descending.
        """
        min_conf    = FINGERPRINT_CONFIG['min_confidence']
        alert_thr   = FINGERPRINT_CONFIG['alert_threshold']
        cor_bonus   = FINGERPRINT_CONFIG['corroboration_bonus']

        raw_findings: list[Finding] = []
        for model in self._models:
            try:
                findings = model.detect(instrument)
                raw_findings.extend(findings)
            except Exception as e:
                print(f"[FP][WARN] {model.NAME} detect() failed for "
                      f"{instrument}: {e}")

        # Apply minimum confidence filter
        raw_findings = [f for f in raw_findings if f.confidence >= min_conf]

        if not raw_findings:
            return []

        # Corroboration: group by (expiry, strike, right) where not None
        # and bonus confidence for each additional model that agrees
        key_groups: dict[tuple, list[Finding]] = defaultdict(list)
        ungrouped  : list[Finding]             = []

        for f in raw_findings:
            if f.expiry is not None and f.strike is not None and f.right is not None:
                gk = (f.expiry, f.strike, f.right)
                key_groups[gk].append(f)
            else:
                ungrouped.append(f)

        boosted: list[Finding] = []

        for gk, group in key_groups.items():
            n_models = len({f.model for f in group})
            bonus    = cor_bonus * max(0, n_models - 1)
            for f in group:
                new_conf = round(min(f.confidence + bonus, 1.0), 4)
                # dataclass is frozen so create new instance
                boosted.append(_replace_confidence(f, new_conf))

        for f in ungrouped:
            boosted.append(f)

        # Final filter at alert threshold
        result = [f for f in boosted if f.confidence >= alert_thr]
        result.sort(key=lambda f: f.confidence, reverse=True)
        return result

    # ── Clear ─────────────────────────────────────────────────────────────────

    def clear(self, instrument: str) -> None:
        for model in self._models:
            model.clear(instrument)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _replace_confidence(f: Finding, new_conf: float) -> Finding:
    """Return a new Finding with updated confidence (frozen dataclass)."""
    return Finding(
        confidence   = new_conf,
        source       = f.source,
        instrument   = f.instrument,
        model        = f.model,
        finding_type = f.finding_type,
        note         = f.note,
        evidence     = f.evidence,
        expiry       = f.expiry,
        strike       = f.strike,
        right        = f.right,
    )
