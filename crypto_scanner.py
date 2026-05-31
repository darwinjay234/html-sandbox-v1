import os
import json
import logging
import requests
import time
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List

# Setup logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Constants & Configuration
# -----------------------------------------------------------------------
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
FEAR_GREED_API_URL = "https://api.alternative.me/fng/"
DEFAULT_COIN       = "solana"
DEFAULT_VS_CURRENCY = "usd"
RSI_PERIOD         = 14

# Option to use a free CoinGecko Developer/Demo API key to increase rate limits to 30 requests/min
COINGECKO_API_KEY   = os.environ.get("COINGECKO_API_KEY", "")

# -----------------------------------------------------------------------
# Discord configuration
#
# You need TWO environment variables in GitHub Secrets:
#
#   ALERT_WEBHOOK_URL  — your existing webhook for buy/sell alerts
#                        (goes to #buy-sell-alerts channel)
#
#   DISCORD_BOT_TOKEN  — a bot token for creating monthly log channels
#                        and posting hourly summaries
#
#   DISCORD_GUILD_ID   — your Discord server (guild) ID
#                        Right-click your server icon → Copy Server ID
#                        (requires Developer Mode on in Discord settings)
#
#   LOG_CATEGORY_ID    — the ID of the category under which monthly log
#                        channels will be created (e.g. "CRYPTO SCANNER")
#                        Right-click the category → Copy ID
# -----------------------------------------------------------------------
ALERT_WEBHOOK_URL  = os.environ.get("ALERT_WEBHOOK_URL",  "https://discord.com/api/webhooks/placeholder-id/placeholder-token")
DISCORD_BOT_TOKEN  = os.environ.get("DISCORD_BOT_TOKEN",  "placeholder-bot-token")
DISCORD_GUILD_ID   = os.environ.get("DISCORD_GUILD_ID",   "placeholder-guild-id")
LOG_CATEGORY_ID    = os.environ.get("LOG_CATEGORY_ID",    "placeholder-category-id")

DISCORD_API_BASE   = "https://discord.com/api/v10"

# -----------------------------------------------------------------------
# TOP 5 WATCHLIST
# -----------------------------------------------------------------------
WATCHLIST = ["bitcoin", "ethereum", "solana", "ripple", "binancecoin"]

# -----------------------------------------------------------------------
# On-Chain smart money flows & resistance (mock / fallback config)
# Tuple: (whale_accumulating, exchange_outflows_rising, large_inflows_detected)
# -----------------------------------------------------------------------
MOCK_ONCHAIN_DATABASE = {
    "solana":      (True,  True,  False),
    "ethereum":    (True,  False, False),
    "bitcoin":     (True,  True,  False),
    "cardano":     (False, False, True),
    "ripple":      (False, False, False),
    "binancecoin": (True,  False, False),
}

MOCK_RESISTANCE_LEVELS = {
    "solana":      185.0,
    "ethereum":    3800.0,
    "cardano":     0.65,
    "ripple":      0.85,
    "dogecoin":    0.22,
    "binancecoin": 720.0,
}


# =======================================================================
# DISCORD BOT HELPERS
# =======================================================================

def print_safe_unicode(content: str) -> None:
    """Prints a string, fallback to safe CP1252 characters if terminal encoding fails."""
    try:
        print(content)
    except UnicodeEncodeError:
        safe_content = content.encode('cp1252', errors='replace').decode('cp1252')
        print(safe_content)


def _bot_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json"
    }


def _is_bot_configured() -> bool:
    """Returns True only when real (non-placeholder) bot credentials exist."""
    return (
        DISCORD_BOT_TOKEN != "placeholder-bot-token"
        and DISCORD_GUILD_ID != "placeholder-guild-id"
        and LOG_CATEGORY_ID != "placeholder-category-id"
    )


def get_or_create_monthly_log_channel() -> Optional[str]:
    """
    Looks for a channel named logs-YYYY-MM under LOG_CATEGORY_ID.
    Creates it if it doesn't exist yet.
    Returns the channel ID, or None on failure.

    Channel naming example: logs-2026-06
    A new channel is auto-created at the first run of each month.
    """
    if not _is_bot_configured():
        logger.info("[Discord] Bot not configured — skipping channel lookup.")
        return None

    channel_name = datetime.utcnow().strftime("logs-%Y-%m")

    # 1. List all channels in the guild
    try:
        resp = requests.get(
            f"{DISCORD_API_BASE}/guilds/{DISCORD_GUILD_ID}/channels",
            headers=_bot_headers(),
            timeout=10
        )
        resp.raise_for_status()
        channels = resp.json()
    except Exception as e:
        logger.error(f"[Discord] Failed to list channels: {e}")
        return None

    # 2. Check if the monthly channel already exists
    for ch in channels:
        if ch.get("name") == channel_name:
            logger.info(f"[Discord] Found existing log channel: #{channel_name} (id={ch['id']})")
            return ch["id"]

    # 3. Create it under the log category
    logger.info(f"[Discord] Creating new log channel: #{channel_name}")
    payload = {
        "name": channel_name,
        "type": 0,                        # 0 = text channel
        "parent_id": LOG_CATEGORY_ID,
        "topic": f"Hourly crypto scanner logs for {datetime.utcnow().strftime('%B %Y')}",
        "position": 0
    }
    try:
        resp = requests.post(
            f"{DISCORD_API_BASE}/guilds/{DISCORD_GUILD_ID}/channels",
            headers=_bot_headers(),
            json=payload,
            timeout=10
        )
        resp.raise_for_status()
        new_channel = resp.json()
        logger.info(f"[Discord] Created #{channel_name} (id={new_channel['id']})")
        return new_channel["id"]
    except Exception as e:
        logger.error(f"[Discord] Failed to create channel: {e}")
        return None


def post_to_channel(channel_id: str, content: str) -> bool:
    """Posts a plain message to a Discord channel via bot token."""
    if not _is_bot_configured():
        logger.info(f"[Discord] Bot not configured — log message would be:\n{content}")
        return False

    try:
        resp = requests.post(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            headers=_bot_headers(),
            json={"content": content},
            timeout=10
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"[Discord] Failed to post to channel {channel_id}: {e}")
        return False


