# EVE Competition Scalper v2.00

Autonomous XAUUSD M5 competition system hosted entirely on Railway, with an MT5 Expert Advisor as the execution bridge.

## Locked competition format

- XAUUSD only
- M5 chart
- Fixed 0.01 lots per order
- The two-hour run begins when the v2 EA is attached and initialises
- First 60 minutes: research only; MT5 trading is blocked
- Second 60 minutes: autonomous execution
- Railway serves the backend, database, API and browser dashboard
- No Netlify

## What changed in v2

The first build froze one rare entry trigger. If that trigger never appeared, the entire trading hour could finish with zero trades.

Version 2 freezes a **research-ranked playbook**, not one signal type. The first hour identifies the live XAUUSD regime and ranks six modules:

1. Adaptive Directional Scalp
2. T3 Pullback Resume
3. T3 Momentum Continuation
4. Volatility Structure Break
5. T3 Squeeze Release
6. Squeeze Mean Reversion

During the trading hour, every fully closed M5 candle is assessed by every module. The strongest qualifying setup is sent to MT5. The dashboard records both executed signals and M5 HOLD decisions.

Historical M5 bars are used to warm the T3, squeeze, Chaikin Volatility, Change of Volatility and ATR calculations. The research ranking itself is based on the actual first-hour competition window and the regime observed in it.

## Deploy the Railway update

This ZIP is a complete repository replacement, not a patch.

1. Extract the ZIP.
2. Replace the contents of the existing `Competition-Bot` GitHub repository with the contents of the extracted project folder.
3. Commit and push all files.
4. Wait for the existing Railway service to redeploy successfully.
5. Open your existing Railway dashboard domain and confirm the page loads.

The existing Railway domain remains:

`https://competition-bot-production-9b15.up.railway.app`

The database migration is automatic. Existing reports remain stored, but each fresh EA attachment gets a new session and a new two-hour clock.

## Install the MT5 EA

Do not attach it until the restarted competition is ready, because attachment starts the research clock.

1. Open MT5.
2. Press `F4` to open MetaEditor.
3. Open the MT5 data folder and place `EVE_Competition_Scalper.mq5` in `MQL5/Experts`.
4. Open the file in MetaEditor.
5. Confirm the `RailwayBaseUrl` input contains the existing Railway domain.
6. Confirm the `ApiKey` matches the `EVE_API_KEY` Railway variable already used by the working v1 connection.
7. Press `F7` to compile.
8. In MT5, open **Tools → Options → Expert Advisors**.
9. Enable **Allow WebRequest for listed URL** and add:

   `https://competition-bot-production-9b15.up.railway.app`

10. Remove the old EA from the chart.
11. Open XAUUSD on M5.
12. Attach `EVE_Competition_Scalper` only when the competition clock should begin.
13. Enable Algo Trading.

## Fresh-session behaviour

A new launch identifier is created when the EA is freshly attached, so the completed v1 session will not be resumed. A terminal restart can resume the same attachment launch. Removing, recompiling or changing the EA inputs clears the launch identifier so the next attachment begins a fresh run.

## Railway variables

Keep the existing variable:

- `EVE_API_KEY` — must exactly match the EA `ApiKey` input

Railway supplies `PORT` automatically.

For persistent SQLite storage, keep the Railway volume mounted. The application uses `RAILWAY_VOLUME_MOUNT_PATH` automatically when it is available.

## Trade reporting

Every issued trade stores:

- entry module
- buy or sell direction
- M5 signal candle
- entry reasons
- confidence
- SL and TP
- T3 values and slopes
- squeeze state and momentum
- Chaikin Volatility
- Change of Volatility
- ATR
- body ratio and breakout state
- research regime and module rank
- every MT5 deal
- commission and swap
- net P/L
- result in R
- MFE and MAE where candle history is available

## Verification performed

The Python application passes six automated tests covering:

- actual-window research ranking
- fresh sessions for fresh EA attachments
- session resumption for the same launch
- research pulse storage
- playbook freezing
- live signal production on a qualifying M5 trend
- complete entry/exit trade reporting

The `.mq5` source has been statically reviewed here, but MetaEditor is not available in this environment. Compile it with F7 before attachment.
