import os
import json
import logging
import requests
import time
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

# Setup logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Constants & Configuration
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
FEAR_GREED_API_URL = "https://api.alternative.me/fng/"
DEFAULT_COIN = "solana"
DEFAULT_VS_CURRENCY = "usd"
RSI_PERIOD = 14

# Webhook configuration
WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "https://discord.com/api/webhooks/placeholder-id/placeholder-token")

# -----------------------------------------------------------------------
# TOP 5 WATCHLIST
# Run the scanner against all 5 coins automatically when using --watchlist
# -----------------------------------------------------------------------
WATCHLIST = ["bitcoin", "ethereum", "solana", "ripple", "binancecoin"]

# -----------------------------------------------------------------------
# On-Chain smart money flows & local resistance (mock / fallback config)
# Tuple: (whale_accumulating, exchange_outflows_rising, large_inflows_detected)
# -----------------------------------------------------------------------
MOCK_ONCHAIN_DATABASE = {
    "solana":      (True,  True,  False),   # Highly Bullish
    "ethereum":    (True,  False, False),   # Mixed
    "bitcoin":     (True,  True,  False),   # Bullish
    "cardano":     (False, False, True),    # Bearish (large inflows / dump risk)
    "ripple":      (False, False, False),   # Neutral
    "binancecoin": (True,  False, False),   # Mild Bullish  ← ADDED
}

MOCK_RESISTANCE_LEVELS = {
    "solana":      185.0,
    "ethereum":    3800.0,
    "cardano":     0.65,
    "ripple":      0.85,
    "dogecoin":    0.22,
    "binancecoin": 720.0,   # ← ADDED
}


def fetch_fear_greed_index() -> int:
    """
    Fetches the live Fear & Greed Index from the public, keyless alternative.me API.
    Returns an integer from 0 (Extreme Fear) to 100 (Extreme Greed).
    Falls back to neutral 50 on failure.
    """
    try:
        response = requests.get(FEAR_GREED_API_URL, timeout=8)
        response.raise_for_status()
        data = response.json()
        value = int(data["data"][0]["value"])
        logger.info(f"Successfully fetched live Fear & Greed Index: {value}")
        return value
    except Exception as e:
        logger.warning(f"Failed to fetch Fear & Greed index ({e}). Falling back to neutral 50.")
        return 50


def generate_synthetic_data(coin_id: str) -> Tuple[pd.Series, pd.Series, pd.DataFrame]:
    """
    Generates realistic synthetic market data to test the 10-factor trading system.
    Ensures RSI, MACD, OBV, Bollinger Bands, StochRSI, VWAP, ADX, and EMAs
    all execute cleanly with High and Low price parameters.
    """
    logger.info(f"[MOCK MODE] Generating synthetic market data for {coin_id.upper()}...")

    # Dynamic seed based on the coin name hash so different watchlist assets have unique mock pricing curves
    import hashlib
    seed_val = int(hashlib.sha256(coin_id.encode('utf-8')).hexdigest(), 16) % 10**8
    np.random.seed(seed_val)

    # BTC daily series — 250 days, clean uptrend so price > EMA 200
    btc_dates = pd.date_range(end=datetime.utcnow(), periods=250, freq="D")
    btc_base = np.linspace(61000, 74000, 250)
    np.random.seed(42)
    btc_noise = np.random.normal(0, 200, 250)
    btc_prices = pd.Series(btc_base + btc_noise, index=btc_dates)

    # Coin daily series — 250 days, aligned uptrend
    coin_daily_dates = pd.date_range(end=datetime.utcnow(), periods=250, freq="D")
    coin_daily_base = np.linspace(100, 155, 250)
    coin_daily_noise = np.random.normal(0, 0.8, 250)
    coin_daily_prices = pd.Series(coin_daily_base + coin_daily_noise, index=coin_daily_dates)

    # Coin hourly data — 30 days (720 hours), slight pullback to trigger RSI/MACD bounce
    coin_hourly_dates = pd.date_range(end=datetime.utcnow(), periods=720, freq="h")
    coin_hourly_base = np.linspace(165, 149, 720)
    cycles = np.sin(np.linspace(0, 5 * np.pi, 720)) * 6.5
    coin_hourly_noise = np.random.normal(0, 0.3, 720)
    hourly_prices = coin_hourly_base + cycles + coin_hourly_noise

    # Volume with a spike in the last 8 hours to trigger volume confirmation
    base_volume = np.random.exponential(scale=6000, size=720)
    base_volume[-8:] = base_volume[-8:] * 4.2

    df_hourly = pd.DataFrame(
        {"price": hourly_prices, "volume": base_volume},
        index=coin_hourly_dates
    )
    df_hourly["high"] = df_hourly["price"] * 1.01
    df_hourly["low"] = df_hourly["price"] * 0.99

    # Resample to 4H bars
    df_4h = df_hourly.resample("4h").agg({
        "price": "last",
        "high": "max",
        "low": "min",
        "volume": "sum"
    }).dropna()

    return btc_prices, coin_daily_prices, df_4h