def _fg_label(fear_greed: int) -> str:
    """Returns a human-readable Fear & Greed label."""
    if fear_greed <= 20:   return "Extreme Fear"
    elif fear_greed <= 40: return "Fear"
    elif fear_greed <= 60: return "Neutral"
    elif fear_greed <= 80: return "Greed"
    else:                  return "Extreme Greed"


def _build_coin_log_message(coin: str, result: Dict[str, Any], now_str: str) -> str:
    """
    Builds a detailed per-coin log message showing every indicator's
    pass/fail status, the exact metric value that caused it, and
    mandatory filter results.

    Example output:
    ════════════════════════════════════════════
    📊 BITCOIN  |  $67,432.10  |  2026-06-01 09:00 UTC
    ════════════════════════════════════════════
    [BUY CHECKLIST] Score: 6/10  |  Mandatory Filters: [PASS]
    ─────────────────────────────────────────────
      [x] MET  BTC Macro Filter (>200 EMA)
      [x] MET  Daily EMA Alignment (Full Alignment)
      [ ] --   4H RSI (40-65 or <30)           RSI: 81.2
      [x] MET  4H MACD Crossover
      [x] MET  4H Volume / OBV Spike           2.77x / OBV: True
      [ ] --   Bollinger Bands (Lower Band)
      [ ] --   Stochastic RSI Cross Up
      [ ] --   VWAP Support
      [x] MET  Fear & Greed (20-40)            Index: 38
      [x] MET  ADX Trend Strength (>=25)       ADX: 51.4
    ─────────────────────────────────────────────
      [Mandatory #1] BTC Macro Trend:          [PASS]
      [Mandatory #8] Risk/Reward (>= 2.0):     [PASS]  2.41x
    ─────────────────────────────────────────────
    ⬜ NO SIGNAL  (need 7/10 + mandatory pass)
    """
    if result.get("status") != "success":
        return (
            f"════════════════════════════════════════════\n"
            f"❗ **{coin.upper()}** — Scan FAILED\n"
            f"Error: {result.get('error', 'Unknown error')}\n"
            f"Timestamp: {now_str}\n"
            f"════════════════════════════════════════════"
        )

    m          = result.get("metrics", {})
    buy_score  = result.get("buy_score", 0)
    sell_score = result.get("sell_score", 0)
    price      = m.get("current_price", 0)
    fear_greed = m.get("fear_greed", 0)
    rsi        = m.get("rsi_4h", 0)
    adx        = m.get("adx", 0)
    vol_ratio  = m.get("volume_ratio", 0)
    obv_bonus  = m.get("obv_bonus", False)
    rr_ratio   = m.get("risk_reward_ratio", 0)
    stop_loss  = m.get("stop_loss", 0)
    target     = m.get("target_price", 0)
    mandatory  = result.get("buy_mandatory_passed", False)
    btc_pass   = m.get("btc_above_200ema", False)
    rr_pass    = m.get("risk_reward_pass", False)

    def row(met: bool, label: str, detail: str = "") -> str:
        """Formats a single checklist row."""
        check  = "[x] MET " if met else "[ ] --  "
        detail = f"  {detail}" if detail else ""
        return f"  {check} {label}{detail}"

    # ── BUY checklist rows ──────────────────────────────────────
    buy_rows = [
        row(m.get("btc_above_200ema",  False), "BTC Macro Filter (>200 EMA)"),
        row(m.get("buy_ema_aligned",   False), f"Daily EMA Alignment",
            f"({m.get('ema_alignment_type', 'No Alignment')})"),
        row(m.get("buy_rsi",           False), "4H RSI (40-65 or <30)",
            f"RSI: {rsi:.1f}"),
        row(m.get("buy_macd",          False), "4H MACD Crossover"),
        row(m.get("buy_volume_obv",    False), "4H Volume / OBV Spike",
            f"{vol_ratio:.2f}x / OBV Bonus: {obv_bonus}"),
        row(m.get("buy_bb",            False), "Bollinger Bands (Near Lower Band)"),
        row(m.get("buy_stoch_rsi",     False), "Stochastic RSI (Cross Up from <20)"),
        row(m.get("buy_vwap",          False), "VWAP Support (Price above VWAP)"),
        row(m.get("buy_sentiment",     False), "Fear & Greed (20-40)",
            f"Index: {fear_greed} — {_fg_label(fear_greed)}"),
        row(m.get("buy_adx",           False), "ADX Trend Strength (>=25)",
            f"ADX: {adx:.1f}"),
    ]

    # ── SELL checklist rows ─────────────────────────────────────
    sell_rows = [
        row(m.get("sell_ema",          False), "EMA Downtrend (Price < 200 EMA)"),
        row(m.get("sell_rsi",          False), "RSI Overbought (>=75)",
            f"RSI: {rsi:.1f}"),
        row(m.get("sell_macd",         False), "MACD Bearish Crossover"),
        row(m.get("sell_volume_obv",   False), "OBV Divergence (Price up, OBV down)"),
        row(m.get("sell_bb",           False), "Bollinger Overextended (Upper Band)"),
        row(m.get("sell_stoch_rsi",    False), "Stochastic RSI (Cross Down from >80)"),
        row(m.get("sell_vwap",         False), "VWAP Rejection (Price below VWAP)"),
        row(m.get("sell_sentiment",    False), "Sentiment Peak (Fear & Greed >= 80)",
            f"Index: {fear_greed} — {_fg_label(fear_greed)}"),
        row(m.get("sell_adx",          False), "ADX Choppy (< 15)",
            f"ADX: {adx:.1f}"),
        row(m.get("sell_onchain",      False), "Whale Dumping (Large Exchange Inflows)"),
    ]

    # ── Mandatory filter rows ───────────────────────────────────
    btc_status = "[PASS]" if btc_pass else "[FAIL]"
    rr_status  = "[PASS]" if rr_pass  else "[FAIL]"
    mandatory_rows = [
        f"  [Mandatory #1] BTC Macro Trend:       {btc_status}",
        f"  [Mandatory #8] Risk/Reward (>= 2.0):  {rr_status}  {rr_ratio:.2f}x"
        + (f"  (Entry: ${price:,.2f}  SL: ${stop_loss:,.2f}  TP: ${target:,.2f})" if not rr_pass else ""),
    ]

    # ── Signal verdict ──────────────────────────────────────────
    if result.get("buy_alert_triggered"):
        verdict = "🟢 **BUY ALERT TRIGGERED** — Signal sent to #buy-sell-alerts"
    elif result.get("warning_alert_triggered"):
        verdict = "🟡 **WARNING ALERT TRIGGERED** — Signal sent to #buy-sell-alerts"
    elif result.get("sell_alert_triggered"):
        verdict = "🔴 **SELL ALERT TRIGGERED** — Signal sent to #buy-sell-alerts"
    else:
        if buy_score >= sell_score:
            verdict = f"⬜ NO BUY SIGNAL  (score {buy_score}/10, need 7 + mandatory pass)"
        else:
            verdict = f"⬜ NO SELL SIGNAL  (score {sell_score}/10, need 7)"

    div1 = "═" * 44
    div2 = "─" * 44

    return (
        f"```\n"
        f"{div1}\n"
        f"📊 {coin.upper():<12} | ${price:>12,.2f} | {now_str}\n"
        f"{div1}\n"
        f"[BUY CHECKLIST]  Score: {buy_score}/10  |  "
        f"Mandatory: {'[PASS]' if mandatory else '[FAIL]'}\n"
        f"{div2}\n"
        + "\n".join(buy_rows) + "\n"
        f"{div2}\n"
        + "\n".join(mandatory_rows) + "\n"
        f"{div2}\n"
        f"[SELL CHECKLIST] Score: {sell_score}/10\n"
        f"{div2}\n"
        + "\n".join(sell_rows) + "\n"
        f"{div2}\n"
        f"{verdict}\n"
        f"```"
    )


