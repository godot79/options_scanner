"""
models/black76.py
-----------------
Black-76 model for options on futures (FOP).

Reference: Black, F. (1976). "The pricing of commodity contracts."
           Journal of Financial Economics 3(1-2): 167-179.
"""

import math
from typing import Optional

from options_scanner.models.base import BasePricingModel, Greeks


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1d2(F: float, K: float, T: float, sigma: float) -> tuple[float, float]:
    sqrt_t = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_t)
    return d1, d1 - sigma * sqrt_t


class Black76Model(BasePricingModel):
    """
    Black-76 model.

    The underlying parameter is interpreted as the futures forward price F.
    There is no spot price or carry cost; the discount factor is e^{-rT}.
    """

    NAME = 'black76'

    def price(self,
              underlying : float,
              strike     : float,
              T          : float,
              r          : float,
              sigma      : float,
              right      : str,
              **kwargs) -> float:
        F = underlying
        K = strike
        if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
            return 0.0
        d1, d2 = _d1d2(F, K, T, sigma)
        disc   = math.exp(-r * T)
        if right.upper() == 'C':
            return disc * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
        return disc * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))

    def greeks(self,
               underlying : float,
               strike     : float,
               T          : float,
               r          : float,
               sigma      : float,
               right      : str,
               **kwargs) -> Greeks:
        F = underlying
        K = strike
        if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
            return Greeks(None, None, None, None)

        d1, d2  = _d1d2(F, K, T, sigma)
        disc    = math.exp(-r * T)
        sqrt_t  = math.sqrt(T)
        pdf_d1  = _norm_pdf(d1)

        gamma = disc * pdf_d1 / (F * sigma * sqrt_t)
        vega  = disc * F * pdf_d1 * sqrt_t

        if right.upper() == 'C':
            delta = disc * _norm_cdf(d1)
            theta = (
                -disc * F * pdf_d1 * sigma / (2 * sqrt_t)
                + r * disc * (F * _norm_cdf(d1) - K * _norm_cdf(d2))
            )
        else:
            delta = -disc * _norm_cdf(-d1)
            theta = (
                -disc * F * pdf_d1 * sigma / (2 * sqrt_t)
                + r * disc * (K * _norm_cdf(-d2) - F * _norm_cdf(-d1))
            )

        return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta)

    def analytic_vega(self, F: float, K: float, T: float,
                      r: float, sigma: float) -> float:
        """Exposed separately for Newton-Raphson in IV solver."""
        if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
            return 0.0
        d1, _ = _d1d2(F, K, T, sigma)
        return math.exp(-r * T) * F * _norm_pdf(d1) * math.sqrt(T)
