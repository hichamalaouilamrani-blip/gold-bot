import time
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime

# ==========================================
# 1. INSTITUTIONAL CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = "8753393752:AAEuCegNTl36ME_rzlxgm33N7EMLnfzJxMI"
TELEGRAM_CHAT_ID = "5535955736"

SYMBOL = "GC=F"           # Data feed ticker
ACCOUNT_BALANCE = 1000    # Balance in USD
RISK_PERCENT = 0.01        # 1% Risk per trade

# متغيرة لمنع تكرار نفس التنبيه لنفس الشمعة المغلقة
LAST_PROCESSED_CANDLE = None

def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

# ==========================================
# 2. ACCURATE SPOT PRICE & INDICATORS
# ==========================================
def get_spot_gold_price():
    """ جلب السعر الفوري الحقيقي والواقعي للذهب """
    try:
        ticker = yf.Ticker("XAUUSD=X")
        data = ticker.history(period="1d", interval="1m")
        if not data.empty:
            return round(float(data['Close'].iloc[-1]), 2)
    except Exception:
        pass
    
    ticker_alt = yf.Ticker("GC=F")
    data_alt = ticker_alt.history(period="1d", interval="1m")
    return round(float(data_alt['Close'].iloc[-1]), 2)

def fetch_data(symbol, interval, period="5d"):
    data = yf.download(tickers=symbol, period=period, interval=interval, progress=False)
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    return data

def calculate_institutional_indicators(df):
    df = df.copy()
    
    # 1. EMAs Exponential Moving Averages
    df['EMA_9'] = df['Close'].ewm(span=9, adjust=False).mean()
    df['EMA_21'] = df['Close'].ewm(span=21, adjust=False).mean()
    df['EMA_50'] = df['Close'].ewm(span=50, adjust=False).mean()
    
    # 2. RSI (Relative Strength Index)
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    df['RSI'] = 100 - (100 / (1 + (gain / loss)))
    
    # 3. ATR (Average True Range)
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    df['ATR'] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(14).mean()

    # 4. ADX (Average Directional Index)
    df['+DM'] = np.where((df['High'] - df['High'].shift(1)) > (df['Low'].shift(1) - df['Low']), 
                         np.maximum(df['High'] - df['High'].shift(1), 0), 0)
    df['-DM'] = np.where((df['Low'].shift(1) - df['Low']) > (df['High'] - df['High'].shift(1)), 
                         np.maximum(df['Low'].shift(1) - df['Low'], 0), 0)
    
    tr = df['ATR'] * 14
    plus_di = 100 * (df['+DM'].ewm(alpha=1/14).mean() / tr)
    minus_di = 100 * (df['-DM'].ewm(alpha=1/14).mean() / tr)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
    df['ADX'] = dx.ewm(alpha=1/14).mean()
    
    return df

def get_h1_trend():
    df_h1 = fetch_data(SYMBOL, interval="1h", period="7d")
    df_h1 = calculate_institutional_indicators(df_h1)
    last = df_h1.iloc[-1]
    
    if last['EMA_9'] > last['EMA_21'] and last['Close'] > last['EMA_50']:
        return "BULLISH 📈"
    elif last['EMA_9'] < last['EMA_21'] and last['Close'] < last['EMA_50']:
        return "BEARISH 📉"
    return "NEUTRAL ⚖️"

def calculate_lot_size(entry_price, stop_loss):
    risk_amount = ACCOUNT_BALANCE * RISK_PERCENT
    price_risk = abs(entry_price - stop_loss)
    if price_risk == 0:
        return 0.01
    lot_size = round(risk_amount / (price_risk * 100), 2)
    return max(0.01, lot_size)

