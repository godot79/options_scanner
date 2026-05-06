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

import asyncio
import sys
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


# ── Per-instrument loop ────────────────────────────────────────────────────────

async def run_instrument_loop(scanner      : InstrumentScanner,
                               stagger_sec  : float) -> None:
    """
    Staggered start then scan every SCAN_INTERVAL_SEC.
    Errors in individual scans are caught and logged; the loop continues.
    """
    await scanner.ib.sleep(stagger_sec)
    print(f"[{scanner.key}] Scan loop started "
          f"(interval={cfg.SCAN_INTERVAL_SEC}s, stagger={stagger_sec:.0f}s).")

    while True:
        try:
            await scanner.scan()
        except Exception as e:
            print(f"[{scanner.key}][ERROR] Scan failed: {e}")
        await scanner.ib.sleep(cfg.SCAN_INTERVAL_SEC)


# ── State persistence loop ────────────────────────────────────────────────────

async def persist_state_loop(ib        : IB,
                              scanners  : dict[str, InstrumentScanner]) -> None:
    """Save volume history every 5 scan intervals."""
    while True:
        await ib.sleep(cfg.SCAN_INTERVAL_SEC * 5)
        state = pack_vol_history(scanners)
        save_state(state)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("=" * 100)
    print("  OPTIONS MARKET SCANNER")
    print(f"  Instruments   : {', '.join(cfg.INSTRUMENTS.keys())}")
    print(f"  Scan interval : {cfg.SCAN_INTERVAL_SEC}s")
    print(f"  Expiry window : {cfg.MAX_EXPIRY_DAYS} days")
    print(f"  Moneyness     : ±{cfg.MONEYNESS_BAND*100:.0f}%")
    print(f"  Tick streaming: {'ON (Option A)' if cfg.USE_STREAMING_TICKS else 'OFF (Option B)'}")
    print("=" * 100)

    # ── Connect ───────────────────────────────────────────────────────────────
    ib = IB()
    if not connect(ib):
        sys.exit(1)

    # ── Load persistent state ─────────────────────────────────────────────────
    state   = load_state()

    # ── Shared components ─────────────────────────────────────────────────────
    alert_mgr   = AlertManager()
    fp_engine   = FingerprintEngine()   # shared across instruments
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
        )

    # ── Discover contracts (staggered to respect IB pacing) ──────────────────
    print("\nDiscovering contracts...")
    for i, (key, scanner) in enumerate(scanners.items()):
        await scanner.discover()
        if i < len(scanners) - 1:
            await ib.sleep(cfg.INSTRUMENT_STAGGER_SEC)

    # ── Launch async tasks ────────────────────────────────────────────────────
    tasks = []
    for i, (key, scanner) in enumerate(scanners.items()):
        stagger = i * cfg.INSTRUMENT_STAGGER_SEC
        tasks.append(asyncio.create_task(
            run_instrument_loop(scanner, stagger_sec=stagger)
        ))

    tasks.append(asyncio.create_task(persist_state_loop(ib, scanners)))

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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    util.startLoop()
    IB().run(main())
