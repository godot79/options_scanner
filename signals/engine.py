"""
signals/engine.py
-----------------
Evaluates the four directional signal factors and combines them via AND logic.

Factors:
  1. put_call_ratio       : aggregate put/call volume ratio
  2. iv_skew              : avg(put IV) - avg(call IV)
  3. delta_weighted_pc    : delta-weighted put/call volume ratio
  4. volume_anomaly       : current vol vs rolling average

AND logic: all non-null factors must agree on direction ('bullish'/'bearish').
Composite signal strength:
  STRONG_BULLISH / STRONG_BEARISH : all 4 factors agree
  BULLISH / BEARISH               : 3 factors agree, 1 is None
  None                            : mixed or insufficient data

This module is stateless — it receives a DataFrame and a VolumeHistory
and returns a SignalResult.  All state lives in VolumeHistory.
"""

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from options_scanner.config import SIGNAL_CONFIG
from options_scanner.signals.volume_history import VolumeHistory


# ── Result container ──────────────────────────────────────────────────────────

@dataclass
class SignalResult:
    composite         : Optional[str]          # e.g. 'STRONG_BEARISH' or None
    factors           : dict[str, Optional[str]]  # factor name -> direction/None
    call_vol          : float
    put_vol           : float
    pc_ratio          : Optional[float]
    factors_active    : int                    # count of non-None factors


# ── Factor evaluators ─────────────────────────────────────────────────────────

def _pc_ratio_direction(call_vol: float, put_vol: float) -> Optional[str]:
    if call_vol <= 0 and put_vol <= 0:
        return None
    if call_vol <= 0:
        return 'bearish'
    ratio = put_vol / call_vol
    if ratio > SIGNAL_CONFIG['pc_ratio_bearish_threshold']:
        return 'bearish'
    if ratio < SIGNAL_CONFIG['pc_ratio_bullish_threshold']:
        return 'bullish'
    return None


def _iv_skew_direction(df: pd.DataFrame) -> Optional[str]:
    # Filter to near-the-money options before averaging IV.
    # Deep ITM/OTM IV is numerically unstable and carries no skew information:
    # - Deep ITM calls have IV ~intrinsic/time-value ratio → meaninglessly high
    # - Far OTM options have very low vega → IV solve is noisy
    # Standard practice: use |delta| 0.15–0.85 as the ATM proxy.
    # This requires the 'delta' column to be populated; if it isn't (e.g. first
    # scan before IV has run), fall back to the full set to avoid returning None
    # when we do have some IV data.
    has_delta = 'delta' in df.columns and df['delta'].notna().any()
    if has_delta:
        atm = df[df['delta'].abs().between(
            SIGNAL_CONFIG['iv_skew_delta_lo'],
            SIGNAL_CONFIG['iv_skew_delta_hi'],
        )]
    else:
        atm = df

    calls = atm[atm['right'] == 'C']['iv'].dropna()
    puts  = atm[atm['right'] == 'P']['iv'].dropna()
    if calls.empty or puts.empty:
        return None
    skew = float(puts.mean()) - float(calls.mean())
    if skew > SIGNAL_CONFIG['iv_skew_bearish_threshold']:
        return 'bearish'
    if skew < SIGNAL_CONFIG['iv_skew_bullish_threshold']:
        return 'bullish'
    return None


def _delta_weighted_pc_direction(df: pd.DataFrame) -> Optional[str]:
    sub = df.dropna(subset=['delta', 'volume'])
    sub = sub[sub['volume'] > 0].copy()
    if sub.empty:
        return None

    calls   = sub[sub['right'] == 'C']
    puts    = sub[sub['right'] == 'P']
    dw_call = float((calls['delta'].abs() * calls['volume']).sum())
    dw_put  = float((puts['delta'].abs()  * puts['volume']).sum())

    total = dw_call + dw_put
    if total <= 0:
        return None

    if dw_call <= 0:
        return 'bearish'

    ratio = dw_put / dw_call
    if ratio > SIGNAL_CONFIG['dw_pc_ratio_bearish_threshold']:
        return 'bearish'
    if ratio < SIGNAL_CONFIG['dw_pc_ratio_bullish_threshold']:
        return 'bullish'
    return None


def _volume_anomaly_direction(call_vol  : float,
                               put_vol   : float,
                               vol_history: VolumeHistory) -> Optional[str]:
    avgs = vol_history.rolling_avg()
    mult = SIGNAL_CONFIG['volume_anomaly_multiplier']

    # Only flag if rolling average is non-trivial (avoids false positives at startup)
    call_anom = (avgs['call'] > 0 and call_vol > avgs['call'] * mult)
    put_anom  = (avgs['put']  > 0 and put_vol  > avgs['put']  * mult)

    if call_anom and not put_anom:
        return 'bullish'
    if put_anom and not call_anom:
        return 'bearish'
    return None


# ── Composite logic ───────────────────────────────────────────────────────────

def _composite(factors: dict[str, Optional[str]]) -> Optional[str]:
    directions = [v for v in factors.values() if v is not None]
    if not directions:
        return None

    bullish = directions.count('bullish')
    bearish = directions.count('bearish')
    total   = len(directions)

    if total == 4 and bullish == 4:
        return 'STRONG_BULLISH'
    if total == 4 and bearish == 4:
        return 'STRONG_BEARISH'
    if total >= 3 and bullish == total:
        return 'BULLISH'
    if total >= 3 and bearish == total:
        return 'BEARISH'
    return None


# ── Public entry point ────────────────────────────────────────────────────────

def evaluate(df: pd.DataFrame, vol_history: VolumeHistory) -> SignalResult:
    """
    Evaluate all four factors and return a SignalResult.

    df must have columns: right, volume, iv, delta
    """
    call_vol = float(df[df['right'] == 'C']['volume'].fillna(0).sum())
    put_vol  = float(df[df['right'] == 'P']['volume'].fillna(0).sum())

    factors = {
        'pc_ratio'         : _pc_ratio_direction(call_vol, put_vol),
        'iv_skew'          : _iv_skew_direction(df),
        'delta_weighted_pc': _delta_weighted_pc_direction(df),
        'volume_anomaly'   : _volume_anomaly_direction(call_vol, put_vol, vol_history),
    }

    pc_ratio = (put_vol / call_vol) if call_vol > 0 else None

    return SignalResult(
        composite      = _composite(factors),
        factors        = factors,
        call_vol       = call_vol,
        put_vol        = put_vol,
        pc_ratio       = pc_ratio,
        factors_active = sum(1 for v in factors.values() if v is not None),
    )
