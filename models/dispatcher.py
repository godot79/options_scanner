"""
models/dispatcher.py
--------------------
Routes pricing requests to the correct model instance based on
instrument configuration.  New models are registered in REGISTRY.
"""

from options_scanner.models.base import BasePricingModel, PricingResult
from options_scanner.models.black76 import Black76Model
from options_scanner.models.bsm import BSMModel
from options_scanner.models.merton_jd import MertonJDModel

# ── Registry: add new models here, nothing else changes ──────────────────────
REGISTRY: dict[str, BasePricingModel] = {
    'black76'   : Black76Model(),
    'bsm'       : BSMModel(),
    'merton_jd' : MertonJDModel(),
}


def get_model(name: str) -> BasePricingModel:
    """Return a model instance by name.  Raises KeyError for unknown names."""
    if name not in REGISTRY:
        available = ', '.join(REGISTRY.keys())
        raise KeyError(f"Unknown model '{name}'. Available: {available}")
    return REGISTRY[name]


def compute(instrument_cfg : dict,
            underlying     : float,
            strike         : float,
            T              : float,
            r              : float,
            market_price   : float,
            right          : str) -> PricingResult:
    """
    Dispatch to the model specified in instrument_cfg['model'].

    Extra model kwargs (q, lam, mu_j, sigma_j, is_futures) are
    extracted from instrument_cfg and forwarded transparently.
    """
    model_name = instrument_cfg.get('model', 'black76')
    model      = get_model(model_name)

    is_futures = instrument_cfg.get('secType') == 'FOP'
    kwargs     = {
        'q'          : instrument_cfg.get('div_yield', 0.0),
        'is_futures' : is_futures,
        'lam'        : instrument_cfg.get('jd_lambda',  0.1),
        'mu_j'       : instrument_cfg.get('jd_mu_j',    0.0),
        'sigma_j'    : instrument_cfg.get('jd_sigma_j', 0.1),
    }

    return model.compute(underlying, strike, T, r, market_price, right, **kwargs)
