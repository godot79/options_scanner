"""
signals/fingerprint/base.py
----------------------------
Abstract base class for all fingerprint models.

Every fingerprint model:
  - receives a standardised data payload via update()
  - runs its detection logic via detect()
  - returns a list of Finding objects with confidence scores and evidence

Source tagging allows external signals to plug in using the same interface:
  source = 'ibkr_snapshot'  : derived from IB snapshot scans (Option B)
  source = 'ibkr_tick'      : derived from IB tick-by-tick stream (Option A)
  source = 'external'       : external data feed (dark pool, news, etc.)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional
import pandas as pd


@dataclass(frozen=True)
class Finding:
    """
    One fingerprint finding from one model.

    confidence : float in [0, 1]; higher = stronger signal
    source     : data origin tag (see module docstring)
    instrument : e.g. 'CL', 'SI', 'TSLA'
    model      : name of the FingerprintModel that produced this
    finding_type : short machine-readable label, e.g. 'oi_build'
    note       : human-readable description
    evidence   : raw supporting data dict (for logging / downstream models)
    expiry     : option expiry string, if applicable
    strike     : option strike, if applicable
    right      : 'C' or 'P', if applicable
    """
    confidence   : float
    source       : str
    instrument   : str
    model        : str
    finding_type : str
    note         : str
    evidence     : dict = field(default_factory=dict)
    expiry       : Optional[str]   = None
    strike       : Optional[float] = None
    right        : Optional[str]   = None

    def __post_init__(self):
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")
        if self.source not in ('ibkr_snapshot', 'ibkr_tick', 'external'):
            raise ValueError(f"Invalid source: {self.source}")


class BaseFingerprintModel(ABC):
    """
    Abstract fingerprint model.

    Subclasses implement:
      accepts_source  : list of source tags this model processes
      update()        : ingest new data for one scan cycle
      detect()        : return findings based on accumulated history
    """

    NAME             : str       = 'base'
    accepts_source   : list[str] = ['ibkr_snapshot', 'ibkr_tick', 'external']

    @abstractmethod
    def update(self, instrument: str, data: Any) -> None:
        """
        Ingest data for one scan cycle.

        `data` type depends on the model:
          snapshot models : pd.DataFrame (options chain with volume/OI)
          tick models     : list[dict]   (tick prints from TickStream.drain())
          external models : dict         (arbitrary external payload)
        """
        ...

    @abstractmethod
    def detect(self, instrument: str) -> list[Finding]:
        """
        Run detection logic on accumulated history.
        Returns list of Finding objects (may be empty).
        """
        ...

    def clear(self, instrument: str) -> None:
        """
        Optional: clear history for an instrument.
        Useful for testing or after a long gap in data.
        """
        pass
