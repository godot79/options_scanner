"""
signals/volume_history.py
--------------------------
Tracks per-instrument aggregate call/put volume across scan cycles.
Used to compute rolling averages for volume anomaly detection.

State is serialisable to/from plain dicts for JSON persistence.
"""

from datetime import datetime, timezone
from typing import Optional

from options_scanner.config import SCAN_INTERVAL_SEC, SIGNAL_CONFIG


class VolumeHistory:
    """
    Rolling call/put volume history for one instrument.

    Records are stored as list[dict] so they survive JSON round-trips.
    Maximum records retained = rolling_days * scans_per_day.
    """

    def __init__(self, instrument: str, history: list[dict] | None = None):
        self.instrument = instrument
        self._records   : list[dict] = history or []

    # ── Mutation ──────────────────────────────────────────────────────────────

    def record(self, call_vol: float, put_vol: float) -> None:
        self._records.append({
            'ts'      : datetime.now(timezone.utc).isoformat(),
            'call_vol': float(call_vol),
            'put_vol' : float(put_vol),
        })
        self._trim()

    def _trim(self) -> None:
        max_records = self._max_records()
        if len(self._records) > max_records:
            self._records = self._records[-max_records:]

    def _max_records(self) -> int:
        rolling_days  = SIGNAL_CONFIG['volume_rolling_days']
        scans_per_day = max(1, 86_400 // max(1, SCAN_INTERVAL_SEC))
        return rolling_days * scans_per_day

    # ── Query ─────────────────────────────────────────────────────────────────

    def rolling_avg(self) -> dict[str, float]:
        """
        Return {'call': avg, 'put': avg} over the configured rolling window,
        excluding the most recent record (current scan, not yet historical).
        """
        if len(self._records) < 2:
            return {'call': 0.0, 'put': 0.0}

        # Exclude last record (current scan)
        window = self._records[:-1]
        n      = len(window)
        avg_c  = sum(r['call_vol'] for r in window) / n
        avg_p  = sum(r['put_vol']  for r in window) / n
        return {'call': avg_c, 'put': avg_p}

    def latest(self) -> Optional[dict]:
        return self._records[-1] if self._records else None

    def __len__(self) -> int:
        return len(self._records)

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_list(self) -> list[dict]:
        return list(self._records)

    @classmethod
    def from_list(cls, instrument: str, data: list[dict]) -> 'VolumeHistory':
        return cls(instrument=instrument, history=data)
