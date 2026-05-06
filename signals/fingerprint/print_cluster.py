"""
signals/fingerprint/print_cluster.py
--------------------------------------
Detects repeated same-size prints at the same strike within a short
time window.  Only meaningful with Option A (tick streaming) enabled.

Hypothesis: a large order being worked algorithmically in fixed-size
child orders leaves a repetitive print-size signature at one strike.
This is distinct from a sweep (which spans strikes) — this is
persistence at one level.

When USE_STREAMING_TICKS=False this model returns no findings since
snapshot data cannot distinguish individual prints at the same strike.

Data source : ibkr_tick  (Option A)
Confidence  : scales with print count and size consistency
"""

from collections import defaultdict
from statistics import mean, stdev
from typing import Any

import pandas as pd

from options_scanner.config import FINGERPRINT_CONFIG, USE_STREAMING_TICKS
from options_scanner.signals.fingerprint.base import BaseFingerprintModel, Finding


class PrintClusterModel(BaseFingerprintModel):

    NAME           = 'print_cluster'
    accepts_source = ['ibkr_tick']

    def __init__(self):
        # buffer[instrument] = list of print dicts
        self._buf: dict[str, list[dict]] = defaultdict(list)

    def update(self, instrument: str, data: Any) -> None:
        """data: list[dict] from TickStream.drain()"""
        if not USE_STREAMING_TICKS:
            return
        if not isinstance(data, list):
            return
        self._buf[instrument].extend(data)
        # Cap buffer
        max_buf = 10 * 60 * 100
        if len(self._buf[instrument]) > max_buf:
            self._buf[instrument] = self._buf[instrument][-max_buf:]

    def detect(self, instrument: str) -> list[Finding]:
        if not USE_STREAMING_TICKS:
            return []

        prints  = self._buf.get(instrument, [])
        if not prints:
            return []

        window_sec = FINGERPRINT_CONFIG['print_cluster_window_sec']
        min_prints = FINGERPRINT_CONFIG['print_cluster_min_prints']
        bucket     = FINGERPRINT_CONFIG['lot_size_bucket']
        findings   = []

        # Group by conId (= one strike)
        by_con: dict = defaultdict(list)
        for p in prints:
            cid = p.get('conId')
            if cid and p.get('size') and p.get('ts'):
                by_con[cid].append(p)

        for cid, con_prints in by_con.items():
            if len(con_prints) < min_prints:
                continue

            # Sort by time
            sorted_p = sorted(con_prints, key=lambda x: x['ts'])

            # Sliding window
            for i in range(len(sorted_p)):
                window = [sorted_p[i]]
                for j in range(i + 1, len(sorted_p)):
                    if _ts_diff_sec(sorted_p[i]['ts'], sorted_p[j]['ts']) > window_sec:
                        break
                    window.append(sorted_p[j])

                if len(window) < min_prints:
                    continue

                sizes       = [p['size'] for p in window if p.get('size')]
                bucketed    = [round(s / bucket) * bucket for s in sizes]
                most_common = max(set(bucketed), key=bucketed.count)
                matching    = bucketed.count(most_common)

                if matching < min_prints:
                    continue

                confidence = _cluster_confidence(matching, min_prints, sizes)

                findings.append(Finding(
                    confidence   = confidence,
                    source       = 'ibkr_tick',
                    instrument   = instrument,
                    model        = self.NAME,
                    finding_type = 'print_cluster',
                    note         = (
                        f"Print cluster: {matching} prints of ~{most_common} "
                        f"lots at conId={cid} within {window_sec}s — "
                        f"possible algo child-order pattern"
                    ),
                    evidence     = {
                        'conId'        : cid,
                        'print_count'  : matching,
                        'lot_size'     : most_common,
                        'anchor_ts'    : sorted_p[i]['ts'],
                        'sizes'        : sizes[:20],
                    },
                ))
                break  # one finding per conId per detect pass

        # Clear buffer after detection
        self._buf[instrument] = []
        return findings

    def clear(self, instrument: str) -> None:
        self._buf.pop(instrument, None)


def _cluster_confidence(matching    : int,
                         min_prints  : int,
                         sizes       : list[float]) -> float:
    count_score = min(0.5 + 0.05 * (matching - min_prints), 0.85)
    if len(sizes) >= 2 and mean(sizes) > 0:
        cv          = stdev(sizes) / mean(sizes)
        size_score  = max(0.0, 1.0 - cv)
    else:
        size_score  = 0.5
    return round(min(0.55 * count_score + 0.45 * size_score, 0.90), 4)


def _ts_diff_sec(ts1: str, ts2: str) -> float:
    from datetime import datetime
    try:
        t1 = datetime.fromisoformat(ts1.replace('Z', '+00:00'))
        t2 = datetime.fromisoformat(ts2.replace('Z', '+00:00'))
        return abs((t2 - t1).total_seconds())
    except Exception:
        return 0.0
