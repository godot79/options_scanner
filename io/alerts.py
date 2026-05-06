"""
io/alerts.py
------------
Alert suppression and formatting.

Tracks last-fired scan index per (instrument, signal_key).
Suppresses re-alert for ALERT_SUPPRESSION_SCANS scans after first fire.
When signal clears, suppression resets so it will fire again on next occurrence.

Structured so console output and log output share the same formatted string —
a future notification layer (email, webhook, etc.) just needs to subscribe
to the same formatted alert string.
"""

from datetime import datetime, timezone
from typing import Optional

from options_scanner.config import ALERT_SUPPRESSION_SCANS


class AlertManager:
    """
    Manages alert suppression per (instrument, signal_key).

    scan_count  : incremented once per scan per instrument
    last_fired  : maps (instrument, signal_key) -> scan count at last fire
    """

    def __init__(self):
        self._scan_count : dict[str, int]          = {}
        self._last_fired : dict[tuple[str,str], int] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def tick(self, instrument: str) -> None:
        """Call once at the start of each scan cycle for an instrument."""
        self._scan_count[instrument] = self._scan_count.get(instrument, 0) + 1

    def _scan_n(self, instrument: str) -> int:
        return self._scan_count.get(instrument, 0)

    # ── Suppression logic ─────────────────────────────────────────────────────

    def should_fire(self, instrument: str, signal_key: str) -> bool:
        """True if the alert has not fired recently."""
        k    = (instrument, signal_key)
        now  = self._scan_n(instrument)
        last = self._last_fired.get(k, -(ALERT_SUPPRESSION_SCANS + 1))
        return (now - last) > ALERT_SUPPRESSION_SCANS

    def mark_fired(self, instrument: str, signal_key: str) -> None:
        self._last_fired[(instrument, signal_key)] = self._scan_n(instrument)

    def clear(self, instrument: str, signal_key: str) -> None:
        """Reset suppression — call when signal is no longer active."""
        self._last_fired.pop((instrument, signal_key), None)

    def clear_all(self, instrument: str) -> None:
        keys = [k for k in self._last_fired if k[0] == instrument]
        for k in keys:
            del self._last_fired[k]

    # ── Formatting ────────────────────────────────────────────────────────────

    @staticmethod
    def format_signal_alert(instrument : str,
                             composite  : str,
                             signal_result) -> str:
        ts    = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        pc    = f"{signal_result.pc_ratio:.2f}" if signal_result.pc_ratio else 'N/A'
        lines = [
            f"[SIGNAL ALERT] {ts}",
            f"  Instrument : {instrument}",
            f"  Signal     : {composite}",
            f"  P/C Ratio  : {pc}",
            f"  Call Vol   : {signal_result.call_vol:.0f}",
            f"  Put Vol    : {signal_result.put_vol:.0f}",
            f"  Factors    :",
        ]
        for factor, direction in signal_result.factors.items():
            lines.append(f"    {factor:<22s}: {direction or 'neutral'}")
        return '\n'.join(lines)

    @staticmethod
    def format_fingerprint_alert(finding) -> str:
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        lines = [
            f"[FINGERPRINT ALERT] {ts}",
            f"  Instrument : {finding.instrument}",
            f"  Type       : {finding.finding_type}",
            f"  Model      : {finding.model}",
            f"  Confidence : {finding.confidence:.2f}",
            f"  Source     : {finding.source}",
            f"  Note       : {finding.note}",
        ]
        if finding.expiry:
            lines.append(f"  Expiry     : {finding.expiry}")
        if finding.strike:
            lines.append(f"  Strike     : {finding.strike}")
        if finding.right:
            lines.append(f"  Right      : {finding.right}")
        return '\n'.join(lines)
