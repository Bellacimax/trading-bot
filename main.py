from flask import Flask
from threading import Thread
from stats import init_stats, save_trade
import yfinance as yf
import pandas as pd
import time
import requests
import os
import csv
from datetime import datetime, UTC
from scanner import rank_tickers
from datetime import timedelta

# =========================================
# TELEGRAM
# =========================================

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_telegram(msg):
    
    try:

        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

        requests.get(
            url,
            params={
                "chat_id": CHAT_ID,
                "text": msg
            }
        )

    except Exception as e:

        print("Errore Telegram:", e)

# =========================================
# SAVE TRADE
# =========================================

def save_trade(

    ticker,

    side,

    entry,

    exit_price,

    pnl,

    rr,

    result

):

    
    row = pd.DataFrame([{

        "ticker": ticker,

        "side": side,

        "entry": round(entry, 2),

        "exit": round(exit_price, 2),

        "pnl": round(pnl, 2),

        "rr": rr,

        "result": result,

        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    }])

    row.to_csv(

        "trade_history.csv",

        mode="a",

        header=not os.path.exists("trade_history.csv"),

        index=False

    )
# =========================================
# PARAMETRI
# =========================================

CAPITALE = 1000
RISCHIO = 0.01
MAX_TICKERS = 20

COMMISSIONI = 2

# =========================================
# TICKERS
# =========================================

with open("tickers.txt") as f:

    TICKERS = [
        line.strip()
        for line in f
        if line.strip()
    ]

# =========================================
# VARIABILI
# =========================================

index = 0

equity = CAPITALE

active_trades = {}

stats = {
    "wins": 0,
    "losses": 0,
    "pnl": 0
}

# =========================================
# CSV
# =========================================

with open("trades.csv", "w", newline="") as f:

    writer = csv.writer(f)

    writer.writerow([
        "time",
        "ticker",
        "side",
        "entry",
        "exit",
        "pnl"
    ])

# =========================================
# ATR
# =========================================

def compute_atr(data, period=14):

    tr = pd.concat([

        data["High"] - data["Low"],

        (data["High"] - data["Close"].shift()).abs(),

        (data["Low"] - data["Close"].shift()).abs()

    ], axis=1).max(axis=1)

    return tr.rolling(period).mean()
    

# =========================================
# INDICATORI
# =========================================

