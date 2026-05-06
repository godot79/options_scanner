"""
data/utils.py
-------------
Shared utility functions used across modules.
No IB dependencies here — pure Python / math only.
"""

import math
from datetime import datetime, timezone, timedelta
from typing import Optional


def year_fraction(expiry_str: str, now: datetime) -> float:
    """
    ACT/365 year fraction from `now` to expiry.

    Accepts 'YYYYMMDD' or 'YYYYMM' (assumes last day of month for 6-char).
    Returns 0.0 for invalid or already-expired dates.
    """
    s = str(expiry_str).strip()
    try:
        if len(s) >= 8:
            dt = datetime.strptime(s[:8], '%Y%m%d').replace(tzinfo=timezone.utc)
        elif len(s) >= 6:
            # First day of month, then roll to last
            dt = datetime.strptime(s[:6] + '01', '%Y%m%d').replace(tzinfo=timezone.utc)
            nm = dt.replace(day=28) + timedelta(days=4)
            dt = nm - timedelta(days=nm.day)
        else:
            return 0.0
    except ValueError:
        return 0.0

    delta_seconds = (dt - now).total_seconds()
    return max(delta_seconds / (365.0 * 86_400), 0.0)


def parse_expiry_date(expiry_str: str) -> Optional[datetime]:
    """Parse expiry string to UTC datetime.  Returns None on failure."""
    s = str(expiry_str).strip()
    try:
        if len(s) >= 8:
            return datetime.strptime(s[:8], '%Y%m%d').replace(tzinfo=timezone.utc)
        elif len(s) >= 6:
            return datetime.strptime(s[:6] + '01', '%Y%m%d').replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None


def safe_mid(bid: Optional[float],
             ask: Optional[float]) -> Optional[float]:
    """
    Return mid-price.  Handles None and crossed/one-sided markets.
    Returns None only when both sides are unavailable or non-positive.
    """
    b_ok = bid is not None and bid > 0
    a_ok = ask is not None and ask > 0

    if b_ok and a_ok:
        return (bid + ask) / 2.0     # type: ignore[operator]
    if b_ok:
        return bid
    if a_ok:
        return ask
    return None
