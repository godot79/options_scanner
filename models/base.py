"""
models/base.py
--------------
Abstract base class for all option pricing models.

Every model must implement:
  price()   -> theoretical price
  iv()      -> implied volatility via Newton-Raphson
  greeks()  -> delta, gamma, vega, theta

This interface is intentionally minimal so new models (e.g. Heston,
SABR, local vol) can be added without touching any other module.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Greeks:
    """Immutable container for option Greeks."""
    delta : Optional[float]
    gamma : Optional[float]
    vega  : Optional[float]
    theta : Optional[float]

    def is_valid(self) -> bool:
        return all(v is not None for v in (self.delta, self.gamma,
                                            self.vega,  self.theta))


@dataclass(frozen=True)
class PricingResult:
    """Full result from a pricing model for one option."""
    iv     : Optional[float]
    greeks : Greeks
    model  : str   # name tag so callers know which model produced this

    @property
    def is_valid(self) -> bool:
        return self.iv is not None and self.greeks.is_valid()

    @classmethod
    def null(cls, model: str) -> 'PricingResult':
        return cls(iv=None, greeks=Greeks(None, None, None, None), model=model)


class BasePricingModel(ABC):
    """
    Abstract base for all option pricing models.

    Subclasses implement _price() and optionally override iv().
    Greeks default to Black finite-differences if not overridden,
    but concrete models should supply analytic Greeks where available.
    """

    NAME: str = 'base'

    # ── Required ──────────────────────────────────────────────────────────────

    @abstractmethod
    def price(self,
              underlying: float,
              strike    : float,
              T         : float,
              r         : float,
              sigma     : float,
              right     : str,
              **kwargs) -> float:
        """
        Return theoretical option price.

        Parameters
        ----------
        underlying : forward price (Black-76) or spot (BSM/Merton)
        strike     : option strike
        T          : time to expiry in years (ACT/365)
        r          : risk-free rate (continuous, annualised)
        sigma      : volatility (annualised)
        right      : 'C' (call) or 'P' (put)
        **kwargs   : model-specific parameters (e.g. jump params)
        """
        ...

    @abstractmethod
    def greeks(self,
               underlying : float,
               strike     : float,
               T          : float,
               r          : float,
               sigma      : float,
               right      : str,
               **kwargs) -> Greeks:
        """Return analytic (or finite-difference) Greeks."""
        ...

    # ── Default IV solver (Newton-Raphson on price()) ─────────────────────────

    def iv(self,
           underlying   : float,
           strike       : float,
           T            : float,
           r            : float,
           market_price : float,
           right        : str,
           init_vol     : float = 0.5,
           tol          : float = 1e-6,
           max_iter     : int   = 100,
           **kwargs) -> Optional[float]:
        """
        Newton-Raphson implied vol with Brent bracket fallback.

        Changes vs original:
        - init_vol raised from 0.3 to 0.5 (better starting point for high-vol
          commodities like CL/SI where realised vol is often 50-100%+)
        - Intrinsic value floor: if market_price is below intrinsic, IV is
          undefined — return None rather than diverging
        - Brent-method bracket fallback when NR diverges or exits bounds,
          searching [1e-4, 10.0] — catches deep moneyness cases where vega
          is near zero and NR step is unreliable
        - max_iter raised to 100 for tighter tolerance on difficult cases
        """
        import math as _math

        if market_price <= 0 or T <= 0 or underlying <= 0 or strike <= 0:
            return None

        # ── Intrinsic value floor ─────────────────────────────────────────────
        # If the market price is at or below intrinsic, vol is undefined.
        # This happens for deep ITM options where the extrinsic is essentially
        # zero — the Black-76/BSM price function is flat w.r.t. sigma there.
        disc = _math.exp(-r * T)
        if right.upper() == 'C':
            intrinsic = max(0.0, disc * (underlying - strike))
        else:
            intrinsic = max(0.0, disc * (strike - underlying))
        if market_price <= intrinsic * 0.999:
            return None

        eps = 1e-4  # finite-difference step for fallback vega

        # ── Newton-Raphson ────────────────────────────────────────────────────
        sigma = init_vol
        for _ in range(max_iter):
            p    = self.price(underlying, strike, T, r, sigma, right, **kwargs)
            diff = p - market_price
            if abs(diff) < tol:
                return max(sigma, 1e-6)

            g             = self.greeks(underlying, strike, T, r, sigma, right, **kwargs)
            analytic_vega = g.vega

            if analytic_vega is not None and analytic_vega > 1e-8:
                vega = analytic_vega
            else:
                p_up = self.price(underlying, strike, T, r, sigma + eps, right, **kwargs)
                vega = (p_up - p) / eps
                if abs(vega) < 1e-8:
                    break

            sigma -= diff / vega
            if sigma <= 0 or sigma > 10:
                break

        # ── Brent bracket fallback ────────────────────────────────────────────
        # When NR diverges (deep moneyness, near-zero vega) try a bracketed
        # root-find over [lo, hi].  More expensive but robust.
        lo, hi = 1e-4, 10.0
        p_lo = self.price(underlying, strike, T, r, lo, right, **kwargs) - market_price
        p_hi = self.price(underlying, strike, T, r, hi, right, **kwargs) - market_price
        if p_lo * p_hi > 0:
            return None   # market_price outside achievable range — give up

        for _ in range(60):
            mid_s = 0.5 * (lo + hi)
            p_mid = self.price(underlying, strike, T, r, mid_s, right, **kwargs) - market_price
            if abs(p_mid) < tol or (hi - lo) < 1e-7:
                result = max(mid_s, 1e-6)
                # Cap at 3.0 (300% vol).  Above this the option is so deep
                # ITM or near-expiry that IV carries no skew information and
                # would dominate any average.  Return None so callers
                # (iv_skew, delta_weighted_pc) treat it as missing data.
                return result if result <= 3.0 else None
            if p_lo * p_mid < 0:
                hi, p_hi = mid_s, p_mid
            else:
                lo, p_lo = mid_s, p_mid

        return None

    # ── Full pricing result ───────────────────────────────────────────────────

    def compute(self,
                underlying   : float,
                strike       : float,
                T            : float,
                r            : float,
                market_price : float,
                right        : str,
                **kwargs) -> PricingResult:
        """
        Convenience method: compute IV then Greeks at that IV.
        Returns PricingResult.null() if IV solve fails.
        """
        sigma = self.iv(underlying, strike, T, r, market_price, right, **kwargs)
        if sigma is None:
            return PricingResult.null(self.NAME)
        g = self.greeks(underlying, strike, T, r, sigma, right, **kwargs)
        return PricingResult(iv=sigma, greeks=g, model=self.NAME)