def fetch_coingecko_market_chart(
    coin_id: str,
    days: int,
    vs_currency: str = DEFAULT_VS_CURRENCY,
    interval: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Fetches raw historical market chart data from CoinGecko API with retry-on-429.
    """
    endpoint = f"{COINGECKO_BASE_URL}/coins/{coin_id}/market_chart"
    params = {"vs_currency": vs_currency, "days": str(days)}
    if interval:
        params["interval"] = interval

    headers = {
        "accept": "application/json",
        "User-Agent": "UnifiedCryptoPipeline/3.1"
    }

    max_retries = 3
    retry_delay = 6.0

    for attempt in range(max_retries):
        time.sleep(2.0)
        try:
            response = requests.get(endpoint, params=params, headers=headers, timeout=15)
            if response.status_code == 429:
                logger.warning(
                    f"CoinGecko Rate Limit (429) for {coin_id}. "
                    f"Attempt {attempt + 1}/{max_retries}. Retrying in {retry_delay}s..."
                )
                time.sleep(retry_delay)
                retry_delay *= 2.0
                continue
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"HTTP Request failed on attempt {attempt + 1}: {e}")
            time.sleep(retry_delay)
            retry_delay *= 1.5

    logger.error(f"Failed to fetch market chart for {coin_id} after {max_retries} attempts.")
    return None


def get_daily_prices(coin_id: str, days: int = 250) -> Optional[pd.Series]:
    """
    Retrieves daily prices to compute EMAs (20, 50, 200).
    """
    data = fetch_coingecko_market_chart(coin_id, days=days, interval="daily")
    if not data or "prices" not in data:
        return None

    df = pd.DataFrame(data["prices"], columns=["timestamp", "price"])
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("datetime", inplace=True)
    return df["price"]


def get_4h_dataframe(coin_id: str, days: int = 30) -> Optional[pd.DataFrame]:
    """
    Fetches hourly price/volume, generates high/low, and resamples to 4H bars.
    """
    data = fetch_coingecko_market_chart(coin_id, days=days)
    if not data or "prices" not in data or "total_volumes" not in data:
        return None

    df_prices = pd.DataFrame(data["prices"], columns=["timestamp", "price"])
    df_volumes = pd.DataFrame(data["total_volumes"], columns=["timestamp", "volume"])

    df = pd.merge(df_prices, df_volumes, on="timestamp")
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("datetime", inplace=True)

    df["high"] = df["price"] * 1.005
    df["low"] = df["price"] * 0.995

    df_4h = df.resample("4h").agg({
        "price": "last",
        "high": "max",
        "low": "min",
        "volume": "sum"
    }).dropna()

    return df_4h


def calculate_rsi(prices: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """
    Calculates RSI using Wilder's Smoothing method.
    """
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    smoothed_gain = pd.Series(index=prices.index, dtype=float)
    smoothed_loss = pd.Series(index=prices.index, dtype=float)

    if len(prices) < period + 1:
        return pd.Series(np.nan, index=prices.index)

    smoothed_gain.iloc[period] = gain.iloc[1: period + 1].mean()
    smoothed_loss.iloc[period] = loss.iloc[1: period + 1].mean()

    for i in range(period + 1, len(prices)):
        smoothed_gain.iloc[i] = (smoothed_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
        smoothed_loss.iloc[i] = (smoothed_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

    rs = smoothed_gain / smoothed_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_macd(prices: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
    ema12 = prices.ewm(span=12, adjust=False).mean()
    ema26 = prices.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_obv(prices: pd.Series, volumes: pd.Series) -> pd.Series:
    obv = [0.0]
    for i in range(1, len(prices)):
        if prices.iloc[i] > prices.iloc[i - 1]:
            obv.append(obv[-1] + volumes.iloc[i])
        elif prices.iloc[i] < prices.iloc[i - 1]:
            obv.append(obv[-1] - volumes.iloc[i])
        else:
            obv.append(obv[-1])
    return pd.Series(obv, index=prices.index)


def calculate_bollinger_bands(df: pd.DataFrame, window: int = 20) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Calculates 20-period Bollinger Bands (Upper, Middle/SMA, Lower).
    """
    sma = df["price"].rolling(window=window).mean()
    std = df["price"].rolling(window=window).std()
    upper = sma + (2 * std)
    lower = sma - (2 * std)
    return upper, sma, lower


def calculate_stochastic_rsi(rsi_series: pd.Series, period: int = 14) -> Tuple[pd.Series, pd.Series]:
    """
    Calculates Stochastic RSI (%K and %D lines).
    """
    min_rsi = rsi_series.rolling(period).min()
    max_rsi = rsi_series.rolling(period).max()
    stoch_rsi = (rsi_series - min_rsi) / (max_rsi - min_rsi).replace(0, np.nan)
    stoch_k = stoch_rsi.rolling(3).mean() * 100
    stoch_d = stoch_k.rolling(3).mean()
    return stoch_k, stoch_d


def calculate_vwap(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """
    Calculates rolling VWAP over a given window.
    """
    pv = df["price"] * df["volume"]
    rolling_pv = pv.rolling(window=window).sum()
    rolling_vol = df["volume"].rolling(window=window).sum()
    return rolling_pv / rolling_vol.replace(0, np.nan)


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Calculates Average Directional Index (ADX) measuring trend strength.
    """
    prev_close = df["price"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    up_move = df["high"] - df["high"].shift(1)
    down_move = df["low"].shift(1) - df["low"]

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)

    atr = tr.rolling(period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(period).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(period).mean() / atr.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.rolling(period).mean()
    return adx


def send_signal_alert(
    coin_id: str,
    direction: str,
    score: int,
    metrics: Dict[str, Any],
    webhook_url: str = WEBHOOK_URL
) -> bool:
    """
    Sends a structured buy/sell trading alert to a Discord webhook.
    """
    color_bar = "🟢" if direction == "BUY" else "🔴"
    title = f"{color_bar} **COMPOSITE SYSTEM SCANNER: {direction} SIGNAL** {color_bar}"

    if direction == "BUY":
        checklist = [
            f"{'✅' if metrics['btc_above_200ema'] else '❌'} 1. BTC Macro Filter (Price > 200 EMA)",
            f"{'✅' if metrics['buy_ema_aligned'] else '❌'} 2. Daily EMA Alignment ({metrics['ema_alignment_type']})",
            f"{'✅' if metrics['buy_rsi'] else '❌'} 3. 4H RSI ({metrics['rsi_4h']:.1f})",
            f"{'✅' if metrics['buy_macd'] else '❌'} 4. 4H MACD Crossover",
            f"{'✅' if metrics['buy_volume_obv'] else '❌'} 5. Volume Spike ({metrics['volume_ratio']:.2f}x / OBV Bonus: {metrics['obv_bonus']})",
            f"{'✅' if metrics['buy_bb'] else '❌'} 6. Bollinger Bands (Price near lower band)",
            f"{'✅' if metrics['buy_stoch_rsi'] else '❌'} 7. Stochastic RSI (Cross up from below 20)",
            f"{'✅' if metrics['buy_vwap'] else '❌'} 8. VWAP Support (Price above VWAP)",
            f"{'✅' if metrics['buy_sentiment'] else '❌'} 9. Sentiment (Fear & Greed Index: {metrics['fear_greed']})",
            f"{'✅' if metrics['buy_adx'] else '❌'} 10. ADX Trend Strength ({metrics['adx']:.1f})",
        ]
        mandatory_status = (
            f"⚠️ **Mandatory Filters:**\n"
            f"  * [Rule #1] BTC Macro Trend: {'✅' if metrics['btc_above_200ema'] else '❌'}\n"
            f"  * [Rule #8] Risk/Reward (>= 2.0): {'✅' if metrics['risk_reward_pass'] else '❌'}\n\n"
        )
    else:
        checklist = [
            f"{'🚨' if metrics['sell_ema'] else '⚪'} 1. EMA Downtrend (Price < 200 EMA)",
            f"{'🚨' if metrics['sell_rsi'] else '⚪'} 2. RSI Overbought (RSI: {metrics['rsi_4h']:.1f})",
            f"{'🚨' if metrics['sell_macd'] else '⚪'} 3. MACD Bearish Crossover",
            f"{'🚨' if metrics['sell_volume_obv'] else '⚪'} 4. OBV Divergence (Price rising, OBV falling)",
            f"{'🚨' if metrics['sell_bb'] else '⚪'} 5. Bollinger Overextended (Price walks upper band)",
            f"{'🚨' if metrics['sell_stoch_rsi'] else '⚪'} 6. Stochastic RSI (Cross down from above 80)",
            f"{'🚨' if metrics['sell_vwap'] else '⚪'} 7. VWAP Rejection (Price rejected below VWAP)",
            f"{'🚨' if metrics['sell_sentiment'] else '⚪'} 8. Sentiment Peak (Fear & Greed: {metrics['fear_greed']})",
            f"{'🚨' if metrics['sell_adx'] else '⚪'} 9. ADX Choppiness (ADX: {metrics['adx']:.1f} < 15)",
            f"{'🚨' if metrics['sell_onchain'] else '⚪'} 10. Whale Dumping (Large exchange inflows detected)",
        ]
        mandatory_status = ""

    checklist_str = "\n".join(checklist)

    payload = {
        "username": "System Scanner Bot",
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
        logger.warning(
            f"[MOCK NOTIFICATION] Webhook is a placeholder. Skipping HTTP POST.\n"
            f"Alert Payload:\n{json.dumps(payload, indent=2)}"
        )
        return True

    try:
        headers = {"Content-Type": "application/json"}
        response = requests.post(webhook_url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        logger.info(f"{direction} alert successfully delivered to Discord.")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to deliver {direction} webhook: {e}")
        return False


def evaluate_trading_system(
    coin_id: str,
    target_override: Optional[float] = None,
    stop_override: Optional[float] = None,
    resistance_override: Optional[float] = None,
    use_mock_data: bool = False,
    force_buy: bool = False,
    force_sell: bool = False
) -> Dict[str, Any]:
    """
    Evaluates the full 10-indicator buy and sell strategy for a given coin.
    Sends a Discord alert when buy_score >= 7 (with mandatory filters passing)
    or sell_score >= 7.
    """
    report = {
        "status": "failed",
        "coin_id": coin_id,
        "buy_score": 0,
        "sell_score": 0,
        "buy_alert_triggered": False,
        "sell_alert_triggered": False,
        "metrics": {}
    }

    logger.info(f"=== Running 10-Indicator Scan on {coin_id.upper()} ===")

    # --- Fetch Fear & Greed Index ---
    fear_greed = fetch_fear_greed_index()

    # --- Acquire price data ---
    if use_mock_data:
        btc_daily, coin_daily, df_4h = generate_synthetic_data(coin_id)
        latest_btc_price = btc_daily.iloc[-1]
        btc_ema_200 = btc_daily.ewm(span=200, adjust=False).mean()
        latest_btc_ema = btc_ema_200.iloc[-1]

        latest_price = coin_daily.iloc[-1]
        coin_ema_20 = coin_daily.ewm(span=20, adjust=False).mean()
        coin_ema_50 = coin_daily.ewm(span=50, adjust=False).mean()
        coin_ema_200 = coin_daily.ewm(span=200, adjust=False).mean()
        latest_ema20 = coin_ema_20.iloc[-1]
        latest_ema50 = coin_ema_50.iloc[-1]
        latest_ema200 = coin_ema_200.iloc[-1]
    else:
        logger.info("Fetching daily BTC prices...")
        btc_daily = get_daily_prices("bitcoin", days=250)
        if btc_daily is None or len(btc_daily) < 200:
            report["error"] = "Insufficient BTC daily data"
            return report

        btc_ema_200 = btc_daily.ewm(span=200, adjust=False).mean()
        latest_btc_price = btc_daily.iloc[-1]
        latest_btc_ema = btc_ema_200.iloc[-1]

        logger.info("Pacing API... sleeping 8s...")
        time.sleep(8.0)

        logger.info(f"Fetching daily {coin_id.upper()} prices...")
        coin_daily = get_daily_prices(coin_id, days=250)
        if coin_daily is None or len(coin_daily) < 200:
            report["error"] = f"Insufficient daily data for {coin_id}"
            return report

        coin_ema_20 = coin_daily.ewm(span=20, adjust=False).mean()
        coin_ema_50 = coin_daily.ewm(span=50, adjust=False).mean()
        coin_ema_200 = coin_daily.ewm(span=200, adjust=False).mean()

        latest_price = coin_daily.iloc[-1]
        latest_ema20 = coin_ema_20.iloc[-1]
        latest_ema50 = coin_ema_50.iloc[-1]
        latest_ema200 = coin_ema_200.iloc[-1]

        logger.info("Pacing API... sleeping 8s...")
        time.sleep(8.0)

        logger.info(f"Fetching 4H data for {coin_id.upper()}...")
        df_4h = get_4h_dataframe(coin_id, days=30)
        if df_4h is None or len(df_4h) < 30:
            report["error"] = f"Insufficient 4H data for {coin_id}"
            return report

    # ==========================================================
    # INDICATOR CALCULATIONS
    # ==========================================================
    df_4h["rsi"] = calculate_rsi(df_4h["price"], period=RSI_PERIOD)
    macd_l, signal_l, hist_l = calculate_macd(df_4h["price"])
    df_4h["macd"] = macd_l
    df_4h["signal"] = signal_l
    df_4h["histogram"] = hist_l
    df_4h["obv"] = calculate_obv(df_4h["price"], df_4h["volume"])

    upper_band, middle_band, lower_band = calculate_bollinger_bands(df_4h)
    df_4h["bb_upper"] = upper_band
    df_4h["bb_lower"] = lower_band

    stoch_k, stoch_d = calculate_stochastic_rsi(df_4h["rsi"])
    df_4h["stoch_k"] = stoch_k
    df_4h["stoch_d"] = stoch_d

    df_4h["vwap"] = calculate_vwap(df_4h)
    df_4h["adx"] = calculate_adx(df_4h)

    latest_4h = df_4h.iloc[-1]
    prev_4h = df_4h.iloc[-2]

    latest_rsi_4h     = latest_4h["rsi"]
    latest_macd       = latest_4h["macd"]
    latest_signal     = latest_4h["signal"]
    latest_hist       = latest_4h["histogram"]
    prev_hist         = prev_4h["histogram"]
    latest_stoch_k    = latest_4h["stoch_k"]
    latest_stoch_d    = latest_4h["stoch_d"]
    prev_stoch_k      = prev_4h["stoch_k"]
    latest_vwap       = latest_4h["vwap"]
    latest_adx        = latest_4h["adx"]
    prev_adx          = prev_4h["adx"]

    whale_accumulating, exchange_outflows_rising, large_inflows_detected = MOCK_ONCHAIN_DATABASE.get(
        coin_id.lower(), (True, False, False)
    )

    # ==========================================================
    # FORCE-BUY OVERRIDE (test/demo only)
    # ==========================================================
    if force_buy:
        logger.info("[FORCED PROJECTION] Injecting bullish data to trigger a BUY alert...")
        fear_greed         = 25
        latest_rsi_4h      = 28.5
        latest_macd        = 0.8
        latest_signal      = 0.2
        latest_hist        = 0.6
        prev_hist          = 0.1
        latest_stoch_k     = 25.0
        prev_stoch_k       = 15.0
        latest_stoch_d     = 20.0
        latest_vwap        = 140.0
        latest_price       = 143.0
        latest_ema200      = 135.0
        latest_ema50       = 138.0
        latest_ema20       = 140.0
        latest_btc_price   = 72000.0
        latest_btc_ema     = 68000.0
        latest_adx         = 30.0
        whale_accumulating         = True
        exchange_outflows_rising   = True
        large_inflows_detected     = False

        df_4h = df_4h.copy()
        latest_4h = df_4h.iloc[-1].copy()
        latest_4h["bb_upper"] = 155.0
        latest_4h["bb_lower"] = 142.5
        latest_4h["price"]    = 143.0
        latest_4h["volume"]   = 12000
        latest_4h["obv"]      = 80000

        prev_4h = df_4h.iloc[-2].copy()
        prev_4h["price"] = 144.0

    # ==========================================================
    # FORCE-SELL OVERRIDE (test/demo only)
    # ==========================================================
    if force_sell:
        logger.info("[FORCED PROJECTION] Injecting bearish data to trigger a SELL alert...")
        fear_greed         = 88
        latest_rsi_4h      = 78.5
        latest_macd        = -1.2
        latest_signal      = 0.5
        latest_hist        = -1.7
        prev_hist          = -0.8
        latest_stoch_k     = 75.0
        prev_stoch_k       = 85.0
        latest_stoch_d     = 80.0
        latest_vwap        = 160.0
        latest_price       = 145.0
        latest_ema200      = 150.0
        latest_ema50       = 148.0
        latest_ema20       = 146.0
        latest_btc_price   = 55000.0
        latest_btc_ema     = 60000.0
        latest_adx         = 10.0
        whale_accumulating         = False
        exchange_outflows_rising   = False
        large_inflows_detected     = True

        # Work on DataFrame copies so OBV divergence check is consistent
        df_4h = df_4h.copy()
        df_4h.iloc[-4, df_4h.columns.get_loc("price")] = 140.0
        df_4h.iloc[-4, df_4h.columns.get_loc("obv")]   = 60000

        # Re-read latest/prev rows after modification
        latest_4h = df_4h.iloc[-1].copy()
        latest_4h["bb_upper"] = 145.2
        latest_4h["bb_lower"] = 135.0
        latest_4h["price"]    = 145.0
        latest_4h["volume"]   = 1000
        latest_4h["obv"]      = 50000

        prev_4h = df_4h.iloc[-2].copy()
        prev_4h["price"] = 147.0

    # ==========================================================
    # STRATEGY SCORING
    # ==========================================================
    buy_score  = 0
    sell_score = 0

    metrics: Dict[str, Any] = {
        "current_price":          float(latest_price),
        "fear_greed":             int(fear_greed),
        "rsi_4h":                 float(latest_rsi_4h),
        "adx":                    float(latest_adx),
        "whale_accumulating":     whale_accumulating,
        "large_inflows_detected": large_inflows_detected,
    }

    # ----------------------------------------------------------
    # Indicator 1 — BTC Macro Filter (EMA 200 on daily)
    # FIX: now also increments buy_score (was missing in original)
    # ----------------------------------------------------------
    btc_above_200ema = bool(latest_btc_price > latest_btc_ema)
    metrics["btc_above_200ema"] = btc_above_200ema
    if btc_above_200ema:
        buy_score += 1

    # ----------------------------------------------------------
    # Indicator 2 — Daily EMA Alignment
    # ----------------------------------------------------------
    buy_ema_aligned    = False
    ema_alignment_type = "No Alignment"

    if latest_price > latest_ema20 > latest_ema50 > latest_ema200:
        buy_ema_aligned    = True
        ema_alignment_type = "Full Alignment (Bullish)"
    elif latest_price > latest_ema50 > latest_ema200:
        buy_ema_aligned    = True
        ema_alignment_type = "Partial Pass (Bullish)"

    sell_ema = bool(latest_price < latest_ema200)

    metrics["buy_ema_aligned"]    = buy_ema_aligned
    metrics["ema_alignment_type"] = ema_alignment_type
    metrics["sell_ema"]           = sell_ema

    if buy_ema_aligned: buy_score  += 1
    if sell_ema:        sell_score += 1

    # ----------------------------------------------------------
    # Indicator 3 — RSI (14-period, 4H)
    # ----------------------------------------------------------
    buy_rsi  = bool((40.0 <= latest_rsi_4h <= 65.0) or (latest_rsi_4h < 30.0))
    sell_rsi = bool(latest_rsi_4h >= 75.0 or (latest_rsi_4h < 25.0 and latest_price < latest_ema200))

    metrics["buy_rsi"]  = buy_rsi
    metrics["sell_rsi"] = sell_rsi

    if buy_rsi:  buy_score  += 1
    if sell_rsi: sell_score += 1

    # ----------------------------------------------------------
    # Indicator 4 — MACD (4H)
    # ----------------------------------------------------------
    buy_macd  = bool((latest_macd > latest_signal) and (latest_hist > 0) and (latest_hist > prev_hist))
    sell_macd = bool(latest_macd < latest_signal)

    metrics["buy_macd"]  = buy_macd
    metrics["sell_macd"] = sell_macd

    if buy_macd:  buy_score  += 1
    if sell_macd: sell_score += 1

    # ----------------------------------------------------------
    # Indicator 5 — Volume + OBV
    # ----------------------------------------------------------
    avg_vol_20    = df_4h["volume"].rolling(window=20).mean().iloc[-1]
    volume_ratio  = latest_4h["volume"] / avg_vol_20 if avg_vol_20 > 0 else 0
    volume_spike  = bool(volume_ratio >= 1.5)

    obv_bonus = False
    if len(df_4h) >= 4:
        obv_bonus = bool(latest_4h["obv"] > df_4h["obv"].iloc[-4])

    buy_volume_obv = volume_spike or obv_bonus

    # OBV divergence: price rising but OBV falling
    obv_divergence = False
    if len(df_4h) >= 4:
        price_rising  = float(latest_4h["price"]) > float(df_4h["price"].iloc[-4])
        obv_falling   = float(latest_4h["obv"])   < float(df_4h["obv"].iloc[-4])
        obv_divergence = bool(price_rising and obv_falling)

    metrics["buy_volume_obv"] = buy_volume_obv
    metrics["volume_ratio"]   = float(volume_ratio)
    metrics["obv_bonus"]      = obv_bonus
    metrics["sell_volume_obv"]= obv_divergence

    if buy_volume_obv:  buy_score  += 1
    if obv_divergence:  sell_score += 1

    # ----------------------------------------------------------
    # Indicator 6 — Bollinger Bands
    # ----------------------------------------------------------
    buy_bb  = bool(latest_price <= latest_4h["bb_lower"] * 1.015)
    sell_bb = bool(latest_price >= latest_4h["bb_upper"] * 0.985)

    metrics["buy_bb"]  = buy_bb
    metrics["sell_bb"] = sell_bb

    if buy_bb:  buy_score  += 1
    if sell_bb: sell_score += 1

    # ----------------------------------------------------------
    # Indicator 7 — Stochastic RSI
    # FIX: sell condition now uses stoch_k < stoch_d as primary signal
    # (avoids the one-bar-late issue in the original)
    # ----------------------------------------------------------
    buy_stoch_rsi  = bool(prev_stoch_k < 20.0 and latest_stoch_k >= 20.0 and latest_stoch_k > latest_stoch_d)

    # FIXED: detect bearish momentum by %K crossing below %D above 70,
    # not requiring the bar to have already crossed 80 → catches the signal one bar earlier
    sell_stoch_rsi = bool(latest_stoch_k < latest_stoch_d and latest_stoch_k > 70.0)

    metrics["buy_stoch_rsi"]  = buy_stoch_rsi
    metrics["sell_stoch_rsi"] = sell_stoch_rsi

    if buy_stoch_rsi:  buy_score  += 1
    if sell_stoch_rsi: sell_score += 1

    # ----------------------------------------------------------
    # Indicator 8 — VWAP
    # FIX: widened buy window from 2.5% to 5% so altcoins can pass
    # ----------------------------------------------------------
    buy_vwap  = bool(latest_price > latest_vwap and latest_price <= latest_vwap * 1.05)
    sell_vwap = bool(latest_price < latest_vwap and float(prev_4h["price"]) < latest_vwap)

    metrics["buy_vwap"]  = buy_vwap
    metrics["sell_vwap"] = sell_vwap

    if buy_vwap:  buy_score  += 1
    if sell_vwap: sell_score += 1

    # ----------------------------------------------------------
    # Indicator 9 — Fear & Greed Sentiment
    # ----------------------------------------------------------
    buy_sentiment  = bool(20 <= fear_greed <= 40)
    sell_sentiment = bool(80 <= fear_greed <= 100)

    metrics["buy_sentiment"]  = buy_sentiment
    metrics["sell_sentiment"] = sell_sentiment

    if buy_sentiment:  buy_score  += 1
    if sell_sentiment: sell_score += 1

    # ----------------------------------------------------------
    # Indicator 10 — ADX (trend strength)
    # ----------------------------------------------------------
    buy_adx  = bool(latest_adx >= 25.0 and latest_adx > prev_adx)
    sell_adx = bool(latest_adx < 15.0)

    metrics["buy_adx"]  = buy_adx
    metrics["sell_adx"] = sell_adx

    if buy_adx:  buy_score  += 1
    if sell_adx: sell_score += 1

    # ----------------------------------------------------------
    # On-chain Whale Flows (used inside ADX checklist row for BUY;
    # standalone scored indicator for SELL)
    # ----------------------------------------------------------
    buy_onchain  = bool(whale_accumulating or exchange_outflows_rising)
    sell_onchain = bool(large_inflows_detected)

    metrics["buy_onchain"]  = buy_onchain
    metrics["sell_onchain"] = sell_onchain

    if sell_onchain: sell_score += 1

    # ==========================================================
    # POSITION SIZING — Stop Loss, Target, Risk/Reward
    # ==========================================================
    stop_loss = stop_override
    if stop_loss is None:
        stop_loss = df_4h["price"].iloc[-20:].min()
        if stop_loss >= latest_price:
            # Fallback: 5% below entry; log a warning so the user knows
            stop_loss = latest_price * 0.95
            logger.warning(
                f"[{coin_id.upper()}] Swing-low stop was at or above entry price. "
                f"Falling back to 5% stop (${stop_loss:,.2f}). "
                f"Review this manually before placing the trade."
            )

    resistance_level = resistance_override or MOCK_RESISTANCE_LEVELS.get(
        coin_id.lower(), df_4h["price"].max()
    )

    target_price = target_override
    if target_price is None:
        target_price = resistance_level
        if target_price <= latest_price:
            target_price = latest_price * 1.05

    risk   = latest_price - stop_loss
    reward = target_price - latest_price

    risk_reward_ratio = reward / risk if risk > 0 else 0
    risk_reward_pass  = bool(risk_reward_ratio >= 2.0)

    metrics["stop_loss"]          = float(stop_loss)
    metrics["target_price"]       = float(target_price)
    metrics["risk_reward_ratio"]  = float(risk_reward_ratio)
    metrics["risk_reward_pass"]   = risk_reward_pass

    resistance_dist_pct = ((resistance_level - latest_price) / latest_price) * 100
    metrics["resistance_dist_pct"]  = float(resistance_dist_pct)
    metrics["resistance_room_pass"] = bool(resistance_dist_pct >= 3.0)

    # ==========================================================
    # FINAL EVALUATION
    # ==========================================================
    report["buy_score"]  = buy_score
    report["sell_score"] = sell_score
    report["metrics"]    = metrics

    # Both BTC trend AND risk/reward must pass for a buy alert to fire
    buy_mandatory_passed = bool(btc_above_200ema and risk_reward_pass)
    report["buy_mandatory_passed"] = buy_mandatory_passed
    report["status"] = "success"

    if buy_score >= 7 and buy_mandatory_passed:
        logger.warning(f"🟢 BUY SIGNAL on {coin_id.upper()}! Score: {buy_score}/10. Sending alert...")
        alert_sent = send_signal_alert(
            coin_id=coin_id, direction="BUY",
            score=buy_score, metrics=metrics, webhook_url=WEBHOOK_URL
        )
        report["buy_alert_triggered"]    = True
        report["buy_notification_sent"]  = alert_sent

    elif sell_score >= 7:
        logger.warning(f"🔴 SELL SIGNAL on {coin_id.upper()}! Score: {sell_score}/10. Sending alert...")
        alert_sent = send_signal_alert(
            coin_id=coin_id, direction="SELL",
            score=sell_score, metrics=metrics, webhook_url=WEBHOOK_URL
        )
        report["sell_alert_triggered"]   = True
        report["sell_notification_sent"] = alert_sent

    else:
        logger.info(
            f"[{coin_id.upper()}] Scan complete — "
            f"Buy: {buy_score}/10 (mandatory: {buy_mandatory_passed}) | "
            f"Sell: {sell_score}/10 — No alert threshold met."
        )

    return report


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Entry point for AWS Lambda / cloud triggers.
    """
    logger.info("Executing Lambda handler...")
    coin_id  = event.get("coin_id", DEFAULT_COIN)
    use_mock = event.get("mock", False)
    force_buy = event.get("force_buy", False)
    force_sell = event.get("force_sell", False)

    output = evaluate_trading_system(
        coin_id=coin_id,
        target_override=event.get("target_price"),
        stop_override=event.get("stop_loss"),
        resistance_override=event.get("resistance_level"),
        use_mock_data=use_mock,
        force_buy=force_buy,
        force_sell=force_sell
    )

    return {
        "statusCode": 200 if output["status"] == "success" else 500,
        "body": json.dumps(output)
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Crypto Trading System Scanner — 10-Factor Buy/Sell Model"
    )
    parser.add_argument("--coin",       type=str,   default=DEFAULT_COIN, help="Single coin ID to analyze")
    parser.add_argument("--watchlist",  action="store_true",              help="Scan all 5 coins in the watchlist")
    parser.add_argument("--target",     type=float, default=None,         help="Force a specific target price")
    parser.add_argument("--stop",       type=float, default=None,         help="Force a specific stop loss")
    parser.add_argument("--resistance", type=float, default=None,         help="Force a specific resistance level")
    parser.add_argument("--mock",       action="store_true",              help="Run with synthetic market data")
    parser.add_argument("--force-buy",  action="store_true",              help="Inject bullish data to test BUY alerts")
    parser.add_argument("--force-sell", action="store_true",              help="Inject bearish data to test SELL alerts")

    args = parser.parse_args()

    # ----------------------------------------------------------
    # Watchlist mode: scan all 5 top coins sequentially
    # Usage: python crypto_scanner.py --watchlist [--mock]
    # ----------------------------------------------------------
    if args.watchlist:
        logger.info(f"=== WATCHLIST MODE: scanning {WATCHLIST} ===")
        all_results = {}

        for coin in WATCHLIST:
            result = evaluate_trading_system(
                coin_id=coin,
                use_mock_data=args.mock,
                force_buy=args.force_buy,
                force_sell=args.force_sell
            )
            all_results[coin] = result

            print(f"\n--- {coin.upper()} ---")
            print(f"  Buy Score  : {result.get('buy_score', 'N/A')}/10")
            print(f"  Sell Score : {result.get('sell_score', 'N/A')}/10")
            print(f"  Buy Alert  : {result.get('buy_alert_triggered', False)}")
            print(f"  Sell Alert : {result.get('sell_alert_triggered', False)}")

            if coin != WATCHLIST[-1]:
                logger.info("Pacing API... sleeping 10s before next coin...")
                time.sleep(10)

        print("\n\n=== WATCHLIST SCAN COMPLETE ===")
        print(json.dumps(all_results, indent=4))

    # ----------------------------------------------------------
    # Single coin mode (default)
    # Usage: python crypto_scanner.py --coin bitcoin [--mock]
    # ----------------------------------------------------------
    else:
        result = evaluate_trading_system(
            coin_id=args.coin,
            target_override=args.target,
            stop_override=args.stop,
            resistance_override=args.resistance,
            use_mock_data=args.mock,
            force_buy=args.force_buy,
            force_sell=args.force_sell
        )

        print("\n--- Pipeline Execution Report ---")
        print(json.dumps(result, indent=4))