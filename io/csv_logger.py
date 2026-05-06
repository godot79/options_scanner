"""
io/csv_logger.py
----------------
One CSV per instrument per calendar day.

Two file types per instrument:
  {INSTRUMENT}_{YYYYMMDD}.csv        : per-scan options chain rows + signal summary
  {INSTRUMENT}_{YYYYMMDD}_alerts.csv : alert events only (signal + fingerprint)

Designed so downstream tools (pandas, BI tools) can consume these directly.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from options_scanner.config import LOG_DIR


class CSVLogger:

    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._today        : dict[str, str]  = {}
        self._data_paths   : dict[str, Path] = {}
        self._alert_paths  : dict[str, Path] = {}

    # ── Path management ───────────────────────────────────────────────────────

    def _roll_date(self, instrument: str) -> None:
        today = datetime.now(timezone.utc).strftime('%Y%m%d')
        if self._today.get(instrument) != today:
            self._today[instrument]       = today
            self._data_paths[instrument]  = (
                LOG_DIR / f"{instrument}_{today}.csv"
            )
            self._alert_paths[instrument] = (
                LOG_DIR / f"{instrument}_{today}_alerts.csv"
            )

    def _data_path(self, instrument: str) -> Path:
        self._roll_date(instrument)
        return self._data_paths[instrument]

    def _alert_path(self, instrument: str) -> Path:
        self._roll_date(instrument)
        return self._alert_paths[instrument]

    # ── Scan data ─────────────────────────────────────────────────────────────

    def write_scan(self,
                   instrument    : str,
                   df            : pd.DataFrame,
                   signal_result,
                   underlying_price: Optional[float] = None) -> None:
        """
        Append current scan rows to the instrument's daily CSV.
        Adds metadata columns: instrument, scan_ts, signal_composite,
        pc_ratio, call_vol, put_vol, underlying_price.
        """
        if df.empty:
            return

        path       = self._data_path(instrument)
        write_hdr  = not path.exists()
        df_out     = df.copy()

        ts_str = datetime.now(timezone.utc).isoformat()
        df_out['instrument']       = instrument
        df_out['scan_ts']          = ts_str
        df_out['signal_composite'] = signal_result.composite
        df_out['pc_ratio']         = (
            round(signal_result.pc_ratio, 4)
            if signal_result.pc_ratio is not None else None
        )
        df_out['call_vol_total']   = signal_result.call_vol
        df_out['put_vol_total']    = signal_result.put_vol
        if underlying_price is not None:
            df_out['underlying_price'] = underlying_price

        try:
            df_out.to_csv(path, mode='a', header=write_hdr, index=False)
        except Exception as e:
            print(f"[CSV][WARN] Scan write failed for {instrument}: {e}")

    # ── Alert events ──────────────────────────────────────────────────────────

    def write_signal_alert(self,
                            instrument    : str,
                            composite     : str,
                            signal_result,
                            formatted_msg : str) -> None:
        self._write_alert_row(
            instrument   = instrument,
            alert_type   = 'SIGNAL',
            subtype      = composite,
            confidence   = None,
            detail       = str(signal_result.factors),
            formatted_msg= formatted_msg,
        )

    def write_fingerprint_alert(self,
                                 instrument    : str,
                                 finding,
                                 formatted_msg : str) -> None:
        self._write_alert_row(
            instrument    = instrument,
            alert_type    = 'FINGERPRINT',
            subtype       = finding.finding_type,
            confidence    = finding.confidence,
            detail        = finding.note,
            formatted_msg = formatted_msg,
        )

    def _write_alert_row(self,
                          instrument    : str,
                          alert_type    : str,
                          subtype       : str,
                          confidence    : Optional[float],
                          detail        : str,
                          formatted_msg : str) -> None:
        path      = self._alert_path(instrument)
        write_hdr = not path.exists()
        row       = pd.DataFrame([{
            'ts'           : datetime.now(timezone.utc).isoformat(),
            'instrument'   : instrument,
            'alert_type'   : alert_type,
            'subtype'      : subtype,
            'confidence'   : confidence,
            'detail'       : detail,
            'formatted_msg': formatted_msg,
        }])
        try:
            row.to_csv(path, mode='a', header=write_hdr, index=False)
        except Exception as e:
            print(f"[CSV][WARN] Alert write failed for {instrument}: {e}")
