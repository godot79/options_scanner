"""
models/merton_jd.py
-------------------
Merton (1976) Jump-Diffusion model.

Prices via Poisson series expansion — each term weights a Black-76 or BSM
price with adjusted vol and rate.  Greeks are computed via finite differences
since analytic forms are non-standard.

Parameters (passed via kwargs or instrument config):
  lam     : jump intensity (expected jumps per year), default 0.1
  mu_j    : mean of log-jump, default 0.0
  sigma_j : std-dev of log-jump, default 0.1
  n_terms : Poisson series truncation, default 20

is_futures (bool, default True): if True, use Black-76 base; else BSM.
q         (float, default 0.0) : dividend yield for BSM base.

Reference: Merton, R.C. (1976). "Option pricing when underlying stock
           returns are discontinuous." Journal of Financial Economics 3: 125-144.
"""

import math
from typing import Optional

from options_scanner.models.base import BasePricingModel, Greeks
from options_scanner.models.black76 import Black76Model
from options_scanner.models.bsm import BSMModel

_b76 = Black76Model()
_bsm = BSMModel()

_DEFAULT_LAM     = 0.1
_DEFAULT_MU_J    = 0.0
_DEFAULT_SIGMA_J = 0.1
_DEFAULT_NTERMS  = 20
_FD_EPS_S        = 1e-3   # relative epsilon for spot/forward finite diff
_FD_EPS_V        = 1e-4   # absolute epsilon for vega finite diff
_FD_EPS_T        = 1 / 365


class MertonJDModel(BasePricingModel):
    """
    Merton Jump-Diffusion.  Works for both futures (is_futures=True)
    and equity (is_futures=False) underlyings.

    The compensated jump mean k_bar = exp(mu_j + 0.5*sigma_j^2) - 1
    adjusts the drift so that E[e^J] = 1 (martingale condition).
    """

    NAME = 'merton_jd'

    def price(self,
              underlying : float,
              strike     : float,
              T          : float,
              r          : float,
              sigma      : float,
              right      : str,
              **kwargs) -> float:
        return self._merton_price(underlying, strike, T, r, sigma, right, **kwargs)

    def _merton_price(self,
                      underlying : float,
                      strike     : float,
                      T          : float,
                      r          : float,
                      sigma      : float,
                      right      : str,
                      **kwargs) -> float:
        lam        = float(kwargs.get('lam',        _DEFAULT_LAM))
        mu_j       = float(kwargs.get('mu_j',       _DEFAULT_MU_J))
        sigma_j    = float(kwargs.get('sigma_j',    _DEFAULT_SIGMA_J))
        n_terms    = int(  kwargs.get('n_terms',    _DEFAULT_NTERMS))
        is_futures = bool( kwargs.get('is_futures', True))
        q          = float(kwargs.get('q',          0.0))

        if T <= 0 or sigma <= 0 or underlying <= 0 or strike <= 0:
            return 0.0

        k_bar     = math.exp(mu_j + 0.5 * sigma_j ** 2) - 1.0
        lam_star  = lam * (1.0 + k_bar)
        total     = 0.0

        for n in range(n_terms):
            # Poisson weight
            poisson_w = (math.exp(-lam_star * T)
                         * (lam_star * T) ** n
                         / math.factorial(n))
            if poisson_w < 1e-12:
                break  # negligible tail

            sigma_n = math.sqrt(max(sigma ** 2 + n * sigma_j ** 2 / T, 1e-10))
            r_n     = r - lam * k_bar + n * (mu_j + 0.5 * sigma_j ** 2) / T

            if is_futures:
                term = _b76.price(underlying, strike, T, r_n, sigma_n, right)
            else:
                term = _bsm.price(underlying, strike, T, r_n, sigma_n, right, q=q)

            total += poisson_w * term

        return total

    def greeks(self,
               underlying : float,
               strike     : float,
               T          : float,
               r          : float,
               sigma      : float,
               right      : str,
               **kwargs) -> Greeks:
        """Finite-difference Greeks (central differences for delta/gamma)."""
        if T <= 0 or sigma <= 0 or underlying <= 0 or strike <= 0:
            return Greeks(None, None, None, None)

        eps_s = underlying * _FD_EPS_S

        p0   = self._merton_price(underlying,       strike, T, r, sigma, right, **kwargs)
        p_su = self._merton_price(underlying+eps_s, strike, T, r, sigma, right, **kwargs)
        p_sd = self._merton_price(underlying-eps_s, strike, T, r, sigma, right, **kwargs)

        delta = (p_su - p_sd) / (2 * eps_s)
        gamma = (p_su - 2 * p0 + p_sd) / (eps_s ** 2)

        # Vega: bump sigma
        p_vu = self._merton_price(underlying, strike, T, r, sigma + _FD_EPS_V,
                                   right, **kwargs)
        vega = (p_vu - p0) / _FD_EPS_V

        # Theta: bump T backward (reduce T by 1 day)
        if T > _FD_EPS_T:
            p_td = self._merton_price(underlying, strike, T - _FD_EPS_T,
                                       r, sigma, right, **kwargs)
            theta = (p_td - p0) / _FD_EPS_T
        else:
            theta = 0.0

        return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta)

    def iv(self,
           underlying   : float,
           strike       : float,
           T            : float,
           r            : float,
           market_price : float,
           right        : str,
           init_vol     : float = 0.3,
           tol          : float = 1e-4,
           max_iter     : int   = 40,
           **kwargs) -> Optional[float]:
        """
        Newton-Raphson IV using finite-difference vega (Merton has no
        clean analytic vega formula under the series expansion).
        """
        if market_price <= 0 or T <= 0 or underlying <= 0 or strike <= 0:
            return None

        sigma = init_vol
        for _ in range(max_iter):
            p    = self._merton_price(underlying, strike, T, r, sigma, right, **kwargs)
            diff = p - market_price
            if abs(diff) < tol:
                return max(sigma, 1e-6)

            p_up = self._merton_price(underlying, strike, T, r,
                                       sigma + _FD_EPS_V, right, **kwargs)
            fd_vega = (p_up - p) / _FD_EPS_V
            if abs(fd_vega) < 1e-8:
                break

            sigma -= diff / fd_vega
            if sigma <= 0 or sigma > 10:
                break

        return None
