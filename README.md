# Options Scanner

Multi-instrument options market scanner for CL (Crude Oil), SI (Silver), and TSLA.
Monitors option expiries for sentiment, unusual volume, and unidirectional signals
using IBKR market data.

---

## Directory structure

```
options_scanner/
├── config.py                   All configuration (env-driven)
├── main.py                     Entry point / orchestrator
├── run_tests.py                Self-contained test runner
├── requirements.txt
├── .env.example                Copy to .env and fill in
│
├── models/                     Pricing models (Black-76, BSM, Merton JD)
├── data/                       IB market data layer + contract cache
├── signals/                    Signal engine + fingerprint models
├── io/                         Alerts, CSV logging, state persistence
├── scanner/                    Per-instrument scan loop
└── tests/                      221+ unit tests
```

Runtime directories created automatically:

```
cache/      Contract metadata (reference data, future DB seed)
logs/       Operational: scan CSVs, alert CSVs, state.json
```

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env: set IB_HOST, IB_PORT, IB_CLIENT_ID

# 3. Run tests (no IB connection needed)
python run_tests.py

# 4. Start scanner
python main.py
```

---

## Running the scanner

```bash
# Normal start (uses cache if available, discovers if not)
python main.py

# Force full re-discovery of all contracts
python main.py --refresh

# Force re-discovery of one instrument only
python main.py --refresh-instrument CL

# Refresh cache then exit (for cron — see below)
python main.py --refresh --exit-after-cache
```

---

## Contract cache

Contracts (strikes, expiries, conIds) are cached in `cache/` so the scanner
starts instantly without waiting for IB discovery.

Cache files:
- `cache_{INSTRUMENT}.json`    — active contracts currently in use
- `archive_{INSTRUMENT}.json`  — all historically seen contracts (never pruned)

The archive is the seed for a future local database.

### Automatic refresh triggers

The cache manager checks every `CACHE_CHECK_INTERVAL_SEC` seconds (default 5 min):

| Trigger | Condition | Default |
|---------|-----------|---------|
| TTL | Cache older than `CACHE_TTL_HOURS` | 24h |
| Wall-clock | `CACHE_REFRESH_TIME_ET` passed today and not yet refreshed | 06:00 ET |
| Moneyness drift | Underlying price moved > `CACHE_MONEYNESS_DRIFT_THRESHOLD` since last discovery | 5% |

Refreshes are per-instrument and run in the background — the scanner continues
on the existing cache and atomically swaps to the new contracts when done.

### Manual refresh (while scanner is running)

Write to `cache/refresh_request` — the scanner picks it up within
`CACHE_CHECK_INTERVAL_SEC` seconds:

```bash
# Refresh one instrument
echo "CL" > cache/refresh_request

# Refresh multiple
printf "CL\nSI\n" > cache/refresh_request

# Refresh all
echo "ALL" > cache/refresh_request

# Or use the CLI helper (does the same thing)
python main.py --request-refresh CL
python main.py --request-refresh ALL
```

### Cron setup

Refresh contracts once per day at 06:00 ET before the US market open:

```bash
# Edit crontab: crontab -e
# Refresh all instruments at 10:00 UTC (= 06:00 ET, adjust for DST)
0 10 * * 1-5 cd /path/to/options_scanner && python main.py --refresh --exit-after-cache >> logs/cron.log 2>&1
```

The `--exit-after-cache` flag ensures the cron job exits cleanly after
discovery without starting the scan loop.

---

## Running tests

```bash
# All tests (no IB connection needed)
python run_tests.py

# One test file
python run_tests.py test_black76

# One test class
python run_tests.py test_black76.TestBlack76Price

# One specific test
python run_tests.py test_black76.TestBlack76Price.test_put_call_parity_atm