# ==========================================
# 3. CORE ENGINE & ALERT LOGIC
# ==========================================
def run_alert_system():
    global LAST_PROCESSED_CANDLE
    
    df_m15 = fetch_data(SYMBOL, interval="15m", period="3d")
    df_m15 = calculate_institutional_indicators(df_m15)
    
    # الاعتماد على الشمعة المغلقة أخيرًا [-2] لتجنب الإشارات الزائفة أثناء التداول
    closed_candle = df_m15.iloc[-2]
    prev_closed_candle = df_m15.iloc[-3]
    candle_time = df_m15.index[-2]
    
    entry_price = get_spot_gold_price()
    atr = float(closed_candle['ATR'])
    adx = float(closed_candle['ADX'])
    h1_trend = get_h1_trend()
    
    buy_score, sell_score = 0, 0
    
    if closed_candle['EMA_9'] > closed_candle['EMA_21']: buy_score += 1
    if closed_candle['EMA_9'] < closed_candle['EMA_21']: sell_score += 1
    
    if closed_candle['Close'] > closed_candle['EMA_50']: buy_score += 1
    if closed_candle['Close'] < closed_candle['EMA_50']: sell_score += 1
    
    if 50 < closed_candle['RSI'] < 70: buy_score += 1
    if 30 < closed_candle['RSI'] < 50: sell_score += 1
    
    if closed_candle['Close'] > prev_closed_candle['Close']: buy_score += 1
    if closed_candle['Close'] < prev_closed_candle['Close']: sell_score += 1
    
    # التأكد من إرسال التنبيه مرة واحدة فقط للشمعة المغلقة
    if LAST_PROCESSED_CANDLE != candle_time:
        if buy_score >= 3 and "BULLISH" in h1_trend and adx > 15:
            LAST_PROCESSED_CANDLE = candle_time
            
            stop_loss = round(entry_price - (atr * 1.5), 2)
            tp1 = round(entry_price + (atr * 1.5), 2)
            tp2 = round(entry_price + (atr * 3.0), 2)
            lot = calculate_lot_size(entry_price, stop_loss)
            
            msg = f"""🚨 <b>INSTITUTIONAL SIGNAL ALERT</b> 🚨

Direction: BUY 🟢
Entry Price: ${entry_price:.2f}
Stop Loss: ${stop_loss:.2f}
Take Profit: ${tp2:.2f}

🎯 <b>Targets:</b>
• <b>TP1 (50% & Move SL to BE):</b> <code>${tp1:.2f}</code>
• <b>TP2 (Final Target):</b> <code>${tp2:.2f}</code>

📊 <b>Position Management:</b>
• <b>Rec. Lot Size:</b> <code>{lot} Lots</code> (1% Risk)
• <b>Score:</b> {buy_score}/4
• <b>H1 Trend Filter:</b> {h1_trend}
• <b>Trend Strength (ADX):</b> {adx:.1f}
• <b>Closed Candle Time:</b> {candle_time}"""
            
            send_telegram_msg(msg)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] BUY Institutional Signal Sent!")

        elif sell_score >= 3 and "BEARISH" in h1_trend and adx > 15:
            LAST_PROCESSED_CANDLE = candle_time
            
            stop_loss = round(entry_price + (atr * 1.5), 2)
            tp1 = round(entry_price - (atr * 1.5), 2)
            tp2 = round(entry_price - (atr * 3.0), 2)
            lot = calculate_lot_size(entry_price, stop_loss)
            
            msg = f"""🚨 <b>INSTITUTIONAL SIGNAL ALERT</b> 🚨

Direction: SELL 🔴
Entry Price: ${entry_price:.2f}
Stop Loss: ${stop_loss:.2f}
Take Profit: ${tp2:.2f}

🎯 <b>Targets:</b>
• <b>TP1 (50% & Move SL to BE):</b> <code>${tp1:.2f}</code>
• <b>TP2 (Final Target):</b> <code>${tp2:.2f}</code>

📊 <b>Position Management:</b>
• <b>Rec. Lot Size:</b> <code>{lot} Lots</code> (1% Risk)
• <b>Score:</b> {sell_score}/4
• <b>H1 Trend Filter:</b> {h1_trend}
• <b>Trend Strength (ADX):</b> {adx:.1f}
• <b>Closed Candle Time:</b> {candle_time}"""
            
            send_telegram_msg(msg)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] SELL Institutional Signal Sent!")
            
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Spot Price: ${entry_price:.2f} | ADX: {adx:.1f} | Scanning...")

# ==========================================
# 4. SYSTEM EXECUTION LOOP
# ==========================================
print("=== Institutional Grade Gold System Active ===")
send_telegram_msg("🔔 <b>24/7 Cloud Bot Initialized & Live Spot Gold Synced!</b>")

while True:
    try:
        run_alert_system()
    except Exception as e:
        print(f"Execution Error: {e}")
    time.sleep(300)
