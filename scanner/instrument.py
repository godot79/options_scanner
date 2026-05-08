"""
scanner/instrument.py
---------------------
Per-instrument scan orchestration.
"""

import asyncio
from datetime import datetime, timezone, timedelta
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
from options_scanner.data.ib_client import (
    EquityStream, OIStream, FuturesTickStream, FuturesMomentum,
    qualify_chain_for_scan,
)
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

        self.underlying_contracts : list = []
        self.option_contracts     : list = []
        self.details_cache        : dict = {}
        self._discovered          : bool = False

        self._tick_streams         : dict[int, TickStream]         = {}
        self._equity_stream        : 'EquityStream | None'         = None
        self._oi_streams           : dict[int, OIStream]           = {}
        self._futures_tick_streams : dict[int, FuturesTickStream]  = {}
        self._chain_specs          : list                          = []

    # ── Discovery ─────────────────────────────────────────────────────────────

    async def discover(self) -> None:
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

        if self.cfg['secType'] == 'OPT':
            self._equity_stream = EquityStream(self.ib, self.cfg)
            await self._equity_stream.start()

        if self.cfg['secType'] == 'FOP':
            await self._start_oi_streams()

        if cfg.USE_STREAMING_TICKS:
            self._start_tick_streams()

    def _load_from_cache(self) -> None:
        self._chain_specs         = self.cache_manager.serve_chain_specs(self.key)
        self.underlying_contracts = self.cache_manager.serve_underlying(self.key)
        self.option_contracts     = []
        self.details_cache        = {}
        total = sum(len(s.expirations) * len(s.strikes) * 2
                    for s in self._chain_specs)
        print(f"[{self.key}] Loaded from cache: "
              f"{len(self._chain_specs)} chain specs "
              f"({total} theoretical contracts), "
              f"{len(self.underlying_contracts)} underlying futures")

    # ── OI + momentum streams (FOP only) ─────────────────────────────────────

    async def _start_oi_streams(self) -> None:
        for fut in self.underlying_contracts:
            oi_stream = OIStream(self.ib, fut)
            await oi_stream.start()
            self._oi_streams[fut.conId] = oi_stream

            ft_stream = FuturesTickStream(self.ib, fut)
            if oi_stream._ticker is not None:
                ft_stream.set_oi_ticker(oi_stream._ticker)
            ft_stream.start()
            self._futures_tick_streams[fut.conId] = ft_stream

        if self._oi_streams:
            print(f"[{self.key}] OI + tick streams started for "
                  f"{len(self._oi_streams)} underlying futures.")

    def _stop_oi_streams(self) -> None:
        for stream in self._oi_streams.values():
            stream.stop()
        self._oi_streams.clear()
        for stream in self._futures_tick_streams.values():
            stream.stop()
        self._futures_tick_streams.clear()

    async def _discover_fop(self) -> None:
        futs = await discover_futures(self.ib, self.cfg)
        if not futs:
            print(f"[{self.key}][WARN] No futures found — scanner inactive.")
            return
        self.underlying_contracts = futs
        await asyncio.sleep(1.0)
        self.option_contracts = await discover_fop_chain(
            self.ib, self.cfg, self.details_cache, futures=futs
        )

    async def _discover_equity(self) -> None:
        underlying, opts = await discover_equity_options(
            self.ib, self.cfg, self.details_cache
        )
        self.underlying_contracts = underlying
        self.option_contracts     = opts

    # ── Option tick streams (Option A) ───────────────────────────────────────

    def _start_tick_streams(self) -> None:
        for opt in self.option_contracts:
            stream = TickStream(self.ib, opt)
            stream.start()
            self._tick_streams[opt.conId] = stream
        print(f"[{self.key}] Option tick streams started for "
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
        if self.cfg['secType'] != 'FOP' or not price_map:
            return contracts

        price = next((v for v in price_map.values() if v and v > 0), None)
        if not price:
            return contracts

        lo = price * (1 - cfg.MONEYNESS_BAND)
        hi = price * (1 + cfg.MONEYNESS_BAND)

        filtered = [
            c for c in contracts
            if lo <= getattr(c, 'strike', 0) <= hi
        ]
        if not filtered:
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

        # ── 1. Drain futures tick streams ─────────────────────────────────
        momentum_by_conid: dict[int, FuturesMomentum] = {}
        for conid, ft_stream in self._futures_tick_streams.items():
            momentum_by_conid[conid] = ft_stream.drain_momentum()

        # ── 2. Underlying price from OI streams ───────────────────────────
        underlying_price_map: dict = {}

        if self.cfg['secType'] == 'FOP':
            for fut in self.underlying_contracts:
                stream = self._oi_streams.get(fut.conId)
                if stream is None:
                    continue
                p = stream.price()
                if p is not None and p > 0:
                    underlying_price_map[fut.conId] = p

            if not underlying_price_map:
                print(f"[{self.key}][WARN] No underlying price from OI streams "
                      f"— skipping scan.")
                return
        else:
            underlying_price_map['equity'] = None

        # ── 3. OPT: resolve equity price before qualify ───────────────────
        if self.cfg['secType'] == 'OPT':
            early_price: Optional[float] = None
            if self._equity_stream is not None:
                early_price = self._equity_stream.price()
            if early_price and early_price > 0:
                underlying_price_map['equity'] = float(early_price)
            else:
                print(f"[{self.key}] Skipping scan — underlying price not yet available.")
                return

        # ── 4. Qualify + snapshot option contracts ────────────────────────
        und_price = next(
            (v for v in underlying_price_map.values() if v and v > 0), None
        )
        if self._chain_specs:
            contracts_to_quote = await qualify_chain_for_scan(
                self.ib,
                self._chain_specs,
                und_price,
                cfg.MONEYNESS_BAND,
                self.details_cache,
            )
            if not self.underlying_contracts and self.cache_manager:
                self.underlying_contracts = \
                    self.cache_manager.serve_underlying(self.key)
        else:
            contracts_to_quote = self._apply_moneyness_prefilter(
                self.option_contracts, underlying_price_map
            )
        opt_tickers = await fetch_snapshot(self.ib, contracts_to_quote)

        if self.cfg['secType'] == 'OPT':
            equity_price = None
            if self._equity_stream is not None:
                equity_price = self._equity_stream.price()
            if not equity_price or equity_price <= 0:
                for t in opt_tickers.values():
                    val = getattr(t, 'undPrice', None)
                    if val and float(val) > 0:
                        equity_price = float(val)
                        break
            if not equity_price or equity_price <= 0:
                for t in opt_tickers.values():
                    for field in ('last', 'close'):
                        val = getattr(t, field, None)
                        if val and float(val) > 0:
                            equity_price = float(val)
                            break
                    if equity_price and equity_price > 0:
                        break
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

        # ── 5. Build raw DataFrame ─────────────────────────────────────────
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

        # ── 6. Liquidity metrics ──────────────────────────────────────────
        df = compute_liquidity_metrics(df)

        # ── 7. Moneyness filter ───────────────────────────────────────────
        def _in_band(row: pd.Series) -> bool:
            price = underlying_price_map.get(row['und_key'])
            if not price or price <= 0:
                return True
            K = row['strike']
            return (K >= price * (1 - cfg.MONEYNESS_BAND) and
                    K <= price * (1 + cfg.MONEYNESS_BAND))

        df = df[df.apply(_in_band, axis=1)].copy()

        if df.empty:
            print(f"[{self.key}] No options within ±{cfg.MONEYNESS_BAND*100:.0f}% band.")
            return

        # ── 8. IV & Greeks ────────────────────────────────────────────────
        iv_col, delta_col, gamma_col, vega_col, theta_col = [], [], [], [], []

        for _, row in df.iterrows():
            price = underlying_price_map.get(row['und_key'])
            T     = year_fraction(str(row['expiry']), now)
            mid   = row.get('mid')

            import math as _math
            if (not mid or mid <= 0 or (_math.isfinite(mid) is False)
                    or not price or price <= 0):
                iv_col.append(None); delta_col.append(None)
                gamma_col.append(None); vega_col.append(None)
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

        # ── 9. Volume history ─────────────────────────────────────────────
        call_vol = float(df[df['right'] == 'C']['volume'].fillna(0).sum())
        put_vol  = float(df[df['right'] == 'P']['volume'].fillna(0).sum())
        self.vol_history.record(call_vol, put_vol)

        primary_price_for_cache = next(
            (v for v in underlying_price_map.values() if v and v > 0), None
        )
        if self.cache_manager and primary_price_for_cache:
            self.cache_manager.report_price(self.key, primary_price_for_cache)

        # ── 10. Signal evaluation ─────────────────────────────────────────
        signal = signal_evaluate(df, self.vol_history)

        # ── 11. Fingerprint ───────────────────────────────────────────────
        tick_prints = self._drain_tick_prints() if cfg.USE_STREAMING_TICKS else None
        self.fp_engine.update(self.key, df, tick_prints)
        fp_findings = self.fp_engine.detect(self.key)

        # ── 12. Alerts ────────────────────────────────────────────────────
        console_alerts: list[str] = []

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

        for fp in fp_findings:
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

        # ── 13. Console output ────────────────────────────────────────────
        primary_price = next(iter(underlying_price_map.values()), None)
        self._print_scan(now, df, signal, console_alerts,
                         fp_findings, underlying_price_map, momentum_by_conid)

        # ── 14. CSV ───────────────────────────────────────────────────────
        self.csv_logger.write_scan(self.key, df, signal, primary_price)

    # ── Console output ────────────────────────────────────────────────────────

    def _print_scan(self,
                    now              : datetime,
                    df               : pd.DataFrame,
                    signal,
                    alerts           : list[str],
                    fp_findings      : list,
                    price_map        : dict,
                    momentum_map     : 'dict[int, FuturesMomentum]') -> None:

        sep  = '─' * 100
        ts   = now.strftime('%Y-%m-%d %H:%M:%S UTC')
        name = self.cfg['description']

        print(f"\n{sep}")
        print(f"  {self.key:6s}  │  {name:40s}  │  {ts}")
        print(sep)

        # ── Underlying prices ─────────────────────────────────────────────
        for k, v in price_map.items():
            label = 'Underlying' if k == 'equity' else f'Future {k}'
            val   = f"{v:.5g}" if v is not None else 'N/A'
            print(f"  {label}: {val}")

        # ── Futures momentum ──────────────────────────────────────────────
        if momentum_map:
            print(f"\n  ── Futures momentum (since last scan) ───────────────────────────")
            for conid, m in momentum_map.items():
                sym = m.local_symbol or str(conid)
                if m.tick_count == 0:
                    print(f"  {sym:12s}  no ticks")
                    continue

                delta_str = (f"{m.price_delta:+.3f}" if m.price_delta is not None
                             else 'N/A')
                vwap_str  = (f"{m.vwap:.3f}" if m.vwap is not None else 'N/A')

                if m.buy_pct is not None:
                    sell_pct  = 1.0 - m.buy_pct
                    buy_str   = f"{m.buy_pct*100:.0f}%"
                    sell_str  = f"{sell_pct*100:.0f}%"
                else:
                    buy_str = sell_str = 'n/c'

                classified_str = (f"{m.classified_pct*100:.0f}%"
                                  if m.classified_pct is not None else 'n/c')

                arrow = ('▲' if (m.price_delta or 0) > 0
                         else ('▼' if (m.price_delta or 0) < 0 else '─'))

                print(
                    f"  {sym:12s}  {arrow} Δ={delta_str:>8s}  "
                    f"VWAP={vwap_str}  "
                    f"ticks={m.tick_count:4d}  vol={m.total_vol:7.0f}  "
                    f"buy={m.buy_vol:6.0f}({buy_str})  "
                    f"sell={m.sell_vol:6.0f}({sell_str})  "
                    f"classif={classified_str}"
                )

        # ── Signal summary ────────────────────────────────────────────────
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
            print(f"    {factor:<24s}: {direction or 'neutral'}")

        # ── Top 10 by liquidity — filtered to near-expiry ─────────────────
        # Only show options with a real quote or volume.
        available = [c for c in _DISPLAY_COLS if c in df.columns]
        has_data  = (
            df['bid'].notna() | df['ask'].notna() |
            (pd.to_numeric(df['volume'], errors='coerce').fillna(0) > 0)
        )
        display_df = df[has_data] if has_data.any() else df

        # Apply near-expiry filter from config.
        # DISPLAY_NEAR_EXPIRY_DAYS=0 means disabled (show all expiries).
        # Fallback: if the filter leaves nothing displayable, show the nearest
        # available expiry that has data — but determine whether ANY expiry
        # exists within the window by checking the FULL df (not display_df),
        # so that today's expiry with no quotes is still recognised as in-window
        # and the label is accurate.
        near_days    = getattr(cfg, 'DISPLAY_NEAR_EXPIRY_DAYS', 2)
        expiry_label = 'all expiries'

        if near_days > 0 and 'expiry' in df.columns:
            today_str  = now.strftime('%Y%m%d')
            cutoff_str = (now + timedelta(days=near_days)).strftime('%Y%m%d')

            # Check full df (including no-data rows) for expiries in window
            all_expiries_in_window = sorted(
                e for e in df['expiry'].dropna().unique()
                if today_str <= e <= cutoff_str
            )

            # Apply the filter to display_df (rows that have actual quote data)
            near_mask = (
                display_df['expiry'].notna() &
                (display_df['expiry'] >= today_str) &
                (display_df['expiry'] <= cutoff_str)
            )
            near_df = display_df[near_mask]

            if not near_df.empty:
                display_df   = near_df
                expiry_label = (f"expiries within {near_days}d "
                                f"(≤{cutoff_str})")
            elif all_expiries_in_window:
                # Expiries exist in-window but none have displayable data
                # (e.g. today's expiry has no quotes). Show nearest with data
                # but label correctly — don't claim there's nothing within 2d.
                available_with_data = sorted(display_df['expiry'].dropna().unique())
                if available_with_data:
                    nearest      = available_with_data[0]
                    display_df   = display_df[display_df['expiry'] == nearest]
                    expiry_label = (
                        f"nearest expiry ({nearest}) — "
                        f"no data for in-window expir{'y' if len(all_expiries_in_window) == 1 else 'ies'} "
                        f"({', '.join(all_expiries_in_window)})"
                    )
                else:
                    expiry_label = f"expiries within {near_days}d (≤{cutoff_str})"
            else:
                # Nothing in window at all — fall back to nearest available expiry
                available_expiries = sorted(display_df['expiry'].dropna().unique())
                if available_expiries:
                    nearest      = available_expiries[0]
                    display_df   = display_df[display_df['expiry'] == nearest]
                    expiry_label = f"nearest expiry ({nearest}) — none within {near_days}d"

        top10 = display_df.sort_values('liquidity_score', ascending=False).head(10)
        print(f"\n  Top 10 by liquidity score ({expiry_label}):")
        with pd.option_context('display.float_format', '{:.4f}'.format,
                               'display.max_columns', 20,
                               'display.width', 200):
            print(top10[available].to_string(index=False))

        # ── Fingerprint findings ──────────────────────────────────────────
        if fp_findings:
            print(f"\n  Fingerprint findings ({len(fp_findings)}):")

            # Build a fast lookup: (expiry, strike_str, right) -> (localSymbol, volume)
            # Used to print per-trade detail under directional volume_cluster findings.
            _trade_lookup: dict[tuple, tuple] = {}
            if 'expiry' in df.columns and 'strike' in df.columns:
                for _, _row in df.iterrows():
                    _expiry = str(_row.get('expiry', ''))
                    _strike = str(_row.get('strike', ''))
                    _right  = str(_row.get('right', ''))
                    _sym    = str(_row.get('localSymbol', ''))
                    _vol    = _row.get('volume')
                    if _expiry and _strike and _right:
                        _trade_lookup[(_expiry, _strike, _right)] = (_sym, _vol)

            for fp in fp_findings[:5]:
                print(f"    [{fp.model}][{fp.confidence:.2f}] {fp.note}")

                # For directional volume_cluster findings, print the individual
                # strikes so the reader can see exactly which trades make up the
                # cluster without needing to cross-reference the full table.
                # "Directional" means n_rights == 1 (calls-only or puts-only).
                ev = fp.evidence or {}
                if (fp.finding_type == 'volume_cluster'
                        and ev.get('direction_count', 2) == 1
                        and ev.get('keys')):
                    for raw_key in ev['keys']:
                        parts = raw_key.split('|')
                        if len(parts) != 3:
                            continue
                        expiry, strike, right = parts
                        lookup_hit = _trade_lookup.get((expiry, strike, str(right)))
                        if lookup_hit:
                            sym, vol = lookup_hit
                            vol_str  = f"{vol:.0f}" if vol is not None else '?'
                            print(f"        {sym or expiry+' '+right:>16s}  "
                                  f"strike={float(strike):>8.2f}  "
                                  f"vol={vol_str:>6s}")
                        else:
                            print(f"        {expiry} {right} K={strike}")

        # ── Alerts ────────────────────────────────────────────────────────
        if alerts:
            print(f"\n{'*' * 100}")
            for a in alerts:
                print(a)
            print('*' * 100)

        print(sep)
