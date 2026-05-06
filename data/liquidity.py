"""
data/liquidity.py
-----------------
Liquidity scoring for options DataFrames.

Score components (configurable weights):
  spread_score : inverted relative bid/ask spread (tighter = better)
  vol_score    : log-normalised daily volume
  oi_score     : log-normalised open interest

Composite liquidity_score in [0, 1].
"""

import math
from typing import Optional

import pandas as pd

from options_scanner.data.utils import safe_mid

# Weights must sum to 1.0
_W_SPREAD : float = 0.40
_W_VOL    : float = 0.35
_W_OI     : float = 0.25


def compute_liquidity_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add liquidity columns to an options DataFrame in-place (returns copy).

    Required input columns : bid, ask, volume, openInterest
    Added columns          : mid, spread, spread_pct, vol_score,
                             oi_score, spread_score, liquidity_score
    """
    df = df.copy()

    # Sanitise IB sentinel -1 values: IB returns -1.0 (not NaN/None) to mean
    # "no data".  Replace with NaN immediately so all downstream logic is clean.
    for _col in ('bid', 'ask'):
        df[_col] = pd.to_numeric(df[_col], errors='coerce')
        df.loc[df[_col] < 0, _col] = float('nan')

    # Mid
    df['mid'] = [
        safe_mid(row['bid'], row['ask'])
        for _, row in df[['bid', 'ask']].iterrows()
    ]

    # Absolute spread
    df['spread'] = df.apply(
        lambda r: r['ask'] - r['bid']
        if (r['ask'] is not None and r['bid'] is not None
            and r['ask'] > 0 and r['bid'] > 0)
        else None,
        axis=1,
    )

    # Relative spread — guard against zero or None mid
    df['spread_pct'] = df.apply(
        lambda r: r['spread'] / r['mid']
        if (r['spread'] is not None and r['mid'] is not None and r['mid'] > 0)
        else None,
        axis=1,
    )

    # Invalidate non-positive spreads and out-of-range spread_pct
    df.loc[df['spread'].isna() | ~df['spread'].gt(0),   'spread']     = None
    df.loc[
        df['spread_pct'].isna() | ~df['spread_pct'].between(0, 1.0),
        'spread_pct'
    ] = None

    # Log-scaled volume and OI
    # pd.to_numeric coerces Python None and mixed types to float NaN cleanly,
    # avoiding the pandas FutureWarning triggered by fillna on object-dtype series
    df['vol_score'] = (
        pd.to_numeric(df['volume'], errors='coerce').fillna(0).clip(lower=0) + 1
    ).apply(math.log)
    df['oi_score'] = (
        pd.to_numeric(df['openInterest'], errors='coerce').fillna(0).clip(lower=0) + 1
    ).apply(math.log)

    # Spread score: 1 = zero spread, 0 = spread_pct >= 0.5
    capped             = pd.to_numeric(df['spread_pct'], errors='coerce').clip(lower=0.0, upper=0.5)
    df['spread_score'] = (1.0 - capped / 0.5).fillna(0.0).astype(float)

    # Min-max normalise vol_score and oi_score to [0, 1]
    for col in ('vol_score', 'oi_score'):
        mn, mx = df[col].min(), df[col].max()
        if mx > mn:
            df[col] = (df[col] - mn) / (mx - mn)
        else:
            df[col] = 0.0

    df['liquidity_score'] = (
        _W_SPREAD * df['spread_score'].fillna(0.0)
        + _W_VOL  * df['vol_score'].fillna(0.0)
        + _W_OI   * df['oi_score'].fillna(0.0)
    )

    return df
