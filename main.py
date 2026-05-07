import yfinance as yf
import pandas as pd
import time
import requests
import os
import csv
from datetime import datetime

# ===== TELEGRAM =====
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.get(url, params={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        print("Errore Telegram:", e)

# ===== PARAMETRI =====
CAPITALE = 1000
RISCHIO = 0.01

# ===== TICKERS =====
with open("tickers.txt") as f:
    TICKERS = [line.strip() for line in f if line.strip()]

MAX_TICKERS = 20
index = 0

active_trades = {}
equity = CAPITALE

# ===== CSV =====
with open("trades.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["time","ticker","type","result","equity"])

# ===== ATR =====
def compute_atr(data, period=14):
    tr = pd.concat([
        data["High"] - data["Low"],
        (data["High"] - data["Close"].shift()).abs(),
        (data["Low"] - data["Close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# ===== INDICATORI =====
def compute_indicators(df):
    df["EMA50"] = df["Close"].ewm(span=50).mean()
    df["EMA200"] = df["Close"].ewm(span=200).mean()

    delta = df["Close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df["RSI"] = 100 - (100 / (1 + rs))

    ema12 = df["Close"].ewm(span=12).mean()
    ema26 = df["Close"].ewm(span=26).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_signal"] = df["MACD"].ewm(span=9).mean()
    return df

print("🚀 BOT AVVIATO")
send_telegram("🚀 BOT ONLINE")

# ===== LOOP =====
while True:
    try:
        # ===== FILTRO ORARIO (IT) =====
        import pytz

        ny = pytz.timezone("America/New_York")
        ora = datetime.now(ny).hour

        # pausa solo notte USA
        if 2 <= ora < 8:
            print("😴 Notte USA - pausa")
            time.sleep(300)
            continue

        # ===== FASE =====
        if ora < 10:
            fase = "Pre-market"
        elif ora < 22:
            fase = "Market"
        else:
            fase = "After-hours"
        print(f"🕒 Fase: {fase}")

        subset = TICKERS[index:index+MAX_TICKERS]

        # ===== LOOP TICKERS =====
        for ticker in subset:
            # --- 5m ---
            df = yf.download(ticker, period="5d", interval="5m", progress=False)
            if df is None or df.empty or len(df) < 50:
                continue

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            df = df[["Open","High","Low","Close","Volume"]].dropna()
            df["ATR"] = compute_atr(df)
            df = compute_indicators(df)

            last = df.iloc[-1]
            price = last["Close"]
            atr = last["ATR"]
            if pd.isna(atr) or atr == 0:
                continue

            # --- 1H (HTF) ---
            df_htf = yf.download(ticker, period="1mo", interval="1h", progress=False)
            if df_htf is None or df_htf.empty or len(df_htf) < 50:
                continue

            if isinstance(df_htf.columns, pd.MultiIndex):
                df_htf.columns = df_htf.columns.get_level_values(0)

            df_htf = df_htf[["Open","High","Low","Close","Volume"]].dropna()
            df_htf = compute_indicators(df_htf)
            htf_last = df_htf.iloc[-1]

            # --- TOP MOVER ---
            if len(df) < 20:
                continue
            move_perc = (df["Close"].iloc[-1] - df["Close"].iloc[-20]) / price
            if abs(move_perc) < 0.01:
                continue
            volume_avg = df["Volume"].rolling(20).mean()
            if df["Volume"].iloc[-1] < volume_avg.iloc[-1]:
                continue

            # --- CONDIZIONI ---
            trend_up = price > last["EMA200"]
            trend_down = price < last["EMA200"]
            htf_up = htf_last["Close"] > htf_last["EMA200"]
            htf_down = htf_last["Close"] < htf_last["EMA200"]

            rsi_buy = last["RSI"] > 50
            rsi_sell = last["RSI"] < 50
            macd_buy = last["MACD"] > last["MACD_signal"]
            macd_sell = last["MACD"] < last["MACD_signal"]

            golden_cross = (
                df["EMA50"].iloc[-2] < df["EMA200"].iloc[-2]
                and df["EMA50"].iloc[-1] > df["EMA200"].iloc[-1]
            )
            death_cross = (
                df["EMA50"].iloc[-2] > df["EMA200"].iloc[-2]
                and df["EMA50"].iloc[-1] < df["EMA200"].iloc[-1]
            )

            score_buy = 0
            score_sell = 0
            if trend_up: score_buy += 1
            if trend_down: score_sell += 1
            if rsi_buy: score_buy += 1
            if rsi_sell: score_sell += 1
            if macd_buy: score_buy += 1
            if macd_sell: score_sell += 1
            if golden_cross: score_buy += 2
            if death_cross: score_sell += 2

            # --- ENTRY ---
            if ticker in active_trades:
                continue

            if score_buy >= 3 and htf_up:
                side = "BUY"; score = score_buy
            elif score_sell >= 3 and htf_down:
                side = "SELL"; score = score_sell
            else:
                continue

            distanza_stop = atr * 4
            stop = price - distanza_stop if side == "BUY" else price + distanza_stop
            target = price + atr * 6 if side == "BUY" else price - atr * 6

            risk = abs(price - stop)
            reward = abs(target - price)
            rr = round(reward / risk, 2) if risk != 0 else 0
            if rr < 2:
                continue

            rischio_euro = CAPITALE * RISCHIO
            qty = int(min(rischio_euro / distanza_stop, CAPITALE / price))
            if qty <= 0:
                continue

            COMMISSIONI = 24
            profitto_potenziale = reward * qty
            if profitto_potenziale < COMMISSIONI * 2:
                continue

            active_trades[ticker] = {
                "side": side,
                "entry": price,
                "stop": stop,
                "target": target,
                "qty": qty,
                "risk": rischio_euro,
            }

            send_telegram(
                f"🚀 {side} {ticker} @ {round(price,2)}\n"
                f"RSI: {round(last['RSI'],1)}\n"
                f"GoldenCross: {'YES' if golden_cross else 'NO'}\n"
                f"Score: {score}\n"
                f"🛑 Stop: {round(stop,2)}\n"
                f"🎯 Target: {round(target,2)}\n"
                f"⚖️ R/R: {rr}"
            )

        print(f"💰 Equity: {round(equity,2)}€ | Attivi: {len(active_trades)}")

        index += MAX_TICKERS
        if index >= len(TICKERS):
            index = 0

        time.sleep(60)

    except Exception as e:
        print("❌ ERRORE:", e)
        time.sleep(10)
