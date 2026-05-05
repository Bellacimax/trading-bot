import yfinance as yf
import pandas as pd
import time
import requests
from datetime import datetime
import csv

# ===== TELEGRAM =====
TOKEN = "INSERISCI_TOKEN"
CHAT_ID = "INSERISCI_CHAT_ID"

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.get(url, params={"chat_id": CHAT_ID, "text": msg})
    except:
        pass

# ===== PARAMETRI =====
CAPITALE = 1000
RISCHIO = 0.01

# ===== TICKERS =====
with open("tickers.txt") as f:
    TICKERS = [line.strip() for line in f if line.strip()]

MAX_TICKERS = 20
index = 0

active_trades = {}
watchlist = {}
equity = CAPITALE

# ===== CSV =====
with open("trades.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["time","ticker","type","result","equity"])

# ===== ATR =====
def compute_atr(data, period=14):
    tr = pd.concat([
        data["High"] - data["Low"],
        abs(data["High"] - data["Close"].shift()),
        abs(data["Low"] - data["Close"].shift())
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# ===== RSI =====
def compute_rsi(data, period=14):
    delta = data["Close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

print("🚀 BOT AVVIATO\n")
send_telegram("🚀 BOT ONLINE")

# ===== LOOP =====
while True:
    try:
        subset = TICKERS[index:index+MAX_TICKERS]

        for ticker in subset:

            df = yf.download(ticker, period="5d", interval="5m", progress=False)

            if df is None or df.empty or len(df) < 50:
                continue

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            df = df[["Open","High","Low","Close","Volume"]].dropna()

            df["ATR"] = compute_atr(df)
            df["EMA200"] = df["Close"].ewm(span=200).mean()

            df["RSI"] = compute_rsi(df)

            df["EMA12"] = df["Close"].ewm(span=12).mean()
            df["EMA26"] = df["Close"].ewm(span=26).mean()
            df["MACD"] = df["EMA12"] - df["EMA26"]
            df["SIGNAL"] = df["MACD"].ewm(span=9).mean()

            last = df.iloc[-1]
            price = last["Close"]
            atr = last["ATR"]

            if pd.isna(atr) or atr == 0:
                continue

            # evita trade piccoli
            if atr < price * 0.003:
                continue

            volume_avg = df["Volume"].rolling(20).mean()
            volume_ok = df["Volume"].iloc[-1] > volume_avg.iloc[-1]

            trend_up = price > last["EMA200"]
            trend_down = price < last["EMA200"]

            rsi = last["RSI"]
            macd = last["MACD"]
            signal = last["SIGNAL"]

            high_20 = df["High"].rolling(20).max()
            low_20 = df["Low"].rolling(20).min()

            resistance = high_20.iloc[-2]
            support = low_20.iloc[-2]

            prev_close = df["Close"].iloc[-2]

            # ===== WATCHLIST =====
            if prev_close <= resistance and price > resistance and trend_up:
                watchlist[ticker] = ("BUY", resistance)

            elif prev_close >= support and price < support and trend_down:
                watchlist[ticker] = ("SELL", support)

            # ===== ENTRY =====
            if ticker in watchlist and ticker not in active_trades:

                side, level = watchlist[ticker]

                distanza_stop = atr * 4
                rischio_euro = CAPITALE * RISCHIO

                qty = int(min(rischio_euro / distanza_stop, CAPITALE / price))
                if qty <= 0:
                    continue

                stop = price - distanza_stop if side=="BUY" else price + distanza_stop
                target = price + atr*7 if side=="BUY" else price - atr*7

                risk = abs(price - stop)
                reward = abs(target - price)
                rr = round(reward / risk, 2) if risk != 0 else 0

                if rr < 1.6:
                    continue

                # filtro RSI
                if side == "BUY":
                    if not (40 < rsi < 75):
                        continue
                else:
                    if not (25 < rsi < 60):
                        continue

                active_trades[ticker] = {
                    "side": side,
                    "entry": price,
                    "stop": stop,
                    "target": target,
                    "qty": qty,
                    "risk": rischio_euro
                }

                send_telegram(
                    f"🚀 {side} {ticker} @ {round(price,2)}\n"
                    f"📈 Resistenza: {round(resistance,2)}\n"
                    f"📉 Supporto: {round(support,2)}\n"
                    f"RSI: {round(rsi,1)}\n"
                    f"🛑 Stop: {round(stop,2)}\n"
                    f"🎯 Target: {round(target,2)}\n"
                    f"⚖️ R/R: {rr}"
                )

                watchlist.pop(ticker)

        # ===== LOOP =====
        print(f"💰 Equity: {round(equity,2)}€ | Attivi: {len(active_trades)}")

        index += MAX_TICKERS
        if index >= len(TICKERS):
            index = 0

        time.sleep(60)

    except Exception as e:
        print("❌ ERRORE:", e)
        time.sleep(10)
