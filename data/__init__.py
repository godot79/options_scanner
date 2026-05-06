"""
data package
------------
Pure utilities are always importable (no IB dependency).
IB-dependent names are available via explicit import from data.ib_client,
or via lazy import here — they will only fail if ib_insync is not installed.
"""

from options_scanner.data.utils import year_fraction, parse_expiry_date, safe_mid
from options_scanner.data.liquidity import compute_liquidity_metrics


def __getattr__(name: str):
    """
    Lazy-load IB-dependent names so that modules without ib_insync installed
    (e.g. test environments) can still import data.utils and data.liquidity.
    """
    _ib_names = {
        'connect', 'disconnect', 'req_contract_details',
        'discover_futures', 'discover_fop_chain', 'discover_equity_options',
        'fetch_snapshot', 'resolve_underlying_price', 'TickStream',
    }
    if name in _ib_names:
        from options_scanner.data import ib_client
        return getattr(ib_client, name)
    _cache_names = {
        'CacheManager', 'load_cache', 'save_cache',
        'archive_contracts', 'write_refresh_request', 'read_refresh_requests',
    }
    if name in _cache_names:
        from options_scanner.data import contract_cache
        return getattr(contract_cache, name)
    raise AttributeError(f"module 'options_scanner.data' has no attribute {name!r}")


__all__ = [
    # Pure — always available
    'year_fraction',
    'parse_expiry_date',
    'safe_mid',
    'compute_liquidity_metrics',
    # IB-dependent — lazy
    'connect',
    'disconnect',
    'req_contract_details',
    'discover_futures',
    'discover_fop_chain',
    'discover_equity_options',
    'fetch_snapshot',
    'resolve_underlying_price',
    'TickStream',
    # Cache
    'CacheManager',
    'load_cache',
    'save_cache',
    'archive_contracts',
    'write_refresh_request',
    'read_refresh_requests',
]
