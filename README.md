# Composite Crypto Trading System Scanner

A production-ready, professional-grade 10-factor multi-timeframe cryptocurrency scanner in Python. Designed to run locally during development or as a serverless cron job (AWS Lambda / GCP Cloud Functions), this tool evaluates market conditions across ten distinct technical, fundamental, sentiment, and on-chain indicators to trigger high-probability Discord/Slack buy and sell alerts.

---

## Key System Indicators & Logic

The system evaluates a coin against a **10-Factor Scorecard** across daily and 4-hour (4H) timeframes:

1. **BTC Macro Filter (Daily)**: Checks if Bitcoin is above its daily 200 EMA. *(Hard Blocker for BUY)*
2. **Daily EMA Alignment (Daily)**: Verifies if price is aligned above EMA 20, EMA 50, and EMA 200 (or EMA 50 > EMA 200 for a partial pass).
3. **RSI 4H Timeframe**: Triggers if 4H RSI is in a strong buy zone ($40 \le \text{RSI} \le 65$), oversold ($<30$), or overbought ($\ge 75$ for sell alerts).
4. **MACD 4H Timeframe**: Evaluates 12/26/9 MACD momentum crossovers and histogram growth.
5. **Volume + OBV (4H)**: Flags volume spikes ($\ge 1.5\times$ 20-period average) and OBV accumulation/divergence.
6. **Bollinger Bands (4H)**: Detects lower band support bounces (Buy) or upper band overextensions (Sell).
7. **Stochastic RSI (4H)**: Detects %K crossing up from below 20 (Buy) or crossing down from above 70 (Sell).
8. **VWAP (4H)**: Measures price testing VWAP from above as support (Buy) or rejection at VWAP from below (Sell).
9. **Fear & Greed Index (Live API)**: Pulls live market sentiment (Buy during Fear 20–40; Sell during Greed 80–100).
10. **ADX & On-Chain Flows (4H)**: Evaluates trend strength (ADX $\ge 25$) paired with whale wallet flows.

### Mandatory Hard Blockers (For BUY Alerts)
Even if a coin scores a high cumulative score, a **BUY alert will only fire** if the following two hard blockers are satisfied:
1. **BTC Trend Filter**: Bitcoin's price must be above its daily 200 EMA (macro protection).
2. **Risk/Reward Ratio**: The target price (nearest resistance) relative to the stop loss (20-bar swing low) must offer a reward-to-risk ratio of **at least 2.0 : 1**.

---

## Local Setup

1. **Open Your Terminal** (PowerShell in VS Code is recommended on Windows) and navigate to the project directory:
   ```powershell
   cd "C:\Users\Darwin\Documents\Darwin's File\wedding-website"
   ```

2. **Activate the Virtual Environment**:
   * **PowerShell**:
     ```powershell
     .\venv\Scripts\Activate.ps1
     ```
   * **Command Prompt (CMD)**:
     ```cmd
     .\venv\Scripts\activate.bat
     ```

3. **Set Your Webhook Environment Variable** (Replace with your actual Discord/Slack webhook URL to receive live notifications):
   * **PowerShell**:
     ```powershell
     $env:ALERT_WEBHOOK_URL="https://discord.com/api/webhooks/your-webhook-id/your-webhook-token"
     ```
   * **Command Prompt (CMD)**:
     ```cmd
     set ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/your-webhook-id/your-webhook-token
     ```

---

## Testing Guide (Symmetrical Mock Alerts)

Because public exchange APIs enforce strict rate limits, the scanner includes **high-fidelity mock simulation profiles**. You can run these simulated profiles to verify the exact checklist scoring math and test the HTTP webhook notifications instantly.

### 1. Symmetrical Mock Testing (Guaranteed Alerts)

#### Test a BUY Signal Notification
Forces a high-conviction bullish pullback profile (extreme fear, low RSI, Stochastic RSI cross-up, MACD golden cross, and strong risk/reward) to trigger and send a **9/10 BUY alert**:
```powershell
python crypto_scanner.py --coin solana --mock --force-buy
```

#### Test a SELL Signal Notification
Forces a high-conviction bearish breakdown profile (extreme greed, overbought RSI, Stochastic RSI cross-down, MACD death cross, and heavy whale exchange inflows) to trigger and send a **10/10 SELL alert**:
```powershell
python crypto_scanner.py --coin solana --mock --force-sell
```

---

### 2. Watchlist Mock Scanning

Runs the mock scanner sequentially across the top 5 coins: `["bitcoin", "ethereum", "solana", "ripple", "binancecoin"]`. 

*Note: The mock generator hashes the coin names so that each asset evaluates a completely unique synthetic pricing curve and indicator output, preventing identical spammed results.*
```powershell
python crypto_scanner.py --watchlist --mock
```

---

## Live Market Testing (Actual Exchange Data)

When you are ready to evaluate real-time market data, omit the `--mock` flag. The system will retrieve 250 daily candles and 30 days of hourly ticks from the public CoinGecko API, resample them to 4H bars, query the live Fear & Greed Index API, and run the indicators.

*Pacing Delay: The live scanner enforces an 8-second pacing sleep between sequential API requests to safely navigate public rate-limiting tiers.*

## Historical CSV collection

Every successful live scan saves market observations and the resulting decision under `data/history/`:

- `market_candles.csv` stores coin, timeframe, UTC timestamp, price, volume, high, low, and data source. It records closed daily price observations and resampled 4-hour observations. The 4-hour high/low fields are estimates derived from CoinGecko price points, not exchange-reported OHLC candles; daily volume/high/low are blank because this scanner does not fetch them.
- `scan_decisions.csv` stores scan time, scores, alert states, and the indicator metrics as JSON. Mock scans are marked `mock` so they can be excluded from real-market analysis. The decision metrics also identify the on-chain values as mock configuration; they are not live on-chain data.

Rows are deduplicated by coin, timeframe, and candle timestamp, so the rolling 250-day and 30-day API windows do not get copied into the CSV on every run. Set `CRYPTO_HISTORY_DIR` to choose another local output directory. The generated `data/history/` files are git-ignored.

The GitHub Actions workflow restores and updates the history through its cache so its normally temporary runner can carry the CSVs forward between scans. Caches are a convenient starter store, not a permanent archive; download the CSVs periodically if you need a durable backup. Locally run `python crypto_scanner.py --watchlist` to collect live history, or `python crypto_scanner.py --coin bitcoin` for one coin. Mock runs only add decision rows and never add synthetic candles to market history.

### 1. Scan a Single Live Asset
Scan **Solana** on real-time market data:
```powershell
python crypto_scanner.py --coin solana
```

Scan **Ethereum** on real-time market data:
```powershell
python crypto_scanner.py --coin ethereum
```

### 2. Scan a Live Watchlist
Scan all 5 top watchlist assets sequentially on real-time market data:
```powershell
python crypto_scanner.py --watchlist
```

### 3. Live Scan with Position Sizing Overrides
You can manually override targets, stop losses, and resistance boundaries to stress-test live checklist triggers:
```powershell
python crypto_scanner.py --coin solana --target 200 --stop 145 --resistance 205
```