# With pytest (if installed)
pytest tests/
pytest tests/test_contract_cache.py -v
```

---

## Configuration reference

All values can be set in `.env` or as environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `IB_HOST` | `127.0.0.1` | IB Gateway host |
| `IB_PORT` | `4002` | IB Gateway port (4002=paper, 4001=live) |
| `IB_CLIENT_ID` | `42` | IB client ID |
| `SCAN_INTERVAL_SEC` | `60` | Seconds between scans |
| `MAX_EXPIRY_DAYS` | `30` | Maximum expiry window |
| `MONEYNESS_BAND` | `0.20` | ±% band around underlying price |
| `RISK_FREE_RATE` | `0.05` | Annual risk-free rate |
| `USE_STREAMING_TICKS` | `false` | Enable tick-by-tick streaming (Option A) |
| `CACHE_DIR` | `./cache` | Contract cache directory |
| `CACHE_TTL_HOURS` | `24` | Cache TTL before scheduled refresh |
| `CACHE_REFRESH_TIME_ET` | `06:00` | Daily wall-clock refresh time (ET) |
| `CACHE_CHECK_INTERVAL_SEC` | `300` | How often to check for refresh triggers |
| `CACHE_MONEYNESS_DRIFT_THRESHOLD` | `0.05` | Price drift % to trigger chain refresh |
| `LOG_DIR` | `./logs` | Operational log directory |
| `ALERT_SUPPRESSION_SCANS` | `5` | Suppress repeat alerts for N scans |

---

## Fingerprint model config reference

All fingerprint thresholds live in `FINGERPRINT_CONFIG` in `config.py` and can be overridden via environment variables.

| Env var | Default | Description |
|---------|---------|-------------|
| `SWEEP_MIN_PRINT_SIZE` | `10` | ★ Minimum per-strike volume delta (contracts) for a strike to count as swept in snapshot mode. Also minimum individual print size in tick mode. Set to `0` to disable (count any positive delta). |
| `OI_PC_MIN_OBSERVATIONS` | `10` | ★ Minimum non-None OI rows across the moneyness band before OI P/C ratio is computed. Guards against false alerts at session open before IB populates OI. |
| `OI_PC_BEARISH_THRESHOLD` | `1.5` | ★ Put OI / Call OI ratio above this value emits a bearish OI P/C finding. |
| `OI_PC_BULLISH_THRESHOLD` | `0.67` | ★ Put OI / Call OI ratio below this value emits a bullish OI P/C finding. |
| `OI_PC_SHIFT_THRESHOLD` | `0.20` | ★ Relative change in OI P/C ratio vs previous scan (e.g. `0.20` = 20%) that triggers a shift finding. Set to `0.0` to disable shift detection. |
| `OI_PC_NEAR_EXPIRY_COUNT` | `2` | ★ Number of nearest expiries for which a separate OI P/C breakdown is computed. Set to `0` to disable per-expiry breakdown. |

★ = new in this release; must be set in `.env` to override defaults.

---

## Pricing models

| Model | Key | Instruments | Notes |
|-------|-----|-------------|-------|
| Black-76 | `black76` | CL, SI | Futures options |
| BSM | `bsm` | TSLA | Equity options, supports dividend yield |
| Merton Jump-Diffusion | `merton_jd` | Any | Stub wired, params in config |

Switch model per instrument in `config.py` `INSTRUMENTS` dict.

---

## Adding a new instrument

1. Add entry to `INSTRUMENTS` in `config.py`
2. Set `secType`: `'FOP'` for futures options, `'OPT'` for equity
3. Set `exchange`, `model`, and optional jump-diffusion params
4. Run `python main.py --refresh` to discover contracts

## Adding a new fingerprint model

1. Create `signals/fingerprint/my_model.py` subclassing `BaseFingerprintModel`
2. Implement `update()` and `detect()` returning `list[Finding]`
3. Add an instance to `FINGERPRINT_MODELS` in `signals/fingerprint/engine.py`
4. Write tests in `tests/test_fingerprint.py`
