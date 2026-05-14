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

send_telegram("🚀 BOT ONLINE")
init_stats()
# =========================================
# DASHBOARD
# =========================================

app = Flask(__name__)

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

        <style>

            body {{

                background: #111;

                color: white;

                font-family: Arial;

                padding: 20px;

            }}

            h1 {{

                color: #00ff99;

            }}

            .card {{

                background: #1e1e1e;

                padding: 20px;

                margin-bottom: 20px;

                border-radius: 10px;

            }}

            table {{

                width: 100%;

                border-collapse: collapse;

                background: #222;

            }}

            th, td {{

                border: 1px solid #333;

                padding: 10px;

                text-align: center;

            }}

            th {{

                background: #333;

            }}

        </style>

    </head>

    <body>

        <h1>🚀 Trading Bot Dashboard</h1>

        <div class="card">

            <h2>📊 Stats</h2>

            <p>💰 PnL: {pnl} €</p>

            <p>📈 Winrate: {winrate}%</p>

            <p>🎯 Wins: {wins}</p>

            <p>❌ Losses: {losses}</p>

            <p>📦 Active Trades: {len(active_trades)}</p>

        </div>

        <div class="card">

            <h2>📡 Trade Attivi</h2>

            <table>

                <tr>

                    <th>Ticker</th>

                    <th>Side</th>

                    <th>Entry</th>

                    <th>Stop</th>

                    <th>Target</th>

                </tr>

                {active_html}

            </table>

        </div>

        <div class="card">

            <h2>📜 Trade History</h2>

            {history_html}

        </div>

    </body>

    </html>

    """

def run_dashboard():

    app.run(host="0.0.0.0", port=8080)

Thread(target=run_dashboard).start()

# =========================================
# LOOP PRINCIPALE
# =========================================

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

        days_left = (earnings_date.date() - datetime.now().date()).days

        return days_left

    except:

        return None
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

    os.getenv("MIN_GAP", 2)

)

MIN_VOLUME_RATIO = float(

    os.getenv("MIN_VOLUME_RATIO", 1.2)

)

RSI_SELL_LIMIT = float(

    os.getenv("RSI_SELL_LIMIT", 10)

)

COOLDOWN_MINUTES = int(

    os.getenv("COOLDOWN_MINUTES", 60)

)

open_positions = []

cooldown_tickers = {}

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

        # pausa solo notte vera
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

        market_bullish = market_is_bullish()

        print(

            f"📈 Market Bullish: "

            f"{market_bullish}"

        )

        # =========================================
        # SUBSET TICKER
        # =========================================

        subset = sorted(list(set(TICKERS)))

        print(f"🔥 Tot Tickers: {len(subset)}")

        # =========================================
        # LOOP TICKER
        # =========================================

        for ticker in subset:

            if (
                "TICKERS" in ticker
                or "[" in ticker
                or "]" in ticker
                or "=" in ticker
                or "#" in ticker
            ):
                continue

            ticker = ticker.replace('"', '').replace(',', '').strip()

            print(f"🔍 Analizzo {ticker}")
            if ticker in cooldown_tickers:

                last_alert = cooldown_tickers[ticker]

                minutes_passed = (

                    datetime.now() - last_alert

                ).seconds / 60

                if minutes_passed < 60:

                    print(f"⏳ COOLDOWN -> {ticker}")

                    continue

                        # =========================================
            # EARNINGS INFO
            # =========================================

            if ticker in ["SPY", "QQQ", "IWM", "SMH", "ARKK", "XLE"]:

                earnings_days = None

            else:

                earnings_days = check_earnings(ticker)

            if earnings_days is not None and earnings_days <= 7:

                print(

                    f"🔥 Earnings Soon -> "

                    f"{ticker} ({earnings_days}d)"

                )

                send_telegram(

                    f"⚠️ EARNINGS SOON\n\n"

                    f"Ticker: {ticker}\n"

                    f"Days: {earnings_days}"

                )

            # =========================================
            # DATI 1H
            # =========================================

            df = yf.download(

                 ticker,

                 period="1mo",

                 interval="1h",

                 progress=False

            )
           
            if df is None or df.empty:

                print(f"❌ NO DATA -> {ticker}")

                continue

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

            if len(df) < 50:

                continue

            # =========================================
            # ATR
            # =========================================

            df["ATR"] = compute_atr(df)

            # =========================================
            # ATR
            # =========================================

            df["ATR"] = compute_atr(df)

            # =========================================
            # INDICATORI
            # =========================================

            df = compute_indicators(df)

            df["VWAP"] = (

                (df["Close"] * df["Volume"]).cumsum()

                / df["Volume"].cumsum()

            )

            df["EMA20"] = df["Close"].ewm(span=20).mean()

            df["EMA50"] = df["Close"].ewm(span=50).mean()

            df["EMA200"] = df["Close"].ewm(span=200).mean()

            last = df.iloc[-1]
            ema20 = last["EMA20"]

            ema50 = last["EMA50"]

            ema200 = last["EMA200"]

            price = last["Close"]
            prev_close = df["Close"].iloc[-2]

            gap_pct = (

                (price - prev_close)

                / prev_close

            ) * 100
                    
            vwap = last["VWAP"]

            rsi = last["RSI"]

            atr = last["ATR"]
            volume = last["Volume"]

            volume_ma = df["Volume"].rolling(20).mean().iloc[-1]

            if volume < volume_ma * MIN_VOLUME_RATIO:

                print(f"⚠️ LOW VOLUME -> {ticker}")

                continue

            if atr < price * 0.01:

                print(f"⚠️ LOW VOLATILITY -> {ticker}")

                continue

            if pd.isna(atr) or atr == 0:

                continue

            # =========================================
            # DATI 1H
            # =========================================

            df_htf = yf.download(

                ticker,

                period="3mo",

                interval="1h",

                progress=False

            )

            if df_htf is None or df_htf.empty or len(df_htf) < 50:

                continue

            if isinstance(df_htf.columns, pd.MultiIndex):

                df_htf.columns = df_htf.columns.get_level_values(0)

            df_htf = df_htf[[
                "Open",
                "High",
                "Low",
                "Close",
                "Volume"
            ]].dropna()

            df_htf = compute_indicators(df_htf)

            htf_last = df_htf.iloc[-1]

            

            # =========================================
            # DATI DAILY
            # =========================================

            df_daily = yf.download(

                ticker,

                period="1y",

                interval="1d",

                progress=False

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
            # DAILY VOLUME FILTER
            # =========================================

            volume_today = df_daily["Volume"].iloc[-1]

            avg_volume = df_daily["Volume"].rolling(20).mean().iloc[-1]

            strong_volume = volume_today > avg_volume * 0.8

            # =========================================
            # SUPPORTI / RESISTENZE
            # =========================================

            support = df["Low"].rolling(20).min().iloc[-1]

            resistance = df["High"].rolling(20).max().iloc[-1]

            # =========================================
            # FIBONACCI
            # =========================================

            swing_high = df["High"].rolling(50).max().iloc[-1]

            swing_low = df["Low"].rolling(50).min().iloc[-1]

            fib_382 = swing_high - (swing_high - swing_low) * 0.382

            fib_50 = swing_high - (swing_high - swing_low) * 0.5

            fib_618 = swing_high - (swing_high - swing_low) * 0.618

            # =========================================
            # PIVOT
            # =========================================

            pivot = (

                last["High"]

                + last["Low"]

                + last["Close"]

            ) / 3

            r1 = (2 * pivot) - last["Low"]

            s1 = (2 * pivot) - last["High"]

            # =========================================
            # TOP MOVER
            # =========================================

            if len(df) < 20:

                continue

            move_perc = (

                df["Close"].iloc[-1]

                - df["Close"].iloc[-20]

            ) / price

            if abs(move_perc) < 0.01:

                continue

            # =========================================
            # VOLUME SPIKE
            # =========================================

            volume_avg = df["Volume"].rolling(20).mean()

            volume_spike = (

                df["Volume"].iloc[-1]

                > volume_avg.iloc[-1] * 1.5

            )

            if not volume_spike:

                continue

            # =========================================
            # TREND
            # =========================================

            trend_up = price > last["EMA200"]

            trend_down = price < last["EMA200"]

            htf_up = htf_last["Close"] > htf_last["EMA200"]

            htf_down = htf_last["Close"] < htf_last["EMA200"]

            # =========================================
            # RSI
            # =========================================

            rsi_buy = last["RSI"] > 50

            rsi_sell = last["RSI"] < 50

            # =========================================
            # MACD
            # =========================================

            macd_buy = last["MACD"] > last["MACD_signal"]

            macd_sell = last["MACD"] < last["MACD_signal"]

            # =========================================
            # GOLDEN / DEATH CROSS
            # =========================================

            golden_cross = (

                df["EMA50"].iloc[-2]

                < df["EMA200"].iloc[-2]

                and

                df["EMA50"].iloc[-1]

                > df["EMA200"].iloc[-1]

            )

            death_cross = (

                df["EMA50"].iloc[-2]

                > df["EMA200"].iloc[-2]

                and

                df["EMA50"].iloc[-1]

                < df["EMA200"].iloc[-1]

            )

            # =========================================
            # SCORE
            # =========================================

            score_buy = 0

            score_sell = 0

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

            if golden_cross:
                score_buy += 2

            if death_cross:
                score_sell += 2

            # =========================================
            # TRADE GIÀ ATTIVO
            # =========================================

            if ticker in active_trades:

                continue

            # =========================================
            # DEBUG SCORE
            # =========================================

            print(
                f"{ticker} | "
                f"BUY={score_buy} "
                f"SELL={score_sell} "
                f"HTF_UP={htf_up} "
                f"HTF_DOWN={htf_down}"
            )

            # =========================================
            # ENTRY
            # =========================================

            if score_buy >= 3 and htf_up and daily_up and market_bullish and strong_volume:

                side = "BUY"

                score = score_buy

                print(f"🟢 BUY READY -> {ticker}")

            elif score_sell >= 3 and htf_down and daily_down and strong_volume:

                side = "SELL"

                score = score_sell

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
            # EMA TREND FILTER
            # =========================================

            if side == "BUY":

                if not (ema20 > ema50 > ema200):

                    print(f"⚠️ EMA TREND FAIL -> {ticker}")

                    continue

            if side == "SELL":

                if not (ema20 < ema50 < ema200):

                    print(f"⚠️ EMA TREND FAIL -> {ticker}")

                    continue
                    
            # =========================================
            # RSI EXTREME FILTER
            # =========================================

            if side == "SELL" and rsi < RSI_SELL_LIMIT:

                print(f"⚠️ RSI TOO LOW -> {ticker}")

                continue

            # =========================================
            # VWAP FILTER
            # =========================================

            if side == "BUY" and price < vwap:

                print(f"⚠️ BELOW VWAP -> {ticker}")

                continue

            if side == "SELL" and price > vwap:

                print(f"⚠️ ABOVE VWAP -> {ticker}")

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

                print(f"❌ RR FAIL -> {ticker} | RR={rr}")

                continue

            # =========================================
            # SIZE
            # =========================================

            rischio_euro = CAPITALE * RISCHIO

            qty = int(

                min(

                    rischio_euro / distanza_stop,

                    CAPITALE / price

                )

            )

            if qty <= 0:

                continue

            # =========================================
            # COMMISSIONI
            # =========================================

            profitto_potenziale = reward * qty

            if profitto_potenziale < COMMISSIONI:

                 print(
                     f"❌ COMMISSION FAIL -> {ticker} | "
                     f"Profit={round(profitto_potenziale,2)}"
                 )

                 continue

            # =========================================
            # SALVA TRADE
            # =========================================

            active_trades[ticker] = {

                "side": side,

                "entry": price,

                "stop": stop,

                "target": target,

                "qty": qty,

                "risk": rischio_euro
            }

            # =========================================
            # TELEGRAM
            # =========================================

            if len(open_positions) >= MAX_TRADES:

                print("⚠️ MAX TRADES REACHED")

                continue

            qty = round(

                CAPITALE_PER_TRADE / price

            )

            send_telegram(

                f"🚀 {side} {ticker}\n\n"

                f"📊 Fase: {fase}\n\n"

                f"💰 Capitale: ${CAPITALE_PER_TRADE}\n"

                f"📦 Shares: {qty}\n\n"

                f"💵 Entry: {round(price,2)}\n"

                f"🛑 Stop: {round(stop,2)}\n"

                f"🎯 Target: {round(target,2)}\n"

                f"⚖️ R/R: {rr}\n\n"

                f"📈 RSI: {round(last['RSI'],1)}\n"

                f"📉 MACD: {'Bullish' if macd_buy else 'Bearish'}\n"

                f"🔥 GoldenCross: {'YES' if golden_cross else 'NO'}\n\n"

                f"🟢 Supporto: {round(support,2)}\n"

                f"🔴 Resistenza: {round(resistance,2)}\n\n"

                f"🌀 Pivot: {round(pivot,2)}\n"

                f"⬆️ R1: {round(r1,2)}\n"

                f"⬇️ S1: {round(s1,2)}\n\n"

                f"📐 Fib 0.382: {round(fib_382,2)}\n"

                f"📐 Fib 0.5: {round(fib_50,2)}\n"

                f"📐 Fib 0.618: {round(fib_618,2)}\n\n"

                f"📦 Volume Spike: YES"

            )

            open_positions.append(ticker)

            cooldown_tickers[ticker] = datetime.now()

            save_trade(

                ticker=ticker,

                side=side,

                entry=round(price, 2),

                exit_price=0,

                pnl=0,

                result="OPEN",

                rr=rr

            )


        # =========================================
        # GESTIONE TRADE
        # =========================================

        for t in list(active_trades.keys()):

            try:

                trade = active_trades[t]

                df_trade = yf.download(
                    t,
                    period="1d",
                    interval="5m",
                    progress=False
                )

                if df_trade is None or df_trade.empty:
                    continue

                if isinstance(df_trade.columns, pd.MultiIndex):
                    df_trade.columns = df_trade.columns.get_level_values(0)

                price_now = df_trade["Close"].iloc[-1]

                atr_now = compute_atr(df_trade).iloc[-1]

                # =========================================
                # BUY
                # =========================================

                if trade["side"] == "BUY":

                    if price_now >= trade["entry"] + atr_now:

                        trade["stop"] = max(
                            trade["stop"],
                            trade["entry"]
                        )

                    trade["stop"] = max(
                        trade["stop"],
                        price_now - atr_now * 3
                    )

                    # STOP BUY
                    if price_now <= trade["stop"]:

                        pnl = (
                            price_now - trade["entry"]
                        ) * trade["qty"]

                        stats["pnl"] += pnl

                        if pnl > 0:
                            stats["wins"] += 1
                        else:
                            stats["losses"] += 1

                        send_telegram(

                            f"❌ STOP BUY {t}\n"

                            f"Exit: {round(price_now,2)}\n"

                            f"PnL: {round(pnl,2)}€"
                        )

                        save_trade(

                            t,

                            trade["side"],

                            trade["entry"],

                            price_now,

                            pnl,

                            rr,

                            "STOP"
                        )

                        del active_trades[t]

                    # TARGET BUY
                    elif price_now >= trade["target"]:

                        pnl = (
                            price_now - trade["entry"]
                        ) * trade["qty"]

                        stats["pnl"] += pnl

                        stats["wins"] += 1

                        send_telegram(

                            f"💰 TARGET BUY {t}\n"

                            f"Exit: {round(price_now,2)}\n"

                            f"PnL: {round(pnl,2)}€"
                        )

                        save_trade(

                            t,

                            trade["side"],

                            trade["entry"],

                            price_now,

                            pnl,

                            rr,

                            "TARGET"
                        )

                        del active_trades[t]

                # =========================================
                # SELL
                # =========================================

                else:

                    if price_now <= trade["entry"] - atr_now:

                        trade["stop"] = min(
                            trade["stop"],
                            trade["entry"]
                        )

                    trade["stop"] = min(
                        trade["stop"],
                        price_now + atr_now * 3
                    )

                    # STOP SELL
                    if price_now >= trade["stop"]:

                        pnl = (
                            trade["entry"] - price_now
                        ) * trade["qty"]

                        stats["pnl"] += pnl

                        if pnl > 0:
                            stats["wins"] += 1
                        else:
                            stats["losses"] += 1

                        send_telegram(

                            f"❌ STOP SELL {t}\n"

                            f"Exit: {round(price_now,2)}\n"

                            f"PnL: {round(pnl,2)}€"
                        )

                        save_trade(

                            t,

                            trade["side"],

                            trade["entry"],

                            price_now,

                            pnl,

                            rr,

                            "STOP"
                        )

                        del active_trades[t]

                    # TARGET SELL
                    elif price_now <= trade["target"]:

                        pnl = (
                            trade["entry"] - price_now
                        ) * trade["qty"]

                        stats["pnl"] += pnl

                        stats["wins"] += 1

                        send_telegram(

                            f"💰 TARGET SELL {t}\n"

                            f"Exit: {round(price_now,2)}\n"

                            f"PnL: {round(pnl,2)}€"
                        )

                        save_trade(

                            t,

                            trade["side"],

                            trade["entry"],

                            price_now,

                            pnl,

                            rr,

                            "TARGET"
                        )

                        del active_trades[t]

            except Exception as e:

                print("Errore gestione trade:", e)

        # =========================================
        # STATS
        # =========================================

        total = stats["wins"] + stats["losses"]

        if total > 0:

            winrate = round(
                stats["wins"] / total * 100,
                1
            )

        else:

            winrate = 0

        print(

            f"💰 Equity: {round(equity,2)}€ | "

            f"PnL: {round(stats['pnl'],2)}€ | "

            f"Winrate: {winrate}% | "

            f"Attivi: {len(active_trades)}"
        )

        # =========================================
        # NEXT BATCH
        # =========================================

        index += MAX_TICKERS

        if index >= len(TICKERS):

            index = 0

        # =========================================
        # SLEEP
        # =========================================

        time.sleep(60)

    except Exception as e:

        print("❌ ERRORE:", e)

        time.sleep(300)


         