def send_hourly_summary(all_results: Dict[str, Any]) -> bool:
    """
    Posts one detailed message per coin to the monthly log channel,
    followed by a compact overall summary message.
    Each coin message shows every indicator's pass/fail with exact values.
    """
    channel_id = get_or_create_monthly_log_channel()
    if not channel_id:
        return False

    now_str    = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    fear_greed = None
    any_failed = False

    # ── Post one detailed message per coin ──────────────────────
    for coin, result in all_results.items():
        msg = _build_coin_log_message(coin, result, now_str)
        post_to_channel(channel_id, msg)
        # Small delay to avoid Discord rate limits (5 messages/5s per channel)
        time.sleep(1.0)

        if result.get("status") == "success":
            fear_greed = result.get("metrics", {}).get("fear_greed", fear_greed)
        else:
            any_failed = True

    # ── Post compact overall summary as the final message ───────
    summary_lines = []
    for coin, result in all_results.items():
        if result.get("status") != "success":
            summary_lines.append(f"  {coin.upper():<12} ❗ FAILED")
            continue
        buy_score  = result.get("buy_score", 0)
        sell_score = result.get("sell_score", 0)
        price      = result.get("metrics", {}).get("current_price", 0)
        if result.get("buy_alert_triggered"):
            sig = "🟢 BUY"
        elif result.get("warning_alert_triggered"):
            sig = "🟡 WARN"
        elif result.get("sell_alert_triggered"):
            sig = "🔴 SELL"
        else:
            sig = "⬜ --"
        summary_lines.append(
            f"  {coin.upper():<12} ${price:>12,.2f}   B:{buy_score}/10  S:{sell_score}/10  {sig}"
        )

    fg_line = ""
    if fear_greed is not None:
        fg_line = f"\n  Fear & Greed Index: {fear_greed} ({_fg_label(fear_greed)})"

    fail_note = "\n  ⚠️  One or more coins failed to scan — check GitHub Actions logs." if any_failed else ""

    summary_msg = (
        f"```\n"
        f"{'─' * 44}\n"
        f"  SCAN COMPLETE — {now_str}\n"
        f"{'─' * 44}\n"
        + "\n".join(summary_lines)
        + fg_line
        + fail_note + "\n"
        f"{'─' * 44}\n"
        f"```"
    )
    post_to_channel(channel_id, summary_msg)

    logger.info(f"[Discord] Hourly summary posted to #{datetime.utcnow().strftime('logs-%Y-%m')}")
    return True


# =======================================================================
# EXISTING FUNCTIONS (unchanged)
# =======================================================================

def fetch_fear_greed_index() -> int:
    try:
        response = requests.get(FEAR_GREED_API_URL, timeout=8)
        response.raise_for_status()
        data  = response.json()
        value = int(data["data"][0]["value"])
        logger.info(f"Successfully fetched live Fear & Greed Index: {value}")
        return value
    except Exception as e:
        logger.warning(f"Failed to fetch Fear & Greed index ({e}). Falling back to neutral 50.")
        return 50


def generate_synthetic_data(coin_id: str) -> Tuple[pd.Series, pd.Series, pd.DataFrame]:
    logger.info(f"[MOCK MODE] Generating synthetic market data for {coin_id.upper()}...")

    btc_dates      = pd.date_range(end=datetime.utcnow(), periods=250, freq="D")
    btc_base       = np.linspace(61000, 74000, 250)
    np.random.seed(42)
    btc_noise      = np.random.normal(0, 200, 250)
    btc_prices     = pd.Series(btc_base + btc_noise, index=btc_dates)

    coin_daily_dates  = pd.date_range(end=datetime.utcnow(), periods=250, freq="D")
    coin_daily_base   = np.linspace(100, 155, 250)
    coin_daily_noise  = np.random.normal(0, 0.8, 250)
    coin_daily_prices = pd.Series(coin_daily_base + coin_daily_noise, index=coin_daily_dates)

    coin_hourly_dates = pd.date_range(end=datetime.utcnow(), periods=720, freq="h")
    coin_hourly_base  = np.linspace(165, 149, 720)
    cycles            = np.sin(np.linspace(0, 5 * np.pi, 720)) * 6.5
    coin_hourly_noise = np.random.normal(0, 0.3, 720)
    hourly_prices     = coin_hourly_base + cycles + coin_hourly_noise

    base_volume       = np.random.exponential(scale=6000, size=720)
    base_volume[-8:]  = base_volume[-8:] * 4.2

    df_hourly         = pd.DataFrame({"price": hourly_prices, "volume": base_volume}, index=coin_hourly_dates)
    df_hourly["high"] = df_hourly["price"] * 1.01
    df_hourly["low"]  = df_hourly["price"] * 0.99

    df_4h = df_hourly.resample("4h").agg({"price": "last", "high": "max", "low": "min", "volume": "sum"}).dropna()
    return btc_prices, coin_daily_prices, df_4h


