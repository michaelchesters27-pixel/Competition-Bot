# EVE Competition Scalper

A complete **Railway-hosted frontend and backend** plus an **MT5 Expert Advisor** for the autonomous XAUUSD M5 competition project.

## Locked operating rules

- Market: **XAUUSD** (broker suffixes such as `XAUUSD.a` are accepted)
- Timeframe: **M5**
- Lot size: **fixed 0.01 lots on every entry**
- Session starts automatically when the EA first connects after being attached
- Phase 1: **exactly 60 minutes of research; trading is blocked**
- Phase 2: **exactly 60 minutes of autonomous trading**
- No Netlify
- Railway serves the API and browser dashboard from one service
- No invented session-loss cap, losing-trade cap, cooldown, martingale, grid, or forced end-of-session close
- No attempt to observe or infer competitors' strategies, positions, P/L, or ranking

The backend resumes an unfinished session if the EA is removed and reattached before the two-hour window ends. Once that window is complete, the next attachment starts a new session.

## What Phase 1 does

The EA uploads recent XAUUSD M5 history and then keeps the Railway engine updated as each M5 candle closes. The engine calculates and evaluates:

- Tillson T3 fast and slow trend state
- Squeeze Momentum compression and release
- Momentum direction and acceleration
- Chaikin Volatility and its change
- Change of Volatility based on ATR
- ATR
- Candle body quality
- Recent M5 structure breaks

It chronologically backtests five candidate strategies against the supplied MT5 history and refreshes the ranking during the research hour:

1. T3 Squeeze Release
2. T3 Pullback Resume
3. Volatility Structure Break
4. T3 Momentum Continuation
5. Squeeze Mean Reversion

At the end of the exact first hour, the highest research score is selected and frozen.

## What Phase 2 does

The frozen strategy is evaluated once per newly closed M5 candle. When its full conditions are met, Railway sends a BUY or SELL instruction containing:

- signal ID
- direction
- dynamic ATR-based stop-loss
- dynamic strategy-specific take-profit
- setup confidence
- exact entry reasons
- complete indicator snapshot

The MT5 EA executes the instruction at **0.01 lots**, acknowledges the broker result, and reports every opening and closing deal back to Railway.

The engine does not open trades during Phase 1 and does not issue new entries after Phase 2 finishes. It does not forcibly close a position that remains open after the competition window; the position continues toward its existing SL or TP and is still reported.

## Full reporting

The Railway dashboard includes:

- exact phase and countdown
- MT5 connection status
- live research ranking
- frozen strategy
- trade count
- realised P/L
- win rate
- profit factor
- average R
- open positions
- engine activity log
- expandable report for every entry

Each trade report includes the signal, reasons, indicator state, direction, volume, entry, exit, SL, TP, confidence, commission, swap, net P/L, R result, MFE, MAE, duration, and all linked MT5 deal activity.

The reporting ledger handles hedging and netting accounts by allocating closing deals back to individual opening deals.

---

# Deploy the Railway project

## 1. Put the project into GitHub

Create a new empty GitHub repository and upload the **contents of this folder** to the repository root.

The repository root must contain:

- `app.py`
- `requirements.txt`
- `railway.json`
- `Procfile`
- `eve_app/`
- `templates/`
- `mql5/`

Do not upload the outer ZIP as a single file inside GitHub. Extract it first and upload the contents.

## 2. Create the Railway service

1. Open Railway.
2. Choose **New Project**.
3. Choose **Deploy from GitHub repo**.
4. Select the repository.
5. Wait for the first deployment.

Railway will install the Python dependencies and use the start command in `railway.json`.

## 3. Add the API key variable

In the Railway service:

1. Open **Variables**.
2. Add:

```text
EVE_API_KEY=choose-a-private-key-here
```

Use a private value with letters and numbers. You must enter the exact same value in the MT5 EA inputs.

## 4. Attach persistent storage

The session clock, research scores, signals, deals, and reports use SQLite.

1. Open the Railway service.
2. Attach a **Volume**.
3. Set its mount path to:

```text
/data
```

Railway provides the mount path to the application automatically. Without a volume, the app still runs, but the database can be lost when Railway replaces the deployment container.

## 5. Generate the Railway domain

1. Open the service **Settings**.
2. Open **Networking**.
3. Choose **Generate Domain**.
4. Copy the complete HTTPS address.

Example format:

```text
https://your-service-name.up.railway.app
```

Open that address in the browser. The Railway dashboard should load and show **Waiting for MT5**.

---

# Install the MT5 EA

The EA source file is:

```text
mql5/EVE_Competition_Scalper.mq5
```

## 1. Copy it into MetaEditor

1. Open MT5.
2. Press **F4** to open MetaEditor.
3. In MetaEditor choose **File → Open Data Folder**.
4. Open `MQL5`.
5. Open `Experts`.
6. Copy `EVE_Competition_Scalper.mq5` into that folder.
7. Open the file in MetaEditor.
8. Press **F7** to compile it.

The compiled file will appear as `EVE_Competition_Scalper.ex5`.

## 2. Allow the Railway connection in MT5

1. In MT5 choose **Tools → Options**.
2. Open **Expert Advisors**.
3. Tick **Allow WebRequest for listed URL**.
4. Add your Railway base URL exactly, without `/api` at the end.

Example:

```text
https://your-service-name.up.railway.app
```

5. Press **OK**.

## 3. Attach the EA

1. Open the broker's XAUUSD chart.
2. Set the chart to **M5**.
3. Drag `EVE_Competition_Scalper` onto the chart.
4. In the EA inputs set:

```text
RailwayBaseUrl = your complete Railway HTTPS domain
ApiKey         = the exact EVE_API_KEY value used in Railway
FixedLots      = 0.01
```

5. Tick **Allow Algo Trading**.
6. Press **OK**.
7. Turn on the main **Algo Trading** button in MT5.

The two-hour session begins when the EA successfully connects. The chart comment and Railway dashboard show the session ID, current phase, countdown, and selected strategy.

---

# Important first-run checks

Before treating it as a competition run, attach it to a demo account and confirm:

1. The chart says `Phase: RESEARCH`.
2. The Railway dashboard changes from **Waiting for MT5** to **MT5 connected**.
3. Research rankings appear after the initial history upload.
4. No order is opened during the first hour.
5. The MT5 **Experts** tab has no WebRequest or authentication errors.

If MT5 shows a WebRequest error, the usual cause is that the Railway URL was not added to **Tools → Options → Expert Advisors**.

If Railway returns `UNAUTHORIZED`, the EA `ApiKey` does not exactly match Railway's `EVE_API_KEY` variable.

If the EA refuses to initialise, verify that the chart is XAUUSD M5 and the broker supports an exact 0.01-lot order on that symbol.

## Files

- `mql5/EVE_Competition_Scalper.mq5` — MT5 execution and reporting bridge
- `app.py` — Railway web application and API
- `eve_app/engine.py` — exact session clock and phase controller
- `eve_app/indicators.py` — T3, Squeeze Momentum, Chaikin Volatility, Change of Volatility, ATR, and structure features
- `eve_app/strategies.py` — candidate strategies, chronological research backtests, strategy selection, and live signals
- `eve_app/storage.py` — persistent SQLite storage
- `eve_app/reporting.py` — per-entry trade ledger and statistics
- `templates/index.html` — Railway-hosted dashboard
- `tests/` — tested core research, session, and reporting logic