def compute_indicators(df):

    # EMA
    df["EMA50"] = df["Close"].ewm(span=50).mean()

    df["EMA200"] = df["Close"].ewm(span=200).mean()

    # RSI
    delta = df["Close"].diff()

    gain = (delta.where(delta > 0, 0)).rolling(14).mean()

    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()

    rs = gain / loss

    df["RSI"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df["Close"].ewm(span=12).mean()

    ema26 = df["Close"].ewm(span=26).mean()

    df["MACD"] = ema12 - ema26

    df["MACD_signal"] = df["MACD"].ewm(span=9).mean()

    return df

# =========================================
# START
# =========================================

print("🚀 BOT AVVIATO")
try:

    print("📨 TEST TELEGRAM...")

    send_telegram("🚀 BOT ONLINE")

    print("✅ TELEGRAM OK")

except Exception as e:

    print(f"❌ TELEGRAM ERROR: {e}")
init_stats()

# =========================================
# DASHBOARD
# =========================================

app = Flask(__name__)

@app.route("/dashboard")
def dashboard():

    total_pnl = 0

    safe_positions = {}

    for tkr, pos in open_positions.items():

        try:

            live = yf.download(

                tkr,

                period="1d",

                interval="1m",

                progress=False,

                threads=False

            )

            if live.empty:

                continue

            current_price = live["Close"].iloc[-1]

            if pos["side"] == "BUY":

                pnl = (

                    current_price - pos["entry"]

                ) * pos["qty"]

            else:

                pnl = (

                    pos["entry"] - current_price

                ) * pos["qty"]

            pos["live_price"] = round(current_price, 2)

            pos["pnl"] = round(pnl, 2)

            safe_positions[tkr] = pos

            total_pnl += pnl

        except Exception as e:

            print(f"DASHBOARD ERROR {tkr}: {e}")

    return {

        "open_positions": safe_positions,

        "total_open": len(safe_positions),

        "max_trades": MAX_TRADES,

        "capital_per_trade": CAPITALE_PER_TRADE,

        "total_pnl": round(total_pnl, 2)

    }


@app.route("/")
def home():

    pnl = round(stats["pnl"], 2)

    wins = stats["wins"]

    losses = stats["losses"]

    total = wins + losses

    winrate = round((wins / total) * 100, 1) if total > 0 else 0

    active_html = ""

    for t, tr in active_trades.items():

        active_html += f"""

        <tr>

            <td>{t}</td>

            <td>{tr['side']}</td>

            <td>{round(tr['entry'],2)}</td>

            <td>{round(tr['stop'],2)}</td>

            <td>{round(tr['target'],2)}</td>

        </tr>

        """

    history_html = "<p>Nessun trade chiuso</p>"

    if os.path.exists("trade_history.csv"):

        df = pd.read_csv("trade_history.csv")

        if len(df) > 0:

            history_html = df.tail(20).to_html(index=False)

    return f"""

    <html>

    <head>

        <title>Trading Bot Dashboard</title>

        <meta http-equiv="refresh" content="15">

    </head>

    <body style="background:#111;color:white;font-family:Arial;padding:20px;">

        <h1>🚀 Trading Bot Dashboard</h1>

        <h2>📊 Stats</h2>

        <p>💰 PnL: {pnl} €</p>

        <p>📈 Winrate: {winrate}%</p>

        <p>🎯 Wins: {wins}</p>

        <p>❌ Losses: {losses}</p>

        <p>📦 Active Trades: {len(active_trades)}</p>

        <h2>📡 Trade Attivi</h2>

        <table border="1" cellpadding="10">

            <tr>

                <th>Ticker</th>

                <th>Side</th>

                <th>Entry</th>

                <th>Stop</th>

                <th>Target</th>

            </tr>

            {active_html}

        </table>

        <h2>📜 Trade History</h2>

        {history_html}

    </body>

    </html>

    """


# =========================================
# DASHBOARD RUNNER
# =========================================

def run_dashboard():

    port = int(os.environ.get("PORT", 8080))

    app.run(

        host="0.0.0.0",

        port=port

    )


# =========================================
# EARNINGS CHECK
# =========================================

def check_earnings(ticker):

    try:

        stock = yf.Ticker(ticker)

        earnings = stock.calendar

        if earnings is None or earnings.empty:

            return None

        earnings_date = earnings.iloc[0][0]

        if earnings_date is None:

            return None

        days_left = (

            earnings_date.date()

            - datetime.now().date()

        ).days

        return days_left

    except:

        return None


# =========================================
# MAIN
# =========================================

# =========================================
# MARKET FILTER
# =========================================

def market_is_bullish():

    try:

        spy = yf.download(

            "SPY",

            period="1y",

            interval="1d",

            progress=False

        )

        if spy is None or spy.empty:

            return True

        if isinstance(spy.columns, pd.MultiIndex):

            spy.columns = spy.columns.get_level_values(0)

        ema200 = spy["Close"].ewm(

            span=200,

            adjust=False

        ).mean()

        return spy["Close"].iloc[-1] > ema200.iloc[-1]

    except:

        return True


# =========================================
# MAIN LOOP
# =========================================

CAPITALE_PER_TRADE = int(

    os.getenv("CAPITALE_PER_TRADE", 1000)

)

MAX_TRADES = int(

    os.getenv("MAX_TRADES", 5)

)
MIN_GAP = float(
    os.getenv("MIN_GAP", 1)

)

MIN_VOLUME_RATIO = float(

    os.getenv("MIN_VOLUME_RATIO", 0.7)

)

RSI_SELL_LIMIT = float(

    os.getenv("RSI_SELL_LIMIT", 10)

)

COOLDOWN_MINUTES = int(

    os.getenv("COOLDOWN_MINUTES", 60)

)

open_positions = {}

cooldown_tickers = {}
bad_tickers = set()


market_data_cache = {}

last_download = {}


def trading_loop():

    index = 0

    while True:

        try:

            # =========================================
            # WEEKEND FILTER
            # =========================================

            weekday = datetime.now().weekday()

            if weekday >= 5:

                print("📴 Weekend - market closed")

                time.sleep(600)

                continue

            # =========================================
            # ORARIO NEW YORK
            # =========================================

            ora_utc = datetime.now(UTC).hour

            ora_ny = (ora_utc - 4) % 24

            print(f"🕒 Ora NY: {ora_ny}")

            if 0 <= ora_ny < 4:

                print("😴 Notte USA - pausa")

                time.sleep(300)

                continue

            # =========================================
            # FASE MERCATO
            # =========================================

            if ora_ny < 10:

                fase = "Pre-market"

            elif ora_ny < 16:

                fase = "Market"

            else:

                fase = "After-hours"

            print(f"📊 Fase: {fase}")

            if fase == "After-hours":

                print("🌙 After-hours pausa")

                time.sleep(60)

                continue

            # =========================================
            # TICKERS
            # =========================================

            subset = sorted(list(set(TICKERS)))

            BLACKLIST = [

                "ARKK",
                "XBI",
                "UVXY",
                "SQQQ",
                "SPXL",
                "SPXS",
                "PPA",
                "XAR",
                "HCP"

            ]

            subset = [

                t for t in subset

                if t not in BLACKLIST

            ]

            print(f"🔥 Tot Tickers: {len(subset)}")

# =========================================
# LOOP TICKER
# =========================================

time.sleep(3)

for ticker in subset:

    try:

        if ticker in bad_tickers:

            continue

        if not ticker.isalpha():

            continue

        if len(ticker) > 5:

            continue

        ticker = ticker.replace('"', '').replace(',', '').strip()

        print(f"🔍 Analizzo {ticker}")

        print(f"🧠 Scanner attivo -> {ticker}")

        # evita rate limit Yahoo
        time.sleep(3)

    except Exception as e:

        print(f"❌ ERRORE {ticker}: {e}")

        continue

        # =========================================
        # COOLDOWN
        # =========================================

        if ticker in cooldown_tickers:

            last_alert = cooldown_tickers[ticker]

            minutes_passed = (

                datetime.now() - last_alert

            ).seconds / 60

            if minutes_passed < COOLDOWN_MINUTES:

                print(f"⏳ COOLDOWN -> {ticker}")

                continue

    except Exception as e:

        print(f"❌ ERRORE {ticker}: {e}")

        continue
                
                    # =========================================
                    # CACHE
                    # =========================================

                    current_time = time.time()

                    if (

                        ticker not in market_data_cache

                        or current_time - last_download.get(ticker, 0) > 60

                    ):

                        df = yf.download(

                            ticker,

                            period="5d",

                            interval="1d",

                            progress=False,

                            threads=False

                        )

                       if df is None or df.empty:

                            bad_tickers.add(ticker)

                            print(f"❌ BAD TICKER -> {ticker}")

                            continue


                        market_data_cache[ticker] = df

                        last_download[ticker] = current_time

                    else:

                        df = market_data_cache[ticker]

                    if len(df) < 50:

                        print(f"⚠️ FEW DATA -> {ticker}")

                        continue

                    if isinstance(df.columns, pd.MultiIndex):

                        df.columns = df.columns.get_level_values(0)

                    df = df[[

                        "Open",
                        "High",
                        "Low",
                        "Close",
                        "Volume"

                    ]].dropna()

    
                     # =========================================
                    # INDICATORI
                    # =========================================

                    df["ATR"] = compute_atr(df)

                    df = compute_indicators(df)

                    df["VWAP"] = (

                        (df["Close"] * df["Volume"]).cumsum()

                        / df["Volume"].cumsum()

                    )

                    df["EMA20"] = df["Close"].ewm(span=20).mean()

                    df["EMA50"] = df["Close"].ewm(span=50).mean()

                    df["EMA200"] = df["Close"].ewm(span=200).mean()

                    last = df.iloc[-1]

                    price = last["Close"]

                    ema20 = last["EMA20"]

                    ema50 = last["EMA50"]

                    ema200 = last["EMA200"]

                    atr = last["ATR"]

                    rsi = last["RSI"]

                    volume = last["Volume"]

                    vwap = last["VWAP"]

                    prev_close = df["Close"].iloc[-2]

                    gap_pct = (

                        (price - prev_close)

                        / prev_close

                    ) * 100

                    if pd.isna(atr) or atr == 0:

                        continue

                    # =========================================
                    # HTF
                    # =========================================

                    df_htf = yf.download(

                        ticker,

                        period="3mo",

                        interval="1d",

                        progress=False,

                        threads=False

                    )

                    if df_htf is None or df_htf.empty:

                        continue

                    if isinstance(df_htf.columns, pd.MultiIndex):

                        df_htf.columns = df_htf.columns.get_level_values(0)

                    df_htf = compute_indicators(df_htf)

                    htf_last = df_htf.iloc[-1]

                    # =========================================
                    # DAILY
                    # =========================================

                    df_daily = yf.download(

                        ticker,

                        period="1y",

                        interval="1d",

                        progress=False,

                        threads=False

                    )

                    if df_daily is None or df_daily.empty:

                        continue

                    if isinstance(df_daily.columns, pd.MultiIndex):

                        df_daily.columns = df_daily.columns.get_level_values(0)

                    ema200_daily = df_daily["Close"].ewm(

                        span=200,

                        adjust=False

                    ).mean()

                    daily_price = df_daily["Close"].iloc[-1]

                    daily_up = daily_price > ema200_daily.iloc[-1]

                    daily_down = daily_price < ema200_daily.iloc[-1]

                    # =========================================
                    # VOLUME
                    # =========================================

                    volume_today = df_daily["Volume"].iloc[-1]

                    avg_volume = df_daily["Volume"].rolling(20).mean().iloc[-1]

                    relative_volume = volume_today / avg_volume

                    strong_volume = relative_volume > 1.5

                    # =========================================
                    # SCORE
                    # =========================================

                    score_buy = 0

                    score_sell = 0

                    trend_up = price > ema200

                    trend_down = price < ema200

                    rsi_buy = rsi > 55

                    rsi_sell = rsi < 45

                    macd_buy = last["MACD"] > last["MACD_signal"]

                    macd_sell = last["MACD"] < last["MACD_signal"]

                    if trend_up:
                        score_buy += 1

                    if trend_down:
                        score_sell += 1

                    if rsi_buy:
                        score_buy += 1

                    if rsi_sell:
                        score_sell += 1

                    if macd_buy:
                        score_buy += 1

                    if macd_sell:
                        score_sell += 1

                    print(

                        f"{ticker} | "

                        f"BUY={score_buy} | "

                        f"SELL={score_sell}"

                    )

                    # =========================================
                    # TRADE ATTIVO
                    # =========================================

                    if ticker in active_trades:

                        continue

            
                    # =========================================
                    # ENTRY
                    # =========================================

                    if score_buy >= 2 and strong_volume:

                        side = "BUY"

                        print(f"🟢 BUY READY -> {ticker}")

                    elif score_sell >= 2 and strong_volume:

                        side = "SELL"

                        print(f"🔴 SELL READY -> {ticker}")

                    else:

                        print(f"⚠️ SKIP -> {ticker}")

                        continue

                    # =========================================
                    # GAP FILTER
                    # =========================================

                    if abs(gap_pct) < MIN_GAP:

                        print(f"⚠️ LOW GAP -> {ticker}")

                        continue

                    # =========================================
                    # EMA FILTER
                    # =========================================

                    if side == "BUY":

                        if not (ema20 > ema50 > ema200):

                            print(f"⚠️ EMA FAIL -> {ticker}")

                            continue

                    if side == "SELL":

                        if not (ema20 < ema50 < ema200):

                            print(f"⚠️ EMA FAIL -> {ticker}")

                            continue

                    # =========================================
                    # VWAP FILTER
                    # =========================================

                    if side == "BUY" and price < vwap:

                        continue

                    if side == "SELL" and price > vwap:

                        continue

                    # =========================================
                    # STOP / TARGET
                    # =========================================

                    distanza_stop = atr * 4

                    if side == "BUY":

                        stop = price - distanza_stop

                        target = price + atr * 6

                    else:

                        stop = price + distanza_stop

                        target = price - atr * 6

                    risk = abs(price - stop)

                    reward = abs(target - price)

                    rr = round(reward / risk, 2) if risk != 0 else 0

                    if rr < 1.3:

                        print(f"❌ RR FAIL -> {ticker}")

                        continue

                    # =========================================
                    # SIZE
                    # =========================================

                    qty = round(

                        CAPITALE_PER_TRADE / price

                    )

                    # =========================================
                    # SALVA
                    # =========================================

                    active_trades[ticker] = {

                        "side": side,

                        "entry": price,

                        "stop": stop,

                        "target": target,

                        "qty": qty

                    }

                    cooldown_tickers[ticker] = datetime.now()

                    print(

                        f"🚀 {side} {ticker} | "

                        f"Entry={round(price,2)} | "

                        f"Target={round(target,2)}"

                    )

                except Exception as e:

                    print(f"❌ ERRORE {ticker}: {e}")

                    continue

            # =========================================
            # NEXT LOOP
            # =========================================

            index += MAX_TICKERS

            if index >= len(TICKERS):

                index = 0

            time.sleep(60)

        except Exception as e:

            print(f"❌ ERRORE LOOP: {e}")

            time.sleep(60)


if __name__ == "__main__":

    Thread(target=trading_loop).start()

    run_dashboard()
