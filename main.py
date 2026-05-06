"""
main.py
-------
Orchestrates all instrument scanners and runs the main async event loop.

Startup sequence:
  1. Connect to IB Gateway / TWS
  2. Load persistent state (volume history, fingerprint)
  3. Discover contracts for each instrument (staggered)
  4. Launch parallel async scan loops + state persistence task
  5. On KeyboardInterrupt: clean shutdown, save final state, disconnect

To run:
    python -m options_scanner.main
or:
    python main.py   (from options_scanner/ directory)
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# ── Path bootstrap ────────────────────────────────────────────────────────────
# Ensures imports work whether run as:
#   python main.py                 (from inside options_scanner/)
#   python options_scanner/main.py (from parent directory)
#   python -m options_scanner.main (as a module)
_HERE   = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _p in (_PARENT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import asyncio
from datetime import datetime, timezone

from ib_insync import IB, util

import options_scanner.config as cfg
from options_scanner.data.ib_client import connect, disconnect
from options_scanner.signals.volume_history import VolumeHistory
from options_scanner.signals.fingerprint import FingerprintEngine
from options_scanner.io.alerts import AlertManager
from options_scanner.io.csv_logger import CSVLogger
from options_scanner.io.state import (
    load_state,
    save_state,
    extract_vol_history,
    pack_vol_history,
)
from options_scanner.scanner.instrument import InstrumentScanner
from options_scanner.data.contract_cache import CacheManager, write_refresh_request


# ── Per-instrument loop ────────────────────────────────────────────────────────

async def run_instrument_loop(scanner      : InstrumentScanner,
                               stagger_sec  : float) -> None:
    """
    Staggered start then scan every SCAN_INTERVAL_SEC.
    Errors in individual scans are caught and logged; the loop continues.
    """
    await asyncio.sleep(stagger_sec)
    print(f"[{scanner.key}] Scan loop started "
          f"(interval={cfg.SCAN_INTERVAL_SEC}s, stagger={stagger_sec:.0f}s).")

    while True:
        try:
            await scanner.scan()
        except Exception as e:
            print(f"[{scanner.key}][ERROR] Scan failed: {e}")
        await asyncio.sleep(cfg.SCAN_INTERVAL_SEC)


# ── State persistence loop ────────────────────────────────────────────────────

async def persist_state_loop(ib        : IB,
                              scanners  : dict[str, InstrumentScanner]) -> None:
    """Save volume history every 5 scan intervals."""
    while True:
        await asyncio.sleep(cfg.SCAN_INTERVAL_SEC * 5)
        state = pack_vol_history(scanners)
        save_state(state)


# ── Main ──────────────────────────────────────────────────────────────────────

def _suppress_ib_console_noise() -> None:
    """
    Redirect ib_insync Error 200 / contract noise from console to a separate
    qualify_errors.log file.

    qualifyContractsAsync fires reqContractDetailsAsync for every contract.
    IB returns Error 200 for strikes/expiries it doesn't list as active
    (e.g. TSLA LEAPS with 2.5pt strike spacing).  ib_insync logs these via
    the 'ib_insync.wrapper' logger at WARNING level, which by default prints
    to stderr and floods the terminal.

    Fix:
    - Remove stderr handler from those loggers (stops console spam)
    - Add a rotating file handler → LOG_DIR/qualify_errors.log
      so the raw Error 200 messages are still available for debugging
    - ib_insync.wrapper stays at WARNING so genuine warnings are kept
    """
    import logging as _logging
    from logging.handlers import RotatingFileHandler as _RFH

    cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
    err_path = cfg.LOG_DIR / 'qualify_errors.log'

    file_handler = _RFH(
        err_path,
        maxBytes    = 5 * 1024 * 1024,   # 5 MB
        backupCount = 2,
        encoding    = 'utf-8',
    )
    file_handler.setFormatter(_logging.Formatter(
        '%(asctime)s  %(name)s  %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    file_handler.setLevel(_logging.WARNING)

    for name in ('ib_insync.wrapper', 'ib_insync.client', 'ib_insync.ib'):
        lgr = _logging.getLogger(name)
        # Remove any existing stderr/stream handlers
        lgr.handlers = [h for h in lgr.handlers
                        if not isinstance(h, _logging.StreamHandler)
                        or isinstance(h, _RFH)]
        lgr.addHandler(file_handler)
        lgr.propagate = False   # don't bubble up to root (avoids double-logging)


def _setup_logging() -> None:
    """
    Tee all print() output to both stdout and a rotating log file.

    File    : LOG_DIR/scanner.log
    Rotation: 10 MB per file, keep 5 files (= up to 50 MB of history)
    Format  : 2026-04-28 14:23:01  [message]

    Uses a Tee on sys.stdout so every print() call goes to both the
    terminal and the log file. No existing print() calls need changing.
    """
    cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = cfg.LOG_DIR / 'scanner.log'

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes    = 10 * 1024 * 1024,  # 10 MB per file
        backupCount = 5,                  # keep scanner.log.1 .. .5
        encoding    = 'utf-8',
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s  %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))

    class _Tee:
        """Write to both original stdout and the rotating log file."""
        def __init__(self, stream, log_fn):
            self._stream = stream
            self._log    = log_fn
            self._buf    = ''

        def write(self, text: str) -> None:
            self._stream.write(text)
            self._buf += text
            while '\n' in self._buf:
                line, self._buf = self._buf.split('\n', 1)
                if line.strip():
                    self._log(line)

        def flush(self) -> None:
            self._stream.flush()

        def __getattr__(self, name):
            return getattr(self._stream, name)

    def _emit(msg: str) -> None:
        record = logging.makeLogRecord({
            'msg': msg, 'levelno': logging.INFO, 'levelname': 'INFO',
        })
        file_handler.emit(record)

    sys.stdout = _Tee(sys.stdout, _emit)
    print(f"[LOG] Logging to {log_path} (10MB × 5 = 50MB history)")


async def main(args=None) -> None:
    _suppress_ib_console_noise()
    print("=" * 100)
    print("  OPTIONS MARKET SCANNER")
    print(f"  Instruments   : {', '.join(cfg.INSTRUMENTS.keys())}")
    print(f"  Scan interval : {cfg.SCAN_INTERVAL_SEC}s")
    print(f"  Expiry window : {cfg.MAX_EXPIRY_DAYS} days")
    print(f"  Moneyness     : ±{cfg.MONEYNESS_BAND*100:.0f}%")
    print(f"  Tick streaming: {'ON (Option A)' if cfg.USE_STREAMING_TICKS else 'OFF (Option B)'}")
    print("=" * 100)

    _setup_logging()

    # Resolve CLI flags
    force_refresh  = bool(args and (args.refresh or args.refresh_instrument))
    exit_after     = bool(args and args.exit_after_cache)
    refresh_keys   = list(args.refresh_instrument) if args else []

    # ── Connect ───────────────────────────────────────────────────────────────
    ib = IB()
    if not connect(ib):
        sys.exit(1)

    # ── Cache initialisation ──────────────────────────────────────────────────
    # For per-instrument refresh flags, write a trigger file and let
    # CacheManager handle it during initialise().
    cache_mgr = CacheManager(ib)
    if refresh_keys:
        write_refresh_request(refresh_keys)
    await cache_mgr.initialise(force_refresh=force_refresh)

    if exit_after:
        print("[MAIN] Cache refreshed. Exiting (--exit-after-cache).")
        disconnect(ib)
        return

    # ── Load persistent state ─────────────────────────────────────────────────
    state   = load_state()

    # ── Shared components ─────────────────────────────────────────────────────
    alert_mgr   = AlertManager()
    fp_engine   = FingerprintEngine()
    csv_logger  = CSVLogger()

    # ── Build scanners ────────────────────────────────────────────────────────
    scanners: dict[str, InstrumentScanner] = {}

    for key, inst_cfg in cfg.INSTRUMENTS.items():
        vol_hist = VolumeHistory.from_list(
            instrument = key,
            data       = extract_vol_history(state, key),
        )
        scanners[key] = InstrumentScanner(
            ib            = ib,
            key           = key,
            cfg_inst      = inst_cfg,
            vol_history   = vol_hist,
            alert_manager = alert_mgr,
            fp_engine     = fp_engine,
            csv_logger    = csv_logger,
            cache_manager = cache_mgr,
        )

    # ── Load contracts from cache into each scanner ───────────────────────────
    print("\nLoading contracts from cache...")
    for key, scanner in scanners.items():
        await scanner.discover()

    # ── Launch async tasks ────────────────────────────────────────────────────
    tasks = []
    for i, (key, scanner) in enumerate(scanners.items()):
        stagger = i * cfg.INSTRUMENT_STAGGER_SEC
        tasks.append(asyncio.create_task(
            run_instrument_loop(scanner, stagger_sec=stagger)
        ))

    tasks.append(asyncio.create_task(persist_state_loop(ib, scanners)))
    tasks.append(asyncio.create_task(cache_mgr.run_background()))

    print(f"\nAll {len(scanners)} scan loops active. Press Ctrl+C to stop.\n")

    try:
        await asyncio.gather(*tasks)
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("\n[MAIN] Shutdown requested.")
    finally:
        # Cancel remaining tasks cleanly
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        # Stop tick streams if active
        for scanner in scanners.values():
            scanner.stop_tick_streams()

        # Save final state
        save_state(pack_vol_history(scanners))
        print("[MAIN] State saved.")

        disconnect(ib)
        print("[MAIN] Done.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args():
    import argparse
    p = argparse.ArgumentParser(description='Options Market Scanner')
    p.add_argument(
        '--refresh', action='store_true',
        help='Force re-discovery of all contracts at startup, overwriting cache'
    )
    p.add_argument(
        '--refresh-instrument', metavar='KEY', action='append', default=[],
        help='Force refresh for a specific instrument (e.g. --refresh-instrument CL). '
             'Can be repeated.'
    )
    p.add_argument(
        '--exit-after-cache', action='store_true',
        help='Discover contracts, save cache, then exit without scanning. '
             'Designed for cron usage.'
    )
    p.add_argument(
        '--request-refresh', metavar='KEY', action='append', default=[],
        help='Write a manual refresh request for a running scanner instance '
             '(e.g. --request-refresh SI). Use ALL for all instruments.'
    )
    return p.parse_args()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    args = _parse_args()

    # --request-refresh: write trigger file and exit (targets a running scanner)
    if args.request_refresh:
        targets = args.request_refresh
        write_refresh_request(None if 'ALL' in targets else targets)
        print(f"[CACHE] Refresh request written for: {targets}")
        import sys; sys.exit(0)

    util.startLoop()
    IB().run(main(args))
