"""
models/bsm.py
-------------
Black-Scholes-Merton model for equity options.

Supports continuous dividend yield q (set to 0.0 for TSLA).
The underlying parameter is the spot price S.
"""

import math

from options_scanner.models.base import BasePricingModel, Greeks


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1d2(S: float, K: float, T: float, r: float,
          sigma: float, q: float) -> tuple[float, float]:
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * sqrt_t)
    return d1, d1 - sigma * sqrt_t


class BSMModel(BasePricingModel):
    """
    Black-Scholes-Merton model with continuous dividend yield.

    Pass q=0.0 for non-dividend-paying equities (e.g. TSLA).
    The 'q' parameter is read from kwargs with default 0.0 so
    the base class interface is respected without modification.
    """

    NAME = 'bsm'

    def price(self,
              underlying : float,
              strike     : float,
              T          : float,
              r          : float,
              sigma      : float,
              right      : str,
              **kwargs) -> float:
        S = underlying
        K = strike
        q = float(kwargs.get('q', 0.0))

        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
            return 0.0

        d1, d2   = _d1d2(S, K, T, r, sigma, q)
        disc_r   = math.exp(-r * T)
        disc_q   = math.exp(-q * T)

        if right.upper() == 'C':
            return S * disc_q * _norm_cdf(d1) - K * disc_r * _norm_cdf(d2)
        return K * disc_r * _norm_cdf(-d2) - S * disc_q * _norm_cdf(-d1)

    def greeks(self,
               underlying : float,
               strike     : float,
               T          : float,
               r          : float,
               sigma      : float,
               right      : str,
               **kwargs) -> Greeks:
        S = underlying
        K = strike
        q = float(kwargs.get('q', 0.0))

        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
            return Greeks(None, None, None, None)

        d1, d2  = _d1d2(S, K, T, r, sigma, q)
        disc_r  = math.exp(-r * T)
        disc_q  = math.exp(-q * T)
        sqrt_t  = math.sqrt(T)
        pdf_d1  = _norm_pdf(d1)

        gamma = disc_q * pdf_d1 / (S * sigma * sqrt_t)
        vega  = S * disc_q * pdf_d1 * sqrt_t

        if right.upper() == 'C':
            delta = disc_q * _norm_cdf(d1)
            theta = (
                -S * disc_q * pdf_d1 * sigma / (2 * sqrt_t)
                - r * K * disc_r * _norm_cdf(d2)
                + q * S * disc_q * _norm_cdf(d1)
            )
        else:
            delta = -disc_q * _norm_cdf(-d1)
            theta = (
                -S * disc_q * pdf_d1 * sigma / (2 * sqrt_t)
                + r * K * disc_r * _norm_cdf(-d2)
                - q * S * disc_q * _norm_cdf(-d1)
            )

        return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta)
