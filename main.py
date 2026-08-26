# =============================================================================
# XAUUSD V21 — RESEARCH / MONITORING ENGINE  (GC=F FIX BUILD)
# =============================================================================

import os
import json
import time
import logging
import traceback

import requests
import numpy as np
import pandas as pd
import yfinance as yf


# =============================================================================
# 1. CONFIG
# =============================================================================

SYMBOL = "GC=F"

TIMEFRAME = "15m"

DOWNLOAD_PERIOD = "30d"

MIN_RAW_BARS = 350

MIN_ANALYSIS_BARS = 250

CHECK_EVERY_SECONDS = 60

STALE_AFTER_SECONDS = 3600

DOWNLOAD_RETRIES = 3
RETRY_BASE_SECONDS = 5

TELEGRAM_BOT_TOKEN = "8753393752:AAGDU0V0HdsXGu8ViYf8ZtOq1a_MpYuDfdc"
TELEGRAM_CHAT_ID = "5535955736"

ALERT_COOLDOWN_SECONDS = 15 * 60

PERIODIC_REPORT_HOURS = 6

STATE_FILE = "xauusd_v21_state.json"

SIGNALS_FILE = "XAUUSD_V21_SIGNALS.csv"


# =============================================================================
# 2. STRATEGY PARAMETERS
# =============================================================================

EMA_FAST = 9
EMA_SLOW = 21
EMA_MID = 50
EMA_TREND = 200

RSI_PERIOD = 14

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

ATR_PERIOD = 14

ADX_PERIOD = 14
ADX_MIN = 22

STOCH_K_PERIOD = 14
STOCH_D_PERIOD = 3

VWAP_ENABLED = True

ATR_SL_MULT = 1.5
ATR_TP_MULT = 3.0

MIN_SCORE_RATIO = 0.65

SWEEP_LOOKBACK = 20
SWEEP_ATR_MIN = 0.10

FVG_LOOKBACK = 50

SWING_LEFT = 3
SWING_RIGHT = 3

SR_LOOKBACK = 250
SR_CLUSTER_ATR = 0.35

H1_PERIOD = "30d"
H1_MIN_BARS = 150

BOS_VALIDITY_HOURS = 6

H1_CACHE_TTL_SECONDS = 3600


# =============================================================================
# 3. LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s"
)

logger = logging.getLogger("XAUUSD_V21")


# =============================================================================
# 4. STATE
# =============================================================================

DEFAULT_STATE = {
    "last_processed_candle": None,
    "last_alert_key": None,
    "last_alert_timestamp": None,
    "last_periodic_report": None,
    "total_alerts": 0,
    "buy_alerts": 0,
    "sell_alerts": 0,
}


def load_state():
    if not os.path.exists(STATE_FILE):
        return DEFAULT_STATE.copy()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        state = DEFAULT_STATE.copy()
        state.update(data)
        return state

    except Exception as e:
        logger.warning(f"State load failed: {e}")
        return DEFAULT_STATE.copy()


def save_state(state):
    tmp_file = STATE_FILE + ".tmp"

    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

        os.replace(tmp_file, STATE_FILE)

    except Exception as e:
        logger.error(f"State save failed: {e}")


STATE = load_state()


_h1_cache = {
    "direction": None,
    "timestamp": 0.0
}


# =============================================================================
# 5. TELEGRAM
# =============================================================================

def telegram_configured():
    return bool(
        TELEGRAM_BOT_TOKEN
        and TELEGRAM_CHAT_ID
        and TELEGRAM_BOT_TOKEN != "YOUR_BOT_TOKEN"
        and TELEGRAM_CHAT_ID != "YOUR_CHAT_ID"
    )


def send_telegram(message):

    if not telegram_configured():
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        if response.status_code == 200:
            return True

        logger.error(
            f"Telegram error {response.status_code}: "
            f"{response.text[:500]}"
        )

        return False

    except Exception as e:
        logger.error(f"Telegram connection error: {e}")
        return False


def test_telegram():
    logger.info("Testing Telegram connection...")

    ok = send_telegram(
        "🟢 *XAUUSD V21 ONLINE*\n"
        "Research / Monitoring engine started.\n"
        "No real orders are executed."
    )

    if ok:
        logger.info("Telegram connection: OK")
    else:
        logger.error("Telegram connection: FAILED")

    return ok


# =============================================================================
# 6. MARKET DATA
# =============================================================================

def normalize_yfinance_columns(df):
    if df is None or df.empty:
        return df

    if isinstance(df.columns, pd.MultiIndex):
        try:
            df.columns = df.columns.get_level_values(0)
        except Exception:
            df.columns = [
                col[0] if isinstance(col, tuple) else col
                for col in df.columns
            ]

    df.columns = [
        str(c).strip().title()
        for c in df.columns
    ]

    return df


