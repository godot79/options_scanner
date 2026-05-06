"""
scanner/instrument.py
---------------------
Per-instrument scan orchestration.

InstrumentScanner handles:
  - contract discovery (delegated to data.ib_client)
  - per-scan cycle: quotes -> liquidity -> moneyness filter -> IV/Greeks
    -> signal evaluation -> fingerprint update/detect -> alerts -> logging
  - console output structured for readability (and future UI consumption)
  - optional tick stream management (Option A)
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from ib_insync import IB

import options_scanner.config as cfg
from options_scanner.data import (
    discover_futures,
    discover_fop_chain,
    discover_equity_options,
    fetch_snapshot,
    resolve_underlying_price,
    compute_liquidity_metrics,
    year_fraction,
    safe_mid,
    TickStream,
)
from options_scanner.data.ib_client import EquityStream, OIStream, qualify_chain_for_scan
from options_scanner.models import compute as model_compute
from options_scanner.signals.volume_history import VolumeHistory
from options_scanner.signals import evaluate as signal_evaluate
from options_scanner.signals.fingerprint import FingerprintEngine
from options_scanner.io.alerts import AlertManager
from options_scanner.io.csv_logger import CSVLogger
from options_scanner.data.contract_cache import CacheManager

_DISPLAY_COLS = [
    'localSymbol', 'expiry', 'right', 'strike',
    'bid', 'ask', 'volume', 'openInterest',
    'spread_pct', 'liquidity_score',
    'iv', 'delta', 'gamma', 'vega', 'theta',
]


class InstrumentScanner:

    def __init__(self,
                 ib            : IB,
                 key           : str,
                 cfg_inst      : dict,
                 vol_history   : VolumeHistory,
                 alert_manager : AlertManager,
                 fp_engine     : FingerprintEngine,
                 csv_logger    : CSVLogger,
                 cache_manager : 'CacheManager | None' = None):
        self.ib            = ib
        self.key           = key
        self.cfg           = cfg_inst
        self.vol_history   = vol_history
        self.alert_manager = alert_manager
        self.fp_engine     = fp_engine
        self.csv_logger    = csv_logger
        self.cache_manager = cache_manager

        # Populated from CacheManager (or fallback inline discovery)
        self.underlying_contracts : list = []
        self.option_contracts     : list = []
        self.details_cache        : dict = {}   # conId -> ContractDetails
        self._discovered          : bool = False

        # Option A: tick streams keyed by conId
        self._tick_streams  : dict[int, TickStream]       = {}
        # Persistent equity stream for OPT instruments (resolves undPrice)
        self._equity_stream : 'EquityStream | None'       = None
        # Persistent OI streams for FOP underlying futures (one per future conId).
        # Keeps genericTickList='101' (open interest) alive so IB populates
        # openInterest on subsequent option snapshot rows.
        self._oi_streams    : dict[int, 'OIStream']       = {}
        # ChainSpec list from cache (v0.3.0+)
        self._chain_specs   : list                        = []

    # ── Discovery ─────────────────────────────────────────────────────────────

    async def discover(self) -> None:
        """
        Load contracts from CacheManager if available, otherwise fall back to
        inline IB discovery.  CacheManager is the preferred path — it is
        non-blocking and returns cached data immediately.
        """
        if self.cache_manager and self.cache_manager.is_ready(self.key):
            self._load_from_cache()
        else:
            print(f"[{self.key}] No cache available — discovering inline...")
            if self.cfg['secType'] == 'FOP':
                await self._discover_fop()
            else:
                await self._discover_equity()

        self._discovered = True
        print(
            f"[{self.key}] Ready — "
            f"{len(self.underlying_contracts)} underlying, "
            f"{len(self.option_contracts)} options."
        )

        # Start persistent equity stream for OPT instruments so that
        # the underlying price is always available for IV/Greeks computation
        if self.cfg['secType'] == 'OPT':
            self._equity_stream = EquityStream(self.ib, self.cfg)
            await self._equity_stream.start()

        # Start persistent OI streams for FOP underlying futures so that
        # IB populates openInterest on option snapshot rows (generic tick 101
        # requires a non-snapshot stream; snapshot=True is incompatible).
        if self.cfg['secType'] == 'FOP':
            await self._start_oi_streams()

        if cfg.USE_STREAMING_TICKS:
            self._start_tick_streams()

    def _load_from_cache(self) -> None:
        """Pull ChainSpecs and futures from CacheManager into scanner state."""
        # v0.3.0+: cache stores ChainSpec params; contracts qualified per-scan
        self._chain_specs         = self.cache_manager.serve_chain_specs(self.key)
        self.underlying_contracts = self.cache_manager.serve_underlying(self.key)
        # Clear pre-qualified option_contracts — they are built fresh each scan
        self.option_contracts     = []
        self.details_cache        = {}
        total = sum(len(s.expirations) * len(s.strikes) * 2
                    for s in self._chain_specs)
        print(f"[{self.key}] Loaded from cache: "
              f"{len(self._chain_specs)} chain specs "
              f"({total} theoretical contracts), "
              f"{len(self.underlying_contracts)} underlying futures")

    # ── OI streams (FOP only) ─────────────────────────────────────────────────

    async def _start_oi_streams(self) -> None:
        """
        Open a persistent non-snapshot stream per underlying future contract
        with genericTickList='588' (Futures Open Interest, tick ID 86).

        IB does not populate openInterest on snapshot=True requests.
        Keeping a non-snapshot stream alive causes IB to populate
        futuresOpenInterest and, over time, openInterest on subsequent option
        snapshot rows during the same session.

        One stream per underlying future is sufficient for the macro OI picture.
        """
        for fut in self.underlying_contracts:
            stream = OIStream(self.ib, fut)
            await stream.start()
            self._oi_streams[fut.conId] = stream
        if self._oi_streams:
            print(f"[{self.key}] OI streams started for "
                  f"{len(self._oi_streams)} underlying futures.")

    def _stop_oi_streams(self) -> None:
        for stream in self._oi_streams.values():
            stream.stop()
        self._oi_streams.clear()

    async def _discover_fop(self) -> None:
        futs = await discover_futures(self.ib, self.cfg)
        if not futs:
            print(f"[{self.key}][WARN] No futures found — scanner inactive.")
            return

        self.underlying_contracts = futs
        await asyncio.sleep(1.0)   # pacing

        # Full chain discovery using reqSecDefOptParams (no throttling)
        # Pass futs to reuse the already-fetched underlying conId
        self.option_contracts = await discover_fop_chain(
            self.ib, self.cfg, self.details_cache, futures=futs
        )

    async def _discover_equity(self) -> None:
        underlying, opts = await discover_equity_options(
            self.ib, self.cfg, self.details_cache
        )
        self.underlying_contracts = underlying
        self.option_contracts     = opts

    # ── Tick streams (Option A) ───────────────────────────────────────────────

    def _start_tick_streams(self) -> None:
        for opt in self.option_contracts:
            stream = TickStream(self.ib, opt)
            stream.start()
            self._tick_streams[opt.conId] = stream
        print(f"[{self.key}] Tick streams started for "
              f"{len(self._tick_streams)} contracts.")

    def _drain_tick_prints(self) -> list[dict]:
        prints = []
        for stream in self._tick_streams.values():
            prints.extend(stream.drain())
        return prints

    def stop_tick_streams(self) -> None:
        for stream in self._tick_streams.values():
            stream.stop()
        self._tick_streams.clear()
        if self._equity_stream is not None:
            self._equity_stream.stop()
            self._equity_stream = None
        self._stop_oi_streams()

    # ── Moneyness pre-filter ──────────────────────────────────────────────────

    def _apply_moneyness_prefilter(self,
                                    contracts   : list,
                                    price_map   : dict) -> list:
        """
        For FOP instruments: filter option contracts to moneyness band
        BEFORE requesting quotes, to avoid snapshotting thousands of
        deep OTM contracts that carry no signal.

        If no underlying price is available, returns all contracts
        (safe fallback — scanner still works, just slower).
        """
        if self.cfg['secType'] != 'FOP' or not price_map:
            return contracts

        # Find a valid underlying price from the map
        price = next((v for v in price_map.values() if v and v > 0), None)
        if not price:
            return contracts   # no price yet — return all, filter later

        lo = price * (1 - cfg.MONEYNESS_BAND)
        hi = price * (1 + cfg.MONEYNESS_BAND)

        filtered = [
            c for c in contracts
            if lo <= getattr(c, 'strike', 0) <= hi
        ]

        if not filtered:
            # Fallback: return all if filter is too aggressive
            return contracts

        if len(filtered) < len(contracts):
            print(f"  [{self.key}] Moneyness pre-filter: "
                  f"{len(filtered)}/{len(contracts)} contracts "
                  f"(±{cfg.MONEYNESS_BAND*100:.0f}% of {price:.4g})")
        return filtered

    # ── Single scan cycle ─────────────────────────────────────────────────────

    async def scan(self) -> None:
        if not self._discovered or (not self.option_contracts and not self._chain_specs):
            print(f"[{self.key}] Skipping scan — not ready.")
            return

        now = datetime.now(timezone.utc)
        self.alert_manager.tick(self.key)

        # ── 1. Underlying price(s) ─────────────────────────────────────────
        underlying_price_map: dict = {}

        if self.cfg['secType'] == 'FOP':
            fut_tickers = await fetch_snapshot(self.ib, self.underlying_contracts)
            for fut in self.underlying_contracts:
                t = fut_tickers.get(fut.conId)
                if t is None:
                    continue
                mid = safe_mid(getattr(t, 'bid', None), getattr(t, 'ask', None))
                if mid is None:
                    mid = getattr(t, 'last', None) or getattr(t, 'close', None)
                if mid and mid > 0:
                    underlying_price_map[fut.conId] = float(mid)
        else:
            # Placeholder — filled after option snapshot below
            underlying_price_map['equity'] = None

        # ── 2. Qualify + snapshot option contracts ────────────────────────────
        # v0.3.0+: qualify only the ±20% moneyness subset from cached ChainSpecs
        # This replaces the old pre-filter on pre-qualified contracts.

        # For OPT instruments resolve the underlying price NOW (before qualify)
        # so the moneyness filter can scope the chain correctly.  Without this,
        # und_price=None causes qualify_chain_for_scan to pass ALL strikes through
        # (e.g. all 10,450 TSLA contracts instead of the ~200 in the band).
        if self.cfg['secType'] == 'OPT':
            early_price: Optional[float] = None
            if self._equity_stream is not None:
                early_price = self._equity_stream.price()
            if early_price and early_price > 0:
                underlying_price_map['equity'] = float(early_price)
            else:
                print(f"[{self.key}] Skipping scan — underlying price not yet "
                      f"available (equity market may be closed).")
                return

        und_price = next(
            (v for v in underlying_price_map.values() if v and v > 0), None
        )
        if self._chain_specs:
            # Qualify the moneyness subset — fast (<30s for ~200 contracts)
            contracts_to_quote = await qualify_chain_for_scan(
                self.ib,
                self._chain_specs,
                und_price,
                cfg.MONEYNESS_BAND,
                self.details_cache,
            )
            # Keep underlying_contracts updated from futures stored in cache
            if not self.underlying_contracts and self.cache_manager:
                self.underlying_contracts =                     self.cache_manager.serve_underlying(self.key)
        else:
            # Fallback: use pre-qualified contracts (legacy path)
            contracts_to_quote = self._apply_moneyness_prefilter(
                self.option_contracts, underlying_price_map
            )
        opt_tickers = await fetch_snapshot(self.ib, contracts_to_quote)

        if self.cfg['secType'] == 'OPT':
            equity_price = None

            # 1. Best source: persistent equity stream (opened at startup)
            if self._equity_stream is not None:
                equity_price = self._equity_stream.price()

            # 2. Fallback: undPrice on option tickers (unreliable but free)
            if not equity_price or equity_price <= 0:
                for t in opt_tickers.values():
                    val = getattr(t, 'undPrice', None)
                    if val and float(val) > 0:
                        equity_price = float(val)
                        break

            # 3. Fallback: last/close on any option ticker
            if not equity_price or equity_price <= 0:
                for t in opt_tickers.values():
                    for field in ('last', 'close'):
                        val = getattr(t, field, None)
                        if val and float(val) > 0:
                            equity_price = float(val)
                            break
                    if equity_price and equity_price > 0:
                        break

            # 4. Fallback: last known price from CacheManager
            if not equity_price or equity_price <= 0:
                equity_price = (
                    self.cache_manager._last_prices.get(self.key)
                    if self.cache_manager else None
                )

            if equity_price and equity_price > 0:
                underlying_price_map['equity'] = float(equity_price)
            else:
                underlying_price_map['equity'] = None
                print(f"[{self.key}] Could not resolve underlying price "
                      f"— moneyness filter will pass all strikes through")

        # ── 3. Build raw DataFrame ─────────────────────────────────────────
        rows = []
        for opt in contracts_to_quote:
            t  = opt_tickers.get(opt.conId)
            cd = self.details_cache.get(opt.conId)
            if t is None or cd is None:
                continue

            und_key = (cd.underConId
                       if self.cfg['secType'] == 'FOP'
                       else 'equity')

            rows.append({
                'conId'        : opt.conId,
                'symbol'       : opt.symbol,
                'localSymbol'  : getattr(opt, 'localSymbol', ''),
                'strike'       : opt.strike,
                'right'        : opt.right,
                'expiry'       : opt.lastTradeDateOrContractMonth,
                'bid'          : getattr(t, 'bid',          None),
                'ask'          : getattr(t, 'ask',          None),
                'last'         : getattr(t, 'last',         None),
                'volume'       : getattr(t, 'volume',       None),
                'openInterest' : getattr(t, 'openInterest', None),
                'und_key'      : und_key,
            })

        if not rows:
            print(f"[{self.key}] No option quotes received.")
            return

        df = pd.DataFrame(rows)

        # ── 4. Liquidity metrics ───────────────────────────────────────────
        df = compute_liquidity_metrics(df)

        # ── 5. Moneyness filter ────────────────────────────────────────────
        def _in_band(row: pd.Series) -> bool:
            price = underlying_price_map.get(row['und_key'])
            if not price or price <= 0:
                return True   # keep if price unavailable
            K = row['strike']
            return (K >= price * (1 - cfg.MONEYNESS_BAND) and
                    K <= price * (1 + cfg.MONEYNESS_BAND))

        df = df[df.apply(_in_band, axis=1)].copy()

        if df.empty:
            print(f"[{self.key}] No options within ±{cfg.MONEYNESS_BAND*100:.0f}% "
                  f"moneyness band.")
            return

        # ── 6. IV & Greeks ────────────────────────────────────────────────
        iv_col, delta_col, gamma_col, vega_col, theta_col = [], [], [], [], []

        for _, row in df.iterrows():
            price = underlying_price_map.get(row['und_key'])
            T     = year_fraction(str(row['expiry']), now)
            mid   = row.get('mid')

            # Skip IV if we have no mid price or no underlying price.
            # Passing 0.0 wastes solver iterations and produces no useful output.
            # Skipping explicitly also prevents zero-price noise from entering
            # iv_skew (which drops NaN rows, so None here is equivalent but faster).
            # NaN guard: float('nan') is truthy and NaN <= 0 is False, so we
            # must check math.isnan explicitly — otherwise NaN mid reaches the
            # solver and Brent fallback returns sigma=10 as the boundary clamp.
            import math as _math
            if (not mid or mid <= 0 or (_math.isfinite(mid) is False)
                    or not price or price <= 0):
                iv_col.append(None)
                delta_col.append(None)
                gamma_col.append(None)
                vega_col.append(None)
                theta_col.append(None)
                continue

            result = model_compute(
                instrument_cfg = self.cfg,
                underlying     = float(price),
                strike         = float(row['strike']),
                T              = T,
                r              = cfg.RISK_FREE_RATE,
                market_price   = float(mid),
                right          = str(row['right']),
            )
            iv_col.append(result.iv)
            delta_col.append(result.greeks.delta)
            gamma_col.append(result.greeks.gamma)
            vega_col.append(result.greeks.vega)
            theta_col.append(result.greeks.theta)

        df = df.copy()
        df['iv']    = iv_col
        df['delta'] = delta_col
        df['gamma'] = gamma_col
        df['vega']  = vega_col
        df['theta'] = theta_col

        # ── 7. Volume history ──────────────────────────────────────────────
        call_vol = float(df[df['right'] == 'C']['volume'].fillna(0).sum())
        put_vol  = float(df[df['right'] == 'P']['volume'].fillna(0).sum())
        self.vol_history.record(call_vol, put_vol)

        # Report current underlying price to CacheManager for drift detection
        primary_price_for_cache = next(
            (v for v in underlying_price_map.values() if v and v > 0), None
        )
        if self.cache_manager and primary_price_for_cache:
            self.cache_manager.report_price(self.key, primary_price_for_cache)

        # ── 8. Signal evaluation ───────────────────────────────────────────
        signal = signal_evaluate(df, self.vol_history)

        # ── 9. Fingerprint ─────────────────────────────────────────────────
        tick_prints = self._drain_tick_prints() if cfg.USE_STREAMING_TICKS else None
        self.fp_engine.update(self.key, df, tick_prints)
        fp_findings = self.fp_engine.detect(self.key)

        # ── 10. Alerts ─────────────────────────────────────────────────────
        console_alerts: list[str] = []

        # Signal alert
        if signal.composite:
            sig_key = f"signal_{signal.composite}"
            if self.alert_manager.should_fire(self.key, sig_key):
                msg = AlertManager.format_signal_alert(
                    self.key, signal.composite, signal
                )
                console_alerts.append(msg)
                self.alert_manager.mark_fired(self.key, sig_key)
                self.csv_logger.write_signal_alert(
                    self.key, signal.composite, signal, msg
                )
        else:
            for direction in ('STRONG_BULLISH', 'BULLISH',
                              'BEARISH', 'STRONG_BEARISH'):
                self.alert_manager.clear(self.key, f"signal_{direction}")

        # Fingerprint alerts
        for fp in fp_findings:
            # Key includes finding content so genuinely new patterns fire
            # even when a different finding of the same type is suppressed.
            # For findings without expiry/strike/right (volume_cluster,
            # expiry_concentration) we fold in lot_size or expiry from
            # evidence so the same persistent position doesn't re-fire
            # every ALERT_SUPPRESSION_SCANS interval.
            ev       = fp.evidence or {}
            lot_size = ev.get('lot_size', '')
            ev_exp   = '_'.join(str(e) for e in ev.get('keys', [])[:1]) if 'keys' in ev else ''
            fp_key   = (f"fp_{fp.finding_type}"
                        f"_{fp.expiry or ev.get('expiry', '')}"
                        f"_{fp.strike or ''}"
                        f"_{fp.right  or ev.get('right', '')}"
                        f"_{lot_size}"
                        f"_{ev_exp}")
            if self.alert_manager.should_fire(self.key, fp_key):
                msg = AlertManager.format_fingerprint_alert(fp)
                console_alerts.append(msg)
                self.alert_manager.mark_fired(self.key, fp_key)
                self.csv_logger.write_fingerprint_alert(self.key, fp, msg)

        # ── 11. Console output ─────────────────────────────────────────────
        primary_price = next(iter(underlying_price_map.values()), None)
        self._print_scan(now, df, signal, console_alerts,
                         fp_findings, underlying_price_map)

        # ── 12. CSV ────────────────────────────────────────────────────────
        self.csv_logger.write_scan(self.key, df, signal, primary_price)

    # ── Console output ────────────────────────────────────────────────────────

    def _print_scan(self,
                    now              : datetime,
                    df               : pd.DataFrame,
                    signal,
                    alerts           : list[str],
                    fp_findings      : list,
                    price_map        : dict) -> None:

        sep   = '─' * 100
        ts    = now.strftime('%Y-%m-%d %H:%M:%S UTC')
        name  = self.cfg['description']

        print(f"\n{sep}")
        print(f"  {self.key:6s}  │  {name:40s}  │  {ts}")
        print(sep)

        # Underlying prices
        for k, v in price_map.items():
            label = 'Underlying' if k == 'equity' else f'Future {k}'
            val   = f"{v:.5g}" if v is not None else 'N/A'
            print(f"  {label}: {val}")

        # Signal summary
        comp = signal.composite or 'NONE'
        pc   = f"{signal.pc_ratio:.3f}" if signal.pc_ratio is not None else 'N/A'
        print(
            f"\n  Signal    : {comp:<20s}  "
            f"P/C={pc}  "
            f"Call vol={signal.call_vol:.0f}  "
            f"Put vol={signal.put_vol:.0f}  "
            f"({signal.factors_active}/4 factors active)"
        )
        for factor, direction in signal.factors.items():
            indicator = direction or 'neutral'
            print(f"    {factor:<24s}: {indicator}")

        # Top 10 by liquidity — only show rows with a real quote or volume.
        # Without this filter, hundreds of zero-score rows (bid=NaN, vol=0)
        # all tie at liquidity_score=0.0 and sort randomly to the top.
        available = [c for c in _DISPLAY_COLS if c in df.columns]
        has_data  = (
            df['bid'].notna() | df['ask'].notna() |
            (pd.to_numeric(df['volume'], errors='coerce').fillna(0) > 0)
        )
        display_df = df[has_data] if has_data.any() else df
        top10      = display_df.sort_values('liquidity_score', ascending=False).head(10)
        print(f"\n  Top 10 by liquidity score:")
        with pd.option_context('display.float_format', '{:.4f}'.format,
                               'display.max_columns', 20,
                               'display.width', 200):
            print(top10[available].to_string(index=False))

        # Fingerprint findings
        if fp_findings:
            print(f"\n  Fingerprint findings ({len(fp_findings)}):")
            for fp in fp_findings[:5]:
                print(f"    [{fp.model}][{fp.confidence:.2f}] {fp.note}")

        # Alerts
        if alerts:
            print(f"\n{'*' * 100}")
            for a in alerts:
                print(a)
            print('*' * 100)

        print(sep)
