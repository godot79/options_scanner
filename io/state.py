"""
io/state.py
-----------
JSON persistence for scanner state.

Two files:
  scanner_state.json    : volume history per instrument (for rolling avg)
  trade_fingerprint.json: fingerprint model history per instrument

Both are loaded on startup and saved periodically + on clean shutdown.
Corrupt / missing files are handled gracefully — scanner starts fresh.
"""

import json
from pathlib import Path
from typing import Any

from options_scanner.config import STATE_FILE, FINGERPRINT_FILE, LOG_DIR


def _ensure_log_dir() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


# ── Scanner state (volume history) ───────────────────────────────────────────

def load_state() -> dict:
    """Load scanner state.  Returns empty dict on failure."""
    _ensure_log_dir()
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        print(f"[STATE][WARN] Could not load state file: {e}")
        return {}


def save_state(state: dict) -> None:
    _ensure_log_dir()
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        print(f"[STATE][WARN] Could not save state file: {e}")


# ── Fingerprint persistence ───────────────────────────────────────────────────

def load_fingerprint() -> dict:
    """Load fingerprint history.  Returns empty dict on failure."""
    _ensure_log_dir()
    if not FINGERPRINT_FILE.exists():
        return {}
    try:
        with open(FINGERPRINT_FILE, 'r') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        print(f"[STATE][WARN] Could not load fingerprint file: {e}")
        return {}


def save_fingerprint(data: dict) -> None:
    _ensure_log_dir()
    try:
        with open(FINGERPRINT_FILE, 'w') as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        print(f"[STATE][WARN] Could not save fingerprint file: {e}")


# ── Convenience: extract / pack volume history ───────────────────────────────

def extract_vol_history(state: dict, instrument: str) -> list[dict]:
    """Pull volume history list for one instrument from loaded state dict."""
    return state.get('vol_history', {}).get(instrument, [])


def pack_vol_history(scanners: dict) -> dict:
    """
    Build a state dict from a mapping of {instrument: InstrumentScanner}.
    Caller passes scanners dict; this module stays decoupled from scanner.
    """
    return {
        'vol_history': {
            key: scanner.vol_history.to_list()
            for key, scanner in scanners.items()
        }
    }