def download_market_data(
    symbol=SYMBOL,
    interval=TIMEFRAME,
    period=DOWNLOAD_PERIOD
):

    last_error = None

    for attempt in range(1, DOWNLOAD_RETRIES + 1):

        try:

            logger.info(
                f"Downloading {symbol} {interval} "
                f"data | attempt {attempt}/{DOWNLOAD_RETRIES}"
            )

            df = yf.download(
                tickers=symbol,
                period=period,
                interval=interval,
                auto_adjust=False,
                progress=False,
                threads=False,
            )

            df = normalize_yfinance_columns(df)

            if df is None or df.empty:
                raise RuntimeError("Yahoo returned empty dataframe")

            required = [
                "Open",
                "High",
                "Low",
                "Close",
            ]

            missing = [
                c for c in required
                if c not in df.columns
            ]

            if missing:
                raise RuntimeError(
                    f"Missing columns: {missing}"
                )

            df = df.copy()

            df.index = pd.to_datetime(
                df.index,
                utc=True
            )

            df = df.sort_index()

            df = df[~df.index.duplicated(keep="last")]

            for col in ["Open", "High", "Low", "Close"]:
                df[col] = pd.to_numeric(
                    df[col],
                    errors="coerce"
                )

            if "Volume" not in df.columns:
                df["Volume"] = np.nan

            df["Volume"] = pd.to_numeric(
                df["Volume"],
                errors="coerce"
            )

            df = df.dropna(
                subset=[
                    "Open",
                    "High",
                    "Low",
                    "Close"
                ]
            )

            if len(df) < MIN_RAW_BARS:
                raise RuntimeError(
                    f"Not enough raw bars: {len(df)} "
                    f"< {MIN_RAW_BARS}"
                )

            return df

        except Exception as e:

            last_error = e

            logger.warning(
                f"Yahoo download failed: {e}"
            )

            if attempt < DOWNLOAD_RETRIES:
                wait = RETRY_BASE_SECONDS * attempt
                logger.info(
                    f"Retrying in {wait}s..."
                )
                time.sleep(wait)

    raise RuntimeError(
        f"Yahoo download failed after retries: {last_error}"
    )


# =============================================================================
# 7. CLOSED CANDLE / FRESHNESS / WEEKEND
# =============================================================================

def get_closed_dataframe(df):

    if df is None or len(df) < 3:
        return None, None

    closed_df = df.iloc[:-1].copy()

    if closed_df.empty:
        return None, None

    return (
        closed_df,
        closed_df.index[-1]
    )


def is_weekend():
    now = pd.Timestamp.now(tz="UTC")
    return now.weekday() >= 5


def check_data_freshness(df):
    if df is None or df.empty:
        return False

    last_timestamp = df.index[-1]

    now = pd.Timestamp.now(tz="UTC")

    age = (
        now - last_timestamp
    ).total_seconds()

    logger.info(
        f"Latest candle: {last_timestamp} | "
        f"Age: {age:.0f}s"
    )

    if age > STALE_AFTER_SECONDS:
        return False

    return True


# =============================================================================
# 8. INDICATORS
# =============================================================================

def calculate_true_range(df):

    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    previous_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - previous_close).abs()
    tr3 = (low - previous_close).abs()

    return pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)


def calculate_atr(df, period=ATR_PERIOD):

    tr = calculate_true_range(df)

    return tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