def fetch_coingecko_market_chart(
    coin_id: str,
    days: int,
    vs_currency: str = DEFAULT_VS_CURRENCY,
    interval: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    # Use demo-api if an API key is provided to unlock higher rate limits
    base_url = "https://demo-api.coingecko.com/api/v3" if COINGECKO_API_KEY else COINGECKO_BASE_URL
    endpoint = f"{base_url}/coins/{coin_id}/market_chart"
    params      = {"vs_currency": vs_currency, "days": str(days)}
    if interval:
        params["interval"] = interval

    headers     = {"accept": "application/json", "User-Agent": "UnifiedCryptoPipeline/3.1"}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY
    max_retries = 3
    retry_delay = 6.0

    for attempt in range(max_retries):
        time.sleep(2.0)
        try:
            response = requests.get(endpoint, params=params, headers=headers, timeout=15)
            if response.status_code == 429:
                logger.warning(f"CoinGecko Rate Limit (429) for {coin_id}. Attempt {attempt+1}/{max_retries}. Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                retry_delay *= 2.0
                continue
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"HTTP Request failed on attempt {attempt+1}: {e}")
            time.sleep(retry_delay)
            retry_delay *= 1.5

    logger.error(f"Failed to fetch market chart for {coin_id} after {max_retries} attempts.")
    return None


def get_daily_prices(coin_id: str, days: int = 250) -> Optional[pd.Series]:
    data = fetch_coingecko_market_chart(coin_id, days=days, interval="daily")
    if not data or "prices" not in data:
        return None
    df             = pd.DataFrame(data["prices"], columns=["timestamp", "price"])
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("datetime", inplace=True)
    return df["price"]


def get_4h_dataframe(coin_id: str, days: int = 30) -> Optional[pd.DataFrame]:
    data = fetch_coingecko_market_chart(coin_id, days=days)
    if not data or "prices" not in data or "total_volumes" not in data:
        return None

    df_prices  = pd.DataFrame(data["prices"],        columns=["timestamp", "price"])
    df_volumes = pd.DataFrame(data["total_volumes"], columns=["timestamp", "volume"])
    df         = pd.merge(df_prices, df_volumes, on="timestamp")
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("datetime", inplace=True)
    df["high"] = df["price"] * 1.005
    df["low"]  = df["price"] * 0.995

    df_4h = df.resample("4h").agg({"price": "last", "high": "max", "low": "min", "volume": "sum"}).dropna()
    return df_4h


def calculate_rsi(prices: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta         = prices.diff()
    gain          = delta.clip(lower=0)
    loss          = -delta.clip(upper=0)
    smoothed_gain = pd.Series(index=prices.index, dtype=float)
    smoothed_loss = pd.Series(index=prices.index, dtype=float)

    if len(prices) < period + 1:
        return pd.Series(np.nan, index=prices.index)

    smoothed_gain.iloc[period] = gain.iloc[1: period + 1].mean()
    smoothed_loss.iloc[period] = loss.iloc[1: period + 1].mean()

    for i in range(period + 1, len(prices)):
        smoothed_gain.iloc[i] = (smoothed_gain.iloc[i-1] * (period-1) + gain.iloc[i]) / period
        smoothed_loss.iloc[i] = (smoothed_loss.iloc[i-1] * (period-1) + loss.iloc[i]) / period

    rs  = smoothed_gain / smoothed_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_macd(prices: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
    ema12       = prices.ewm(span=12, adjust=False).mean()
    ema26       = prices.ewm(span=26, adjust=False).mean()
    macd_line   = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_obv(prices: pd.Series, volumes: pd.Series) -> pd.Series:
    obv = [0.0]
    for i in range(1, len(prices)):
        if   prices.iloc[i] > prices.iloc[i-1]: obv.append(obv[-1] + volumes.iloc[i])
        elif prices.iloc[i] < prices.iloc[i-1]: obv.append(obv[-1] - volumes.iloc[i])
        else:                                    obv.append(obv[-1])
    return pd.Series(obv, index=prices.index)


def calculate_bollinger_bands(df: pd.DataFrame, window: int = 20) -> Tuple[pd.Series, pd.Series, pd.Series]:
    sma   = df["price"].rolling(window=window).mean()
    std   = df["price"].rolling(window=window).std()
    upper = sma + (2 * std)
    lower = sma - (2 * std)
    return upper, sma, lower


def calculate_stochastic_rsi(rsi_series: pd.Series, period: int = 14) -> Tuple[pd.Series, pd.Series]:
    min_rsi   = rsi_series.rolling(period).min()
    max_rsi   = rsi_series.rolling(period).max()
    stoch_rsi = (rsi_series - min_rsi) / (max_rsi - min_rsi).replace(0, np.nan)
    stoch_k   = stoch_rsi.rolling(3).mean() * 100
    stoch_d   = stoch_k.rolling(3).mean()
    return stoch_k, stoch_d


def calculate_vwap(df: pd.DataFrame, window: int = 20) -> pd.Series:
    pv          = df["price"] * df["volume"]
    rolling_pv  = pv.rolling(window=window).sum()
    rolling_vol = df["volume"].rolling(window=window).sum()
    return rolling_pv / rolling_vol.replace(0, np.nan)


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["price"].shift(1)
    tr1        = df["high"] - df["low"]
    tr2        = (df["high"] - prev_close).abs()
    tr3        = (df["low"]  - prev_close).abs()
    tr         = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    up_move    = df["high"] - df["high"].shift(1)
    down_move  = df["low"].shift(1) - df["low"]

    plus_dm    = np.where((up_move > down_move)   & (up_move   > 0), up_move,   0)
    minus_dm   = np.where((down_move > up_move)   & (down_move > 0), down_move, 0)

    atr        = tr.rolling(period).mean()
    plus_di    = 100 * pd.Series(plus_dm,  index=df.index).rolling(period).mean() / atr.replace(0, np.nan)
    minus_di   = 100 * pd.Series(minus_dm, index=df.index).rolling(period).mean() / atr.replace(0, np.nan)

    dx         = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx        = dx.rolling(period).mean()
    return adx


def send_signal_alert(
    coin_id: str,
    direction: str,
    score: int,
    metrics: Dict[str, Any],
    webhook_url: str = ALERT_WEBHOOK_URL
) -> bool:
    if direction == "BUY":
        color_bar = "🟢"
        title     = f"{color_bar} **COMPOSITE SYSTEM SCANNER: BUY SIGNAL** {color_bar}"
    elif direction == "WARNING":
        color_bar = "🟡"
        title     = f"{color_bar} **COMPOSITE SYSTEM SCANNER: WARNING ALERT** {color_bar}"
    else:
        color_bar = "🔴"
        title     = f"{color_bar} **COMPOSITE SYSTEM SCANNER: SELL SIGNAL** {color_bar}"

    if direction in ("BUY", "WARNING"):
        checklist = [
            f"{'✅' if metrics['btc_above_200ema'] else '❌'} 1. BTC Macro Filter (Price > 200 EMA)",
            f"{'✅' if metrics['buy_ema_aligned']  else '❌'} 2. Daily EMA Alignment ({metrics['ema_alignment_type']})",
            f"{'✅' if metrics['buy_rsi']          else '❌'} 3. 4H RSI ({metrics['rsi_4h']:.1f})",
            f"{'✅' if metrics['buy_macd']         else '❌'} 4. 4H MACD Crossover",
            f"{'✅' if metrics['buy_volume_obv']   else '❌'} 5. Volume Spike ({metrics['volume_ratio']:.2f}x / OBV Bonus: {metrics['obv_bonus']})",
            f"{'✅' if metrics['buy_bb']           else '❌'} 6. Bollinger Bands (Price near lower band)",
            f"{'✅' if metrics['buy_stoch_rsi']    else '❌'} 7. Stochastic RSI (Cross up from below 20)",
            f"{'✅' if metrics['buy_vwap']         else '❌'} 8. VWAP Support (Price above VWAP)",
            f"{'✅' if metrics['buy_sentiment']    else '❌'} 9. Sentiment (Fear & Greed: {metrics['fear_greed']})",
            f"{'✅' if metrics['buy_adx']          else '❌'} 10. ADX Trend Strength ({metrics['adx']:.1f})",
        ]
        mandatory_status = (
            f"⚠️ **Mandatory Filters:**\n"
            f"  * [Rule #1] BTC Macro Trend: {'✅' if metrics['btc_above_200ema'] else '❌'}\n"
            f"  * [Rule #8] Risk/Reward (>= 2.0): {'✅' if metrics['risk_reward_pass'] else '❌'}\n\n"
        )
    else:
        checklist = [
            f"{'🚨' if metrics['sell_ema']        else '⚪'} 1. EMA Downtrend (Price < 200 EMA)",
            f"{'🚨' if metrics['sell_rsi']        else '⚪'} 2. RSI Overbought (RSI: {metrics['rsi_4h']:.1f})",
            f"{'🚨' if metrics['sell_macd']       else '⚪'} 3. MACD Bearish Crossover",
            f"{'🚨' if metrics['sell_volume_obv'] else '⚪'} 4. OBV Divergence",
            f"{'🚨' if metrics['sell_bb']         else '⚪'} 5. Bollinger Overextended",
            f"{'🚨' if metrics['sell_stoch_rsi']  else '⚪'} 6. Stochastic RSI (Cross down from above 80)",
            f"{'🚨' if metrics['sell_vwap']       else '⚪'} 7. VWAP Rejection",
            f"{'🚨' if metrics['sell_sentiment']  else '⚪'} 8. Sentiment Peak (Fear & Greed: {metrics['fear_greed']})",
            f"{'🚨' if metrics['sell_adx']        else '⚪'} 9. ADX Choppiness ({metrics['adx']:.1f} < 15)",
            f"{'🚨' if metrics['sell_onchain']    else '⚪'} 10. Whale Dumping (Large exchange inflows)",
        ]
        mandatory_status = ""

    checklist_str = "\n".join(checklist)
    payload = {
        "username":   "System Scanner Bot",
        "avatar_url": "https://images.unsplash.com/photo-1621761191319-c6fb62004040?w=150",
        "content": (
            f"{title}\n"
            f"**Asset:** ${coin_id.upper()}\n"
            f"**Current Price:** ${metrics['current_price']:,.2f} USD\n"
            f"**Composite Score:** **{score}/10**\n\n"
            f"{mandatory_status}"
            f"📋 **System Checklist:**\n"
            f"```\n{checklist_str}\n```\n"
            f"📊 **Position Specs:**\n"
            f"  * *Entry (Current):* ${metrics['current_price']:,.2f}\n"
            f"  * *Stop Loss:* ${metrics['stop_loss']:,.2f}\n"
            f"  * *Target (Resistance):* ${metrics['target_price']:,.2f}\n"
            f"  * *Risk/Reward:* {metrics['risk_reward_ratio']:.2f}x\n"
            f"**Status:** Setup validated. Execute manual validation before entry."
        )
    }

    if "placeholder-id" in webhook_url:
        logger.warning(f"[MOCK NOTIFICATION] Webhook placeholder. Skipping POST.\n{json.dumps(payload, indent=2)}")
        return True

    try:
        resp = requests.post(webhook_url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        resp.raise_for_status()
        logger.info(f"{direction} alert delivered to Discord.")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to deliver {direction} webhook: {e}")
        return False


# =======================================================================
# CORE EVALUATION
# =======================================================================

def evaluate_trading_system(
    coin_id: str,
    target_override: Optional[float] = None,
    stop_override: Optional[float] = None,
    resistance_override: Optional[float] = None,
    use_mock_data: bool = False,
    force_sell: bool = False,
    btc_daily_cache: Optional[pd.Series] = None
) -> Dict[str, Any]:

    report = {
        "status": "failed", "coin_id": coin_id,
        "buy_score": 0, "sell_score": 0,
        "buy_alert_triggered": False, "sell_alert_triggered": False,
        "metrics": {}
    }

    logger.info(f"=== Running 10-Indicator Scan on {coin_id.upper()} ===")
    fear_greed = fetch_fear_greed_index()

    if use_mock_data:
        btc_daily, coin_daily, df_4h = generate_synthetic_data(coin_id)
        latest_btc_price = btc_daily.iloc[-1]
        latest_btc_ema   = btc_daily.ewm(span=200, adjust=False).mean().iloc[-1]
        latest_price     = coin_daily.iloc[-1]
        latest_ema20     = coin_daily.ewm(span=20,  adjust=False).mean().iloc[-1]
        latest_ema50     = coin_daily.ewm(span=50,  adjust=False).mean().iloc[-1]
        latest_ema200    = coin_daily.ewm(span=200, adjust=False).mean().iloc[-1]
    else:
        if btc_daily_cache is not None:
            logger.info("Using cached BTC daily prices...")
            btc_daily = btc_daily_cache
        else:
            logger.info("Fetching daily BTC prices...")
            btc_daily = get_daily_prices("bitcoin", days=250)
            
        if btc_daily is None or len(btc_daily) < 200:
            report["error"] = "Insufficient BTC daily data"; return report

        latest_btc_price = btc_daily.iloc[-1]
        latest_btc_ema   = btc_daily.ewm(span=200, adjust=False).mean().iloc[-1]

        # Only sleep if we actually fetched a fresh BTC chart to pace requests
        if btc_daily_cache is None:
            logger.info("Pacing API... sleeping 8s...")
            time.sleep(8.0)

        logger.info(f"Fetching daily {coin_id.upper()} prices...")
        coin_daily = get_daily_prices(coin_id, days=250)
        if coin_daily is None or len(coin_daily) < 200:
            report["error"] = f"Insufficient daily data for {coin_id}"; return report

        latest_price  = coin_daily.iloc[-1]
        latest_ema20  = coin_daily.ewm(span=20,  adjust=False).mean().iloc[-1]
        latest_ema50  = coin_daily.ewm(span=50,  adjust=False).mean().iloc[-1]
        latest_ema200 = coin_daily.ewm(span=200, adjust=False).mean().iloc[-1]

        logger.info("Pacing API... sleeping 8s...")
        time.sleep(8.0)

        logger.info(f"Fetching 4H data for {coin_id.upper()}...")
        df_4h = get_4h_dataframe(coin_id, days=30)
        if df_4h is None or len(df_4h) < 30:
            report["error"] = f"Insufficient 4H data for {coin_id}"; return report

    # Calculate indicators
    df_4h["rsi"]       = calculate_rsi(df_4h["price"], period=RSI_PERIOD)
    macd_l, sig_l, hist_l = calculate_macd(df_4h["price"])
    df_4h["macd"]      = macd_l
    df_4h["signal"]    = sig_l
    df_4h["histogram"] = hist_l
    df_4h["obv"]       = calculate_obv(df_4h["price"], df_4h["volume"])
    upper_b, _, lower_b = calculate_bollinger_bands(df_4h)
    df_4h["bb_upper"]  = upper_b
    df_4h["bb_lower"]  = lower_b
    sk, sd             = calculate_stochastic_rsi(df_4h["rsi"])
    df_4h["stoch_k"]   = sk
    df_4h["stoch_d"]   = sd
    df_4h["vwap"]      = calculate_vwap(df_4h)
    df_4h["adx"]       = calculate_adx(df_4h)

    latest_4h     = df_4h.iloc[-1]
    prev_4h       = df_4h.iloc[-2]
    latest_rsi_4h = latest_4h["rsi"]
    latest_macd   = latest_4h["macd"]
    latest_signal = latest_4h["signal"]
    latest_hist   = latest_4h["histogram"]
    prev_hist     = prev_4h["histogram"]
    latest_stoch_k= latest_4h["stoch_k"]
    latest_stoch_d= latest_4h["stoch_d"]
    prev_stoch_k  = prev_4h["stoch_k"]
    latest_vwap   = latest_4h["vwap"]
    latest_adx    = latest_4h["adx"]
    prev_adx      = prev_4h["adx"]

    whale_accumulating, exchange_outflows_rising, large_inflows_detected = \
        MOCK_ONCHAIN_DATABASE.get(coin_id.lower(), (True, False, False))

    if force_sell:
        logger.info("[FORCED PROJECTION] Injecting bearish data...")
        fear_greed = 88; latest_rsi_4h = 78.5; latest_macd = -1.2; latest_signal = 0.5
        latest_hist = -1.7; prev_hist = -0.8; latest_stoch_k = 75.0; prev_stoch_k = 85.0
        latest_stoch_d = 80.0; latest_vwap = 160.0; latest_price = 145.0
        latest_ema200 = 150.0; latest_ema50 = 148.0; latest_ema20 = 146.0
        latest_btc_price = 55000.0; latest_btc_ema = 60000.0; latest_adx = 10.0
        whale_accumulating = False; exchange_outflows_rising = False; large_inflows_detected = True
        df_4h = df_4h.copy()
        df_4h.iloc[-4, df_4h.columns.get_loc("price")] = 140.0
        df_4h.iloc[-4, df_4h.columns.get_loc("obv")]   = 60000
        latest_4h = df_4h.iloc[-1].copy()
        latest_4h["bb_upper"] = 145.2; latest_4h["bb_lower"] = 135.0
        latest_4h["price"] = 145.0;   latest_4h["volume"] = 1000; latest_4h["obv"] = 50000
        prev_4h = df_4h.iloc[-2].copy(); prev_4h["price"] = 147.0

    # --- Scoring ---
    buy_score = 0; sell_score = 0
    metrics: Dict[str, Any] = {
        "current_price": float(latest_price), "fear_greed": int(fear_greed),
        "rsi_4h": float(latest_rsi_4h),       "adx": float(latest_adx),
        "whale_accumulating": whale_accumulating, "large_inflows_detected": large_inflows_detected,
    }

    # 1. BTC Macro
    btc_above_200ema = bool(latest_btc_price > latest_btc_ema)
    metrics["btc_above_200ema"] = btc_above_200ema
    if btc_above_200ema: buy_score += 1

    # 2. EMA Alignment
    buy_ema_aligned = False; ema_alignment_type = "No Alignment"
    if   latest_price > latest_ema20 > latest_ema50 > latest_ema200: buy_ema_aligned = True; ema_alignment_type = "Full Alignment (Bullish)"
    elif latest_price > latest_ema50 > latest_ema200:                 buy_ema_aligned = True; ema_alignment_type = "Partial Pass (Bullish)"
    sell_ema = bool(latest_price < latest_ema200)
    metrics["buy_ema_aligned"] = buy_ema_aligned; metrics["ema_alignment_type"] = ema_alignment_type; metrics["sell_ema"] = sell_ema
    if buy_ema_aligned: buy_score  += 1
    if sell_ema:        sell_score += 1

    # 3. RSI
    buy_rsi  = bool((40.0 <= latest_rsi_4h <= 65.0) or (latest_rsi_4h < 30.0))
    sell_rsi = bool(latest_rsi_4h >= 75.0 or (latest_rsi_4h < 25.0 and latest_price < latest_ema200))
    metrics["buy_rsi"] = buy_rsi; metrics["sell_rsi"] = sell_rsi
    if buy_rsi:  buy_score  += 1
    if sell_rsi: sell_score += 1

    # 4. MACD
    buy_macd  = bool((latest_macd > latest_signal) and (latest_hist > 0) and (latest_hist > prev_hist))
    sell_macd = bool(latest_macd < latest_signal)
    metrics["buy_macd"] = buy_macd; metrics["sell_macd"] = sell_macd
    if buy_macd:  buy_score  += 1
    if sell_macd: sell_score += 1

    # 5. Volume + OBV
    avg_vol_20   = df_4h["volume"].rolling(window=20).mean().iloc[-1]
    volume_ratio = latest_4h["volume"] / avg_vol_20 if avg_vol_20 > 0 else 0
    volume_spike = bool(volume_ratio >= 1.5)
    obv_bonus    = bool(len(df_4h) >= 4 and latest_4h["obv"] > df_4h["obv"].iloc[-4])
    buy_volume_obv = volume_spike or obv_bonus
    obv_divergence = bool(len(df_4h) >= 4 and float(latest_4h["price"]) > float(df_4h["price"].iloc[-4]) and float(latest_4h["obv"]) < float(df_4h["obv"].iloc[-4]))
    metrics["buy_volume_obv"] = buy_volume_obv; metrics["volume_ratio"] = float(volume_ratio)
    metrics["obv_bonus"] = obv_bonus;           metrics["sell_volume_obv"] = obv_divergence
    if buy_volume_obv:  buy_score  += 1
    if obv_divergence:  sell_score += 1

    # 6. Bollinger Bands
    buy_bb  = bool(latest_price <= latest_4h["bb_lower"] * 1.015)
    sell_bb = bool(latest_price >= latest_4h["bb_upper"] * 0.985)
    metrics["buy_bb"] = buy_bb; metrics["sell_bb"] = sell_bb
    if buy_bb:  buy_score  += 1
    if sell_bb: sell_score += 1

    # 7. Stochastic RSI
    buy_stoch_rsi  = bool(prev_stoch_k < 20.0 and latest_stoch_k >= 20.0 and latest_stoch_k > latest_stoch_d)
    sell_stoch_rsi = bool(latest_stoch_k < latest_stoch_d and latest_stoch_k > 70.0)
    metrics["buy_stoch_rsi"] = buy_stoch_rsi; metrics["sell_stoch_rsi"] = sell_stoch_rsi
    if buy_stoch_rsi:  buy_score  += 1
    if sell_stoch_rsi: sell_score += 1

    # 8. VWAP
    buy_vwap  = bool(latest_price > latest_vwap and latest_price <= latest_vwap * 1.05)
    sell_vwap = bool(latest_price < latest_vwap and float(prev_4h["price"]) < latest_vwap)
    metrics["buy_vwap"] = buy_vwap; metrics["sell_vwap"] = sell_vwap
    if buy_vwap:  buy_score  += 1
    if sell_vwap: sell_score += 1

    # 9. Fear & Greed
    buy_sentiment  = bool(20 <= fear_greed <= 40)
    sell_sentiment = bool(80 <= fear_greed <= 100)
    metrics["buy_sentiment"] = buy_sentiment; metrics["sell_sentiment"] = sell_sentiment
    if buy_sentiment:  buy_score  += 1
    if sell_sentiment: sell_score += 1

    # 10. ADX
    buy_adx  = bool(latest_adx >= 25.0 and latest_adx > prev_adx)
    sell_adx = bool(latest_adx < 15.0)
    metrics["buy_adx"] = buy_adx; metrics["sell_adx"] = sell_adx
    if buy_adx:  buy_score  += 1
    if sell_adx: sell_score += 1

    # On-chain
    buy_onchain  = bool(whale_accumulating or exchange_outflows_rising)
    sell_onchain = bool(large_inflows_detected)
    metrics["buy_onchain"] = buy_onchain; metrics["sell_onchain"] = sell_onchain
    if sell_onchain: sell_score += 1

    # Position sizing
    stop_loss = stop_override
    if stop_loss is None:
        stop_loss = df_4h["price"].iloc[-20:].min()
        if stop_loss >= latest_price:
            stop_loss = latest_price * 0.95
            logger.warning(f"[{coin_id.upper()}] Swing-low stop above entry. Falling back to 5% stop (${stop_loss:,.2f}).")

    resistance_level = resistance_override or MOCK_RESISTANCE_LEVELS.get(coin_id.lower(), df_4h["price"].max())
    target_price     = target_override
    if target_price is None:
        target_price = resistance_level
        if target_price <= latest_price: target_price = latest_price * 1.05

    risk  = latest_price - stop_loss
    reward = target_price - latest_price
    risk_reward_ratio = reward / risk if risk > 0 else 0
    risk_reward_pass  = bool(risk_reward_ratio >= 2.0)

    metrics["stop_loss"]           = float(stop_loss)
    metrics["target_price"]        = float(target_price)
    metrics["risk_reward_ratio"]   = float(risk_reward_ratio)
    metrics["risk_reward_pass"]    = risk_reward_pass
    resistance_dist_pct            = ((resistance_level - latest_price) / latest_price) * 100
    metrics["resistance_dist_pct"] = float(resistance_dist_pct)
    metrics["resistance_room_pass"]= bool(resistance_dist_pct >= 3.0)

    # Core indicators raw values for terminal/diagnostics visibility
    metrics["btc_price"]   = float(latest_btc_price)
    metrics["btc_ema_200"] = float(latest_btc_ema)
    metrics["macd"]        = float(latest_macd)
    metrics["macd_signal"] = float(latest_signal)
    metrics["macd_hist"]   = float(latest_hist)
    metrics["bb_upper"]    = float(latest_4h["bb_upper"])
    metrics["bb_lower"]    = float(latest_4h["bb_lower"])
    metrics["stoch_k"]     = float(latest_stoch_k) if not pd.isna(latest_stoch_k) else 0.0
    metrics["stoch_d"]     = float(latest_stoch_d) if not pd.isna(latest_stoch_d) else 0.0
    metrics["vwap"]        = float(latest_vwap)

    report["buy_score"]  = buy_score
    report["sell_score"] = sell_score
    report["metrics"]    = metrics
    buy_mandatory_passed = bool(btc_above_200ema and risk_reward_pass)
    report["buy_mandatory_passed"] = buy_mandatory_passed
    report["status"] = "success"

    report["buy_alert_triggered"] = False
    report["warning_alert_triggered"] = False
    report["sell_alert_triggered"] = False

    if buy_score >= 7:
        if buy_mandatory_passed:
            logger.warning(f"🟢 BUY SIGNAL on {coin_id.upper()}! Score: {buy_score}/10. Sending alert...")
            alert_sent = send_signal_alert(coin_id=coin_id, direction="BUY", score=buy_score, metrics=metrics)
            report["buy_alert_triggered"]   = True
            report["buy_notification_sent"] = alert_sent
        else:
            logger.warning(f"🟡 WARNING ALERT on {coin_id.upper()}! Score: {buy_score}/10. Sending alert...")
            alert_sent = send_signal_alert(coin_id=coin_id, direction="WARNING", score=buy_score, metrics=metrics)
            report["warning_alert_triggered"] = True
            report["buy_notification_sent"] = alert_sent

    elif sell_score >= 7:
        logger.warning(f"🔴 SELL SIGNAL on {coin_id.upper()}! Score: {sell_score}/10. Sending alert...")
        alert_sent = send_signal_alert(coin_id=coin_id, direction="SELL", score=sell_score, metrics=metrics)
        report["sell_alert_triggered"]   = True
        report["sell_notification_sent"] = alert_sent

    else:
        logger.info(f"[{coin_id.upper()}] Buy: {buy_score}/10 (mandatory: {buy_mandatory_passed}) | Sell: {sell_score}/10 — No alert.")

    return report


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("Executing Lambda handler...")
    output = evaluate_trading_system(
        coin_id=event.get("coin_id", DEFAULT_COIN),
        target_override=event.get("target_price"),
        stop_override=event.get("stop_loss"),
        resistance_override=event.get("resistance_level"),
        use_mock_data=event.get("mock", False)
    )
    return {"statusCode": 200 if output["status"] == "success" else 500, "body": json.dumps(output)}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Crypto Trading System Scanner — 10-Factor Buy/Sell Model")
    parser.add_argument("--coin",       type=str,   default=DEFAULT_COIN)
    parser.add_argument("--watchlist",  action="store_true", help="Scan all 5 coins + post hourly summary to Discord")
    parser.add_argument("--target",     type=float, default=None)
    parser.add_argument("--stop",       type=float, default=None)
    parser.add_argument("--resistance", type=float, default=None)
    parser.add_argument("--mock",       action="store_true")
    parser.add_argument("--force-sell", action="store_true")
    args = parser.parse_args()

    if args.watchlist:
        logger.info(f"=== WATCHLIST MODE: scanning {WATCHLIST} ===")
        all_results = {}

        # Pre-fetch BTC daily prices once to prevent rate limits and pacing delays
        btc_daily_cache = None
        if not args.mock:
            logger.info("Pre-fetching daily BTC prices for watchlist caching...")
            btc_daily_cache = get_daily_prices("bitcoin", days=250)
            if btc_daily_cache is not None:
                logger.info("Pacing API... sleeping 8s after BTC pre-fetch...")
                time.sleep(8.0)

        for coin in WATCHLIST:
            result = evaluate_trading_system(
                coin_id=coin,
                use_mock_data=args.mock,
                force_sell=args.force_sell,
                btc_daily_cache=btc_daily_cache
            )
            all_results[coin] = result
            now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            msg = _build_coin_log_message(coin, result, now_str)
            # Remove the markdown code block backticks for clean terminal output
            console_msg = msg.replace("```\n", "").replace("```", "")
            print("")
            print_safe_unicode(console_msg)

            if coin != WATCHLIST[-1]:
                logger.info("Pacing API... sleeping 10s...")
                time.sleep(10)

        # Post hourly summary to the monthly Discord log channel
        logger.info("Posting hourly summary to Discord log channel...")
        send_hourly_summary(all_results)

        print("\n\n=== WATCHLIST SCAN COMPLETE ===")
        print(json.dumps(all_results, indent=4))

    else:
        result = evaluate_trading_system(
            coin_id=args.coin, target_override=args.target,
            stop_override=args.stop, resistance_override=args.resistance,
            use_mock_data=args.mock, force_sell=args.force_sell
        )
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        msg = _build_coin_log_message(args.coin, result, now_str)
        console_msg = msg.replace("```\n", "").replace("```", "")
        print("")
        print_safe_unicode(console_msg)
            
        print("\n--- Pipeline Execution Report ---")
        print(json.dumps(result, indent=4))