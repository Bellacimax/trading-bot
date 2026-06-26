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
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY")

# =========================================
# TELEGRAM
# =========================================

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_telegram(msg):

    try:

        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

        response = requests.get(

            url,

            params={

                "chat_id": CHAT_ID,

                "text": msg

            }

        )

        print("📨 TELEGRAM STATUS:", response.status_code)

        print("📨 TELEGRAM RESPONSE:", response.text)

    except Exception as e:

        print(f"❌ TELEGRAM ERROR: {e}")


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
MAX_TICKERS = 10

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

    print("⚠️ MARKET FILTER DISABLED")

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

            if 0 <= ora_ny < 1: 
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
            
                time.sleep(300)
            
                continue
                
            subset = sorted(list(set(TICKERS)))[index:index+MAX_TICKERS]

            index += MAX_TICKERS
            
            if index >= len(TICKERS):
            
                index = 0


            # =========================================
            # TICKERS
            # =========================================

            BLACKLIST = [

                "ARKK",
                "XBI",
                "UVXY",
                "SQQQ"

            ]

            subset = [

                t for t in subset

                if t not in BLACKLIST

            ]

            
            print(f"🔥 Tot Tickers: {len(subset)}")
            # =========================================
            # LOOP TICKER
            # =========================================

            try:

                market_bullish = market_is_bullish()

            except Exception as e:

                print(f"❌ MARKET FILTER ERROR: {e}")

                market_bullish = True

            print("🚀 INIZIO LOOP TICKER")
            print(f"📈 MARKET BULLISH: {market_bullish}")
            print(f"📋 Tickers nel batch: {len(subset)}")

            print("📋 LISTA TICKERS:")
            
            for x in subset:
            
                print(f"➡️ {x}")
            
            print("📋 FINE LISTA")
                   
            for ticker in subset:
    
                print("🔥 FOR LOOP ENTRATO")
            
                print(f"🔥 TICKER RAW: {ticker}")
            
                ticker = str(ticker)
            
                ticker = ticker.split("#")[0]
            
                ticker = ticker.replace('"', '')
            
                ticker = ticker.replace("'", "")
            
                ticker = ticker.replace(",", "")
            
                ticker = ticker.strip()
            
                print(f"✅ CLEAN TICKER: {ticker}")
        
                try:
        

                    if ticker in bad_tickers:

                        continue

                    if not ticker.isalpha():

                        continue

                    if len(ticker) > 5:

                        continue

                    ticker = ticker.strip()

                    
                    print(f"🔍 Analizzo {ticker}")
                    
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

                    # =========================================
                    # DOWNLOAD DATI
                    # =========================================
                    
                    print(f"📥 Download {ticker} START")
                    
                    try:
                    
                        print("DOWNLOAD CON YFINANCE")
                    
                        df = yf.download(
                            ticker,
                            period="9mo",
                            interval="1d",
                            progress=False,
                            auto_adjust=False,
                            threads=False
                        )
                    
                        if df.empty:
                    
                            print(f"❌ NO DATA -> {ticker}")
                    
                            continue
                    
                        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
                    
                        print(f"📊 DOWNLOAD OK {ticker}")
                        print(f"📊 ROWS = {len(df)}")
                    
                    except Exception as e:
                    
                        print(f"❌ DOWNLOAD ERROR {ticker}: {repr(e)}")
                    
                        continue
                    

                    # =========================================
                    # CONTROLLO DATI
                    # =========================================
                    
                    if len(df) < 50:
                    
                        print(f"⚠️ FEW DATA -> {ticker}")
                    
                        continue
                    
                    
                    supporto = round(
                        df["Low"].tail(20).min(),
                        2
                    )
                    
                    resistenza = round(
                        df["High"].tail(20).max(),
                        2
                    )
                    
                    print(
                        f"✅ DOWNLOAD OK {ticker} | "
                        f"Rows={len(df)} | "
                        f"SUP={supporto} | "
                        f"RES={resistenza}"
                    )
                    
                    # =========================================
                    # INDICATORI
                    # =========================================

                    df["ATR"] = compute_atr(df)

                    df = compute_indicators(df)

                    last = df.iloc[-1]

                    price = last["Close"]

                    atr = last["ATR"]

                    if pd.isna(atr) or atr == 0:

                        continue

                    print(f"✅ {ticker} OK | Price={round(price,2)}")

                    
                    # =========================================
                    # SIGNALS
                    # =========================================
                    
                    score_buy = 0
                    score_sell = 0
                    
                    # TREND
                    if last["EMA50"] > last["EMA200"]:
                        score_buy += 1
                    else:
                        score_sell += 1
                    
                    # RSI
                    if last["RSI"] > 55:
                        score_buy += 1
                    
                    elif last["RSI"] < 45:
                        score_sell += 1
                    
                    # MACD
                    if last["MACD"] > last["MACD_signal"]:
                        score_buy += 1
                    else:
                        score_sell += 1
                    
                    # VOLUME
                    volume_ratio = (
                        df["Volume"].iloc[-1]
                        /
                        df["Volume"].rolling(20).mean().iloc[-1]
                    )
                    
                    strong_volume = volume_ratio > MIN_VOLUME_RATIO
                    
                    print(
                        f"{ticker} | "
                        f"BUY={score_buy} | "
                        f"SELL={score_sell} | "
                        f"VOL={round(volume_ratio,2)}"
                    )
                    
                    # =========================================
                    # DECISIONE
                    # =========================================
                    
                    side = None
                    
                    if score_buy >= 2 and strong_volume:
                    
                        side = "BUY"
                    
                        stop = price - atr
                    
                        target = price + (atr * 2)
                    
                        rr = 2
                    
                    elif score_sell >= 2 and strong_volume:
                    
                        side = "SELL"
                    
                        stop = price + atr
                    
                        target = price - (atr * 2)
                    
                        rr = 2
                    
                    if side is None:
                    
                        print(f"⏭️ NO SIGNAL {ticker}")
                    
                        continue

                    qty = int(

                        CAPITALE_PER_TRADE / price

                    )

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

                    send_telegram(

                        f"🚀 {side} {ticker}\n"

                        f"💰 Entry: {round(price,2)}\n"

                        f"🛑 Stop: {round(stop,2)}\n"

                        f"🎯 Target: {round(target,2)}\n"

                        f"📊 RR: {rr}\n"

                        f"🟢 Supporto: {supporto}\n"

                        f"🔴 Resistenza: {resistenza}"

                    )

                except Exception as e:

                    print(f"❌ ERRORE {ticker}: {e}")

                    continue

            time.sleep(60)

        except Exception as e:

            print(f"❌ ERRORE LOOP: {e}")

            time.sleep(60)


# =========================================
# START BOT
# =========================================

if __name__ == "__main__":

    Thread(
        target=trading_loop,
        daemon=True
    ).start()

    run_dashboard()