def calculate_adx_from_tr(df, tr, period=ADX_PERIOD):

    high = df["High"]
    low = df["Low"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = up_move.where(
        (up_move > down_move)
        & (up_move > 0),
        0.0
    )

    minus_dm = down_move.where(
        (down_move > up_move)
        & (down_move > 0),
        0.0
    )

    atr = tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    plus_dm_smoothed = plus_dm.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    minus_dm_smoothed = minus_dm.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    plus_di = (
        100
        * plus_dm_smoothed
        / atr.replace(0, np.nan)
    )

    minus_di = (
        100
        * minus_dm_smoothed
        / atr.replace(0, np.nan)
    )

    denominator = (
        plus_di + minus_di
    ).replace(0, np.nan)

    dx = (
        100
        * (plus_di - minus_di).abs()
        / denominator
    )

    adx = dx.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    return adx, plus_di, minus_di


def calculate_rsi(close, period=RSI_PERIOD):

    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    rs = (
        avg_gain
        / avg_loss.replace(0, np.nan)
    )

    return 100 - (
        100 / (1 + rs)
    )


def calculate_macd(
    close,
    fast=MACD_FAST,
    slow=MACD_SLOW,
    signal=MACD_SIGNAL
):

    ema_fast = close.ewm(
        span=fast,
        adjust=False
    ).mean()

    ema_slow = close.ewm(
        span=slow,
        adjust=False
    ).mean()

    macd = ema_fast - ema_slow

    signal_line = macd.ewm(
        span=signal,
        adjust=False
    ).mean()

    histogram = macd - signal_line

    return (
        macd,
        signal_line,
        histogram
    )


def calculate_stochastic(
    df,
    k_period=STOCH_K_PERIOD,
    d_period=STOCH_D_PERIOD
):

    lowest = (
        df["Low"]
        .rolling(k_period)
        .min()
    )

    highest = (
        df["High"]
        .rolling(k_period)
        .max()
    )

    denominator = (
        highest - lowest
    ).replace(0, np.nan)

    k = (
        100
        * (df["Close"] - lowest)
        / denominator
    )

    d = k.rolling(
        d_period
    ).mean()

    return k, d


def calculate_vwap(df):

    typical_price = (
        df["High"] + df["Low"] + df["Close"]
    ) / 3

    volume = (
        df["Volume"]
        .replace([np.inf, -np.inf, 0], np.nan)
    )

    temp = pd.DataFrame({
        "tp": typical_price,
        "volume": volume
    })

    temp["date"] = temp.index.date

    cum_volume = (
        temp.groupby("date")["volume"].cumsum()
    )

    pv = temp["tp"] * temp["volume"]

    cum_pv = pv.groupby(temp["date"]).cumsum()

    daily_total_volume = (
        temp.groupby("date")["volume"].transform("sum")
    )

    vwap = cum_pv / cum_volume.replace(0, np.nan)

    vwap = vwap.where(daily_total_volume > 0, np.nan)

    return vwap


def calculate_indicators(df):

    df = df.copy()

    close = df["Close"]

    df["EMA_9"] = close.ewm(
        span=EMA_FAST,
        adjust=False
    ).mean()

    df["EMA_21"] = close.ewm(
        span=EMA_SLOW,
        adjust=False
    ).mean()

    df["EMA_50"] = close.ewm(
        span=EMA_MID,
        adjust=False
    ).mean()

    df["EMA_200"] = close.ewm(
        span=EMA_TREND,
        adjust=False
    ).mean()

    df["RSI"] = calculate_rsi(
        close,
        RSI_PERIOD
    )

    tr = calculate_true_range(df)

    df["TR"] = tr

    df["ATR"] = calculate_atr(
        df,
        ATR_PERIOD
    )

    (
        df["ADX"],
        df["PLUS_DI"],
        df["MINUS_DI"]
    ) = calculate_adx_from_tr(
        df,
        tr,
        ADX_PERIOD
    )

    (
        df["MACD"],
        df["MACD_SIGNAL"],
        df["MACD_HIST"]
    ) = calculate_macd(close)

    (
        df["STOCH_K"],
        df["STOCH_D"]
    ) = calculate_stochastic(df)

    volume_ma = (
        df["Volume"]
        .replace(0, np.nan)
        .rolling(20)
        .mean()
    )

    df["VOLUME_RATIO"] = (
        df["Volume"]
        / volume_ma
    )

    df["VOLUME_RATIO"] = (
        df["VOLUME_RATIO"]
        .replace(
            [np.inf, -np.inf],
            np.nan
        )
    )

    if VWAP_ENABLED:
        df["VWAP"] = calculate_vwap(df)
    else:
        df["VWAP"] = np.nan

    critical = [
        "EMA_200",
        "RSI",
        "ATR",
        "ADX",
        "MACD_HIST",
        "STOCH_K",
        "STOCH_D"
    ]

    df = df.dropna(
        subset=critical
    ).copy()

    return df


# =============================================================================
# 9. SWING POINTS
# =============================================================================

def detect_confirmed_swings(
    df,
    left=SWING_LEFT,
    right=SWING_RIGHT
):

    if len(df) < left + right + 10:
        return [], []

    highs = []
    lows = []

    high_values = df["High"].values
    low_values = df["Low"].values

    for i in range(
        left,
        len(df) - right
    ):

        left_highs = high_values[
            i - left:i
        ]

        right_highs = high_values[
            i + 1:i + right + 1
        ]

        left_lows = low_values[
            i - left:i
        ]

        right_lows = low_values[
            i + 1:i + right + 1
        ]

        current_high = high_values[i]
        current_low = low_values[i]

        if (
            current_high > left_highs.max()
            and current_high >= right_highs.max()
        ):
            highs.append({
                "index": i,
                "price": float(current_high),
                "time": df.index[i]
            })

        if (
            current_low < left_lows.min()
            and current_low <= right_lows.min()
        ):
            lows.append({
                "index": i,
                "price": float(current_low),
                "time": df.index[i]
            })

    return highs, lows


# =============================================================================
# 10. MARKET STRUCTURE / BOS
# =============================================================================

def detect_bos(df):

    highs, lows = detect_confirmed_swings(df)

    result = {
        "direction": "NONE",
        "level": None,
        "swing_time": None
    }

    if not highs and not lows:
        return result

    current_close = float(
        df["Close"].iloc[-1]
    )

    latest_time = df.index[-1]

    bos_validity_window = pd.Timedelta(
        hours=BOS_VALIDITY_HOURS
    )

    if highs:

        last_high = highs[-1]

        if (
            last_high["time"]
            >= latest_time - bos_validity_window
            and current_close > last_high["price"]
        ):
            result = {
                "direction": "BULLISH_BOS",
                "level": last_high["price"],
                "swing_time": last_high["time"]
            }

    if lows:

        last_low = lows[-1]

        if (
            last_low["time"]
            >= latest_time - bos_validity_window
            and current_close < last_low["price"]
        ):
            bearish = {
                "direction": "BEARISH_BOS",
                "level": last_low["price"],
                "swing_time": last_low["time"]
            }

            if result["direction"] == "NONE":
                result = bearish

    return result


# =============================================================================
# 11. LIQUIDITY SWEEP
# =============================================================================

def detect_liquidity_sweep(
    df,
    lookback=SWEEP_LOOKBACK
):

    result = {
        "type": "NONE",
        "level": None,
        "penetration": 0.0,
        "strength": 0.0
    }

    if len(df) < lookback + 5:
        return result

    current = df.iloc[-1]

    previous = df.iloc[
        -lookback - 1:-1
    ]

    previous_high = float(
        previous["High"].max()
    )

    previous_low = float(
        previous["Low"].min()
    )

    atr = float(
        current["ATR"]
    )

    if not np.isfinite(atr) or atr <= 0:
        return result

    downside_penetration = (
        previous_low
        - float(current["Low"])
    )

    bullish_close_reclaim = (
        float(current["Close"])
        > previous_low
    )

    bullish_strength = (
        downside_penetration / atr
    )

    if (
        downside_penetration
        >= atr * SWEEP_ATR_MIN
        and bullish_close_reclaim
    ):

        result = {
            "type": "BULLISH_SWEEP",
            "level": previous_low,
            "penetration": downside_penetration,
            "strength": bullish_strength
        }

    upside_penetration = (
        float(current["High"])
        - previous_high
    )

    bearish_close_reject = (
        float(current["Close"])
        < previous_high
    )

    bearish_strength = (
        upside_penetration / atr
    )

    if (
        upside_penetration
        >= atr * SWEEP_ATR_MIN
        and bearish_close_reject
    ):

        bearish_result = {
            "type": "BEARISH_SWEEP",
            "level": previous_high,
            "penetration": upside_penetration,
            "strength": bearish_strength
        }

        if result["type"] == "NONE":
            result = bearish_result

    return result


# =============================================================================
# 12. FVG
# =============================================================================

def detect_fvg(df):

    none_result = {
        "type": "NONE",
        "low": None,
        "high": None,
        "index": None,
        "mitigated": True
    }

    if len(df) < 10:
        return none_result

    start = max(
        2,
        len(df) - FVG_LOOKBACK
    )

    candidates = []

    for i in range(
        start,
        len(df)
    ):

        c1 = df.iloc[i - 2]
        c3 = df.iloc[i]

        if (
            float(c3["Low"])
            > float(c1["High"])
        ):
            candidates.append({
                "type": "BULLISH_FVG",
                "low": float(c1["High"]),
                "high": float(c3["Low"]),
                "index": i
            })

        if (
            float(c3["High"])
            < float(c1["Low"])
        ):
            candidates.append({
                "type": "BEARISH_FVG",
                "low": float(c3["High"]),
                "high": float(c1["Low"]),
                "index": i
            })

    if not candidates:
        return none_result

    for candidate in reversed(candidates):

        start_idx = candidate["index"] + 1

        if start_idx >= len(df):
            continue

        future = df.iloc[
            start_idx:
        ]

        if candidate["type"] == "BULLISH_FVG":

            touched = (
                future["Low"]
                <= candidate["high"]
            ) & (
                future["High"]
                >= candidate["low"]
            )

        else:

            touched = (
                future["High"]
                >= candidate["low"]
            ) & (
                future["Low"]
                <= candidate["high"]
            )

        if not touched.any():

            candidate["mitigated"] = False

            return candidate

    return none_result


# =============================================================================
# 13. FIBONACCI CONTEXT
# =============================================================================

def fibonacci_context(df):

    unknown = {
        "bias": "UNKNOWN",
        "retracement": None,
        "swing_high": None,
        "swing_low": None
    }

    if len(df) < 50:
        return unknown

    highs, lows = detect_confirmed_swings(df)

    if not highs or not lows:
        return unknown

    last_high = highs[-1]
    last_low = lows[-1]

    high_price = last_high["price"]
    low_price = last_low["price"]

    current = float(
        df["Close"].iloc[-1]
    )

    if high_price <= low_price:
        return {
            "bias": "UNKNOWN",
            "retracement": None,
            "swing_high": high_price,
            "swing_low": low_price
        }

    range_size = (
        high_price - low_price
    )

    retracement = (
        high_price - current
    ) / range_size * 100

    if current > high_price:
        bias = "ABOVE_SWING_HIGH"

    elif current < low_price:
        bias = "BELOW_SWING_LOW"

    else:
        if last_high["index"] > last_low["index"]:
            if retracement <= 38.2:
                bias = "PREMIUM"
            elif retracement >= 61.8:
                bias = "DISCOUNT"
            else:
                bias = "MID_RANGE"
        else:
            if retracement <= 38.2:
                bias = "DISCOUNT"
            elif retracement >= 61.8:
                bias = "PREMIUM"
            else:
                bias = "MID_RANGE"

    return {
        "bias": bias,
        "retracement": retracement,
        "swing_high": high_price,
        "swing_low": low_price
    }


# =============================================================================
# 14. SUPPORT / RESISTANCE CLUSTERING
# =============================================================================

def cluster_levels(
    levels,
    tolerance
):

    if not levels:
        return []

    sorted_levels = sorted(
        [float(x) for x in levels]
    )

    clusters = []

    current_cluster = [
        sorted_levels[0]
    ]

    for level in sorted_levels[1:]:

        cluster_mean = np.mean(
            current_cluster
        )

        if (
            abs(level - cluster_mean)
            <= tolerance
        ):
            current_cluster.append(level)

        else:
            clusters.append(
                current_cluster
            )

            current_cluster = [
                level
            ]

    clusters.append(
        current_cluster
    )

    result = []

    for cluster in clusters:

        result.append({
            "level": float(
                np.mean(cluster)
            ),
            "touches": len(cluster)
        })

    return result


def historical_levels(df):

    empty = {
        "support": None,
        "resistance": None,
        "supports": [],
        "resistances": []
    }

    if len(df) < 50:
        return empty

    sample = df.tail(
        min(SR_LOOKBACK, len(df))
    )

    atr = float(
        sample["ATR"].iloc[-1]
    )

    if not np.isfinite(atr) or atr <= 0:
        return empty

    tolerance = atr * SR_CLUSTER_ATR

    highs, lows = detect_confirmed_swings(
        sample
    )

    high_levels = [
        x["price"]
        for x in highs
    ]

    low_levels = [
        x["price"]
        for x in lows
    ]

    resistance_clusters = cluster_levels(
        high_levels,
        tolerance
    )

    support_clusters = cluster_levels(
        low_levels,
        tolerance
    )

    price = float(
        sample["Close"].iloc[-1]
    )

    supports = [
        x for x in support_clusters
        if x["level"] < price
    ]

    resistances = [
        x for x in resistance_clusters
        if x["level"] > price
    ]

    supports = sorted(
        supports,
        key=lambda x: price - x["level"]
    )

    resistances = sorted(
        resistances,
        key=lambda x: x["level"] - price
    )

    nearest_support = (
        supports[0]["level"]
        if supports
        else None
    )

    nearest_resistance = (
        resistances[0]["level"]
        if resistances
        else None
    )

    return {
        "support": nearest_support,
        "resistance": nearest_resistance,
        "supports": supports[:5],
        "resistances": resistances[:5]
    }


# =============================================================================
# 15. H1 CONTEXT
# =============================================================================

def get_h1_context():

    now = time.time()

    if (
        _h1_cache["direction"] is not None
        and now - _h1_cache["timestamp"] < H1_CACHE_TTL_SECONDS
    ):
        return _h1_cache["direction"]

    try:

        h1 = download_market_data(
            symbol=SYMBOL,
            interval="1h",
            period=H1_PERIOD
        )

        direction = "UNKNOWN"

        if h1 is not None and len(h1) >= H1_MIN_BARS:

            h1, _ = get_closed_dataframe(h1)

            h1 = calculate_indicators(h1)

            if h1 is not None and len(h1) >= 50:

                c = h1.iloc[-1]

                bullish = (
                    c["Close"] > c["EMA_200"]
                    and c["EMA_9"] > c["EMA_21"]
                    and c["EMA_21"] > c["EMA_50"]
                )

                bearish = (
                    c["Close"] < c["EMA_200"]
                    and c["EMA_9"] < c["EMA_21"]
                    and c["EMA_21"] < c["EMA_50"]
                )

                if bullish:
                    direction = "BULLISH"
                elif bearish:
                    direction = "BEARISH"
                else:
                    direction = "NEUTRAL"

        _h1_cache["direction"] = direction
        _h1_cache["timestamp"] = now

        return direction

    except Exception as e:
        _h1_cache["direction"] = "UNKNOWN"
        _h1_cache["timestamp"] = (
            now - H1_CACHE_TTL_SECONDS + 600
        )
        return "UNKNOWN"


# =============================================================================
# 16. TECHNICAL ANALYSIS
# =============================================================================

def analyze_market(df, h1_direction):

    if df is None:
        return None

    if len(df) < MIN_ANALYSIS_BARS:
        return None

    c = df.iloc[-1]
    p = df.iloc[-2]

    price = float(c["Close"])
    atr = float(c["ATR"])

    if not np.isfinite(atr) or atr <= 0:
        return None

    vwap_available = bool(
        VWAP_ENABLED
        and np.isfinite(c["VWAP"])
    )

    volume_available = bool(
        np.isfinite(c["VOLUME_RATIO"])
    )

    h1_known = h1_direction in ("BULLISH", "BEARISH")

    bullish_conditions = {
        "price_above_ema200":
            price > float(c["EMA_200"]),

        "ema_alignment":
            float(c["EMA_9"]) > float(c["EMA_21"]) > float(c["EMA_50"]),

        "rsi_bullish":
            50 <= float(c["RSI"]) <= 70,

        "macd_momentum":
            float(c["MACD_HIST"]) > float(p["MACD_HIST"]),

        "adx_strength":
            float(c["ADX"]) >= ADX_MIN,

        "di_bullish":
            float(c["PLUS_DI"]) > float(c["MINUS_DI"]),

        "stoch_bullish":
            float(c["STOCH_K"]) > float(c["STOCH_D"]),

        "vwap_bullish": (
            price > float(c["VWAP"])
            if vwap_available
            else None
        ),

        "h1_bullish": (
            h1_direction == "BULLISH"
            if h1_known
            else None
        ),

        "volume_confirmation": (
            float(c["VOLUME_RATIO"]) >= 1.0
            if volume_available
            else None
        ),
    }

    bearish_conditions = {
        "price_below_ema200":
            price < float(c["EMA_200"]),

        "ema_alignment":
            float(c["EMA_9"]) < float(c["EMA_21"]) < float(c["EMA_50"]),

        "rsi_bearish":
            30 <= float(c["RSI"]) <= 50,

        "macd_momentum":
            float(c["MACD_HIST"]) < float(p["MACD_HIST"]),

        "adx_strength":
            float(c["ADX"]) >= ADX_MIN,

        "di_bearish":
            float(c["MINUS_DI"]) > float(c["PLUS_DI"]),

        "stoch_bearish":
            float(c["STOCH_K"]) < float(c["STOCH_D"]),

        "vwap_bearish": (
            price < float(c["VWAP"])
            if vwap_available
            else None
        ),

        "h1_bearish": (
            h1_direction == "BEARISH"
            if h1_known
            else None
        ),

        "volume_confirmation": (
            float(c["VOLUME_RATIO"]) >= 1.0
            if volume_available
            else None
        ),
    }

    def score_conditions(conditions):
        return sum(
            v for v in conditions.values()
            if v is not None
        )

    def active_max_conditions(conditions):
        return sum(
            1 for v in conditions.values()
            if v is not None
        )

    bullish_score = score_conditions(bullish_conditions)
    bearish_score = score_conditions(bearish_conditions)

    bos = detect_bos(df)
    sweep = detect_liquidity_sweep(df)
    fvg = detect_fvg(df)
    fib = fibonacci_context(df)
    sr = historical_levels(df)

    bullish_bonus = 0
    bearish_bonus = 0

    if bos["direction"] == "BULLISH_BOS":
        bullish_bonus += 2
    if bos["direction"] == "BEARISH_BOS":
        bearish_bonus += 2

    if sweep["type"] == "BULLISH_SWEEP":
        bullish_bonus += 2
    if sweep["type"] == "BEARISH_SWEEP":
        bearish_bonus += 2

    if fvg["type"] == "BULLISH_FVG":
        bullish_bonus += 1
    if fvg["type"] == "BEARISH_FVG":
        bearish_bonus += 1

    if fib["bias"] == "DISCOUNT":
        bullish_bonus += 1
    if fib["bias"] == "PREMIUM":
        bearish_bonus += 1

    base_max_score = max(
        active_max_conditions(bullish_conditions),
        active_max_conditions(bearish_conditions)
    )

    structural_max = 6
    max_score = base_max_score + structural_max

    bullish_total = bullish_score + bullish_bonus
    bearish_total = bearish_score + bearish_bonus

    bullish_confidence = bullish_total / max_score * 100
    bearish_confidence = bearish_total / max_score * 100

    min_required = max_score * MIN_SCORE_RATIO

    direction = "WAIT"
    score = 0
    confidence = 0

    if bullish_total >= min_required and bullish_total > bearish_total:
        direction = "BUY"
        score = bullish_total
        confidence = bullish_confidence
    elif bearish_total >= min_required and bearish_total > bullish_total:
        direction = "SELL"
        score = bearish_total
        confidence = bearish_confidence

    sl = None
    tp = None

    if direction == "BUY":
        sl = price - (atr * ATR_SL_MULT)
        tp = price + (atr * ATR_TP_MULT)
    elif direction == "SELL":
        sl = price + (atr * ATR_SL_MULT)
        tp = price - (atr * ATR_TP_MULT)

    adx_value = float(c["ADX"])
    atr_pct = atr / price * 100

    if adx_value < 18:
        market_condition = "RANGE"
    elif atr_pct > 0.35:
        market_condition = "HIGH_VOLATILITY"
    elif adx_value >= ADX_MIN:
        market_condition = "TREND"
    else:
        market_condition = "UNCLEAR"

    return {
        "timestamp": df.index[-1],
        "direction": direction,
        "price": price,
        "sl": sl,
        "tp": tp,
        "score": int(score),
        "max_score": int(max_score),
        "confidence": float(confidence),
        "bullish_score": int(bullish_total),
        "bearish_score": int(bearish_total),
        "rsi": float(c["RSI"]),
        "adx": float(c["ADX"]),
        "plus_di": float(c["PLUS_DI"]),
        "minus_di": float(c["MINUS_DI"]),
        "macd_hist": float(c["MACD_HIST"]),
        "stoch_k": float(c["STOCH_K"]),
        "stoch_d": float(c["STOCH_D"]),
        "atr": atr,
        "ema9": float(c["EMA_9"]),
        "ema21": float(c["EMA_21"]),
        "ema50": float(c["EMA_50"]),
        "ema200": float(c["EMA_200"]),
        "vwap": float(c["VWAP"]) if np.isfinite(c["VWAP"]) else None,
        "volume_ratio": float(c["VOLUME_RATIO"]) if np.isfinite(c["VOLUME_RATIO"]) else None,
        "h1": h1_direction,
        "bos": bos,
        "sweep": sweep,
        "fvg": fvg,
        "fib": fib,
        "sr": sr,
        "market_condition": market_condition,
        "bullish_conditions": bullish_conditions,
        "bearish_conditions": bearish_conditions,
    }


# =============================================================================
# 17. ALERT KEY / DUPLICATE PROTECTION
# =============================================================================

def create_alert_key(result):
    timestamp = str(result["timestamp"])
    return f"{timestamp}|{result['direction']}"


def should_send_alert(result):
    if result is None:
        return False

    if result["direction"] not in ("BUY", "SELL"):
        return False

    key = create_alert_key(result)
    now = time.time()

    last_key = STATE.get("last_alert_key")
    last_timestamp = STATE.get("last_alert_timestamp")

    if key == last_key:
        return False

    if last_timestamp is not None:
        elapsed = now - float(last_timestamp)
        if elapsed < ALERT_COOLDOWN_SECONDS:
            return False

    return True


# =============================================================================
# 18. FORMAT ALERT
# =============================================================================

def format_alert(result):
    direction = result["direction"]

    if direction == "BUY":
        title = "🟢 XAUUSD V21 BUY ALERT"
    else:
        title = "🔴 XAUUSD V21 SELL ALERT"

    lines = [
        title,
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Entry: `{result['price']:.2f}`",
        f"SL: `{result['sl']:.2f}`",
        f"TP: `{result['tp']:.2f}`",
        "",
        f"Score: `{result['score']}/{result['max_score']}`",
        f"Confidence: `{result['confidence']:.1f}%`",
        "",
        f"RSI: `{result['rsi']:.1f}` | ADX: `{result['adx']:.1f}`",
        f"DI+: `{result['plus_di']:.1f}` | DI-: `{result['minus_di']:.1f}`",
        f"MACD Hist: `{result['macd_hist']:.4f}`",
        f"Stoch: `{result['stoch_k']:.1f}/{result['stoch_d']:.1f}`",
        "",
        f"H1: `{result['h1']}`",
        f"Market: `{result['market_condition']}`",
        "",
    ]

    bos = result["bos"]
    lines.append(f"BOS: `{bos['direction']}`")
    if bos["level"] is not None:
        lines.append(f"BOS Level: `{bos['level']:.2f}`")

    sweep = result["sweep"]
    lines.append(f"Liquidity: `{sweep['type']}`")
    if sweep["level"] is not None:
        lines.append(f"Sweep Level: `{sweep['level']:.2f}`")

    fvg = result["fvg"]
    lines.append(f"FVG: `{fvg['type']}`")
    if fvg["low"] is not None and fvg["high"] is not None:
        lines.append(f"FVG Zone: `{fvg['low']:.2f} - {fvg['high']:.2f}`")

    fib = result["fib"]
    lines.extend(["", f"Fibonacci: `{fib['bias']}`"])
    if fib["retracement"] is not None:
        lines.append(f"Retracement: `{fib['retracement']:.1f}%`")

    sr = result["sr"]
    if sr["support"] is not None:
        lines.append(f"Support: `{sr['support']:.2f}`")
    if sr["resistance"] is not None:
        lines.append(f"Resistance: `{sr['resistance']:.2f}`")

    if result["vwap"] is not None:
        lines.append(f"VWAP: `{result['vwap']:.2f}`")

    if result["volume_ratio"] is not None:
        lines.append(f"Volume Ratio: `{result['volume_ratio']:.2f}x`")
    else:
        lines.append("Volume Ratio: `N/A`")

    lines.extend([
        "",
        f"Candle: `{result['timestamp']}`",
        "",
        "⚠️ Research / Paper Alert Only",
        "No real order executed."
    ])

    return "\n".join(lines)


# =============================================================================
# 19. SAVE SIGNAL
# =============================================================================

SIGNAL_COLUMNS = [
    "timestamp", "direction", "price", "sl", "tp",
    "score", "max_score", "confidence",
    "bullish_score", "bearish_score",
    "RSI", "ADX", "DI_plus", "DI_minus",
    "MACD_HIST", "STOCH_K", "STOCH_D", "ATR",
    "EMA_9", "EMA_21", "EMA_50", "EMA_200",
    "VWAP", "VOLUME_RATIO",
    "H1", "BOS", "SWEEP", "FVG", "FIB",
    "MARKET", "telegram_success",
]


def save_signal(result, telegram_success=False):
    row = {
        "timestamp": result["timestamp"],
        "direction": result["direction"],
        "price": result["price"],
        "sl": result["sl"],
        "tp": result["tp"],
        "score": result["score"],
        "max_score": result["max_score"],
        "confidence": result["confidence"],
        "bullish_score": result["bullish_score"],
        "bearish_score": result["bearish_score"],
        "RSI": result["rsi"],
        "ADX": result["adx"],
        "DI_plus": result["plus_di"],
        "DI_minus": result["minus_di"],
        "MACD_HIST": result["macd_hist"],
        "STOCH_K": result["stoch_k"],
        "STOCH_D": result["stoch_d"],
        "ATR": result["atr"],
        "EMA_9": result["ema9"],
        "EMA_21": result["ema21"],
        "EMA_50": result["ema50"],
        "EMA_200": result["ema200"],
        "VWAP": result["vwap"],
        "VOLUME_RATIO": result["volume_ratio"],
        "H1": result["h1"],
        "BOS": result["bos"]["direction"],
        "SWEEP": result["sweep"]["type"],
        "FVG": result["fvg"]["type"],
        "FIB": result["fib"]["bias"],
        "MARKET": result["market_condition"],
        "telegram_success": telegram_success,
    }

    row_df = pd.DataFrame([row])
    row_df = row_df[SIGNAL_COLUMNS]

    file_exists = os.path.exists(SIGNALS_FILE)
    header_needed = True

    if file_exists:
        try:
            with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
            existing_cols = [col.strip() for col in first_line.split(",")]
            header_needed = existing_cols != SIGNAL_COLUMNS
        except Exception:
            header_needed = False

    row_df.to_csv(
        SIGNALS_FILE,
        mode="a",
        header=header_needed,
        index=False
    )


# =============================================================================
# 20. UPDATE ALERT STATE
# =============================================================================

def mark_alert_sent(result):
    STATE["last_alert_key"] = create_alert_key(result)
    STATE["last_alert_timestamp"] = time.time()
    STATE["total_alerts"] += 1

    if result["direction"] == "BUY":
        STATE["buy_alerts"] += 1
    elif result["direction"] == "SELL":
        STATE["sell_alerts"] += 1

    save_state(STATE)


# =============================================================================
# 21. PERIODIC REPORT
# =============================================================================

def periodic_report_due():
    last_report = STATE.get("last_periodic_report")
    if last_report is None:
        return True

    elapsed = time.time() - float(last_report)
    return elapsed >= PERIODIC_REPORT_HOURS * 3600


def send_periodic_report():
    if not telegram_configured():
        return False

    message = (
        "📊 *XAUUSD V21 STATUS*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Total alerts: `{STATE['total_alerts']}`\n"
        f"BUY alerts: `{STATE['buy_alerts']}`\n"
        f"SELL alerts: `{STATE['sell_alerts']}`\n\n"
        "Engine: `ONLINE`\n"
        "Mode: `RESEARCH / MONITORING`\n"
        "Execution: `NONE`\n"
    )

    ok = send_telegram(message)

    if ok:
        STATE["last_periodic_report"] = time.time()
        save_state(STATE)

    return ok


# =============================================================================
# 22. MARKET SUMMARY
# =============================================================================

def print_market_summary(result):
    logger.info("--------------------------------------------------")
    logger.info(f"PRICE      : {result['price']:.2f}")
    logger.info(f"DIRECTION  : {result['direction']}")
    logger.info(f"SCORE      : {result['score']}/{result['max_score']}")
    logger.info(f"CONFIDENCE : {result['confidence']:.1f}%")
    logger.info(f"H1         : {result['h1']}")
    logger.info(f"RSI        : {result['rsi']:.1f}")
    logger.info(f"ADX        : {result['adx']:.1f}")
    logger.info(f"MACD HIST  : {result['macd_hist']:.4f}")
    logger.info(f"BOS        : {result['bos']['direction']}")
    logger.info(f"LIQUIDITY  : {result['sweep']['type']}")
    logger.info(f"FVG        : {result['fvg']['type']}")
    logger.info(f"FIB        : {result['fib']['bias']}")
    logger.info(f"MARKET     : {result['market_condition']}")
    logger.info("--------------------------------------------------")


# =============================================================================
# 23. PROCESS MARKET
# =============================================================================

def process_market():
    try:
        if is_weekend():
            logger.info("Weekend detected. Market monitoring paused.")
            return

        raw_df = download_market_data()

        if not check_data_freshness(raw_df):
            return

        closed_df, closed_timestamp = get_closed_dataframe(raw_df)

        if closed_df is None or closed_timestamp is None:
            return

        last_processed = STATE.get("last_processed_candle")
        closed_key = str(closed_timestamp)

        if last_processed == closed_key:
            if periodic_report_due():
                send_periodic_report()
            return

        df = calculate_indicators(closed_df)

        if df is None:
            return

        if len(df) < MIN_ANALYSIS_BARS:
            return

        h1_direction = get_h1_context()
        result = analyze_market(df, h1_direction)

        if result is None:
            return

        print_market_summary(result)

        STATE["last_processed_candle"] = closed_key
        save_state(STATE)

        if result["direction"] in ("BUY", "SELL"):
            if should_send_alert(result):
                message = format_alert(result)
                telegram_ok = send_telegram(message)
                save_signal(result, telegram_success=telegram_ok)

                if telegram_ok:
                    mark_alert_sent(result)

        if periodic_report_due():
            send_periodic_report()

    except Exception as e:
        logger.exception(f"process_market error: {e}")


# =============================================================================
# 24. MAIN MONITOR LOOP
# =============================================================================

def run_monitor():
    logger.info("==================================================")
    logger.info("XAUUSD V21 RESEARCH MONITOR")
    logger.info("==================================================")
    logger.info(f"Symbol       : {SYMBOL}")
    logger.info(f"Timeframe    : {TIMEFRAME}")
    logger.info(f"Download     : {DOWNLOAD_PERIOD}")
    logger.info("Execution    : DISABLED")
    logger.info("Mode         : PAPER / RESEARCH ONLY")
    logger.info("==================================================")

    test_telegram()

    while True: # syntax check below
