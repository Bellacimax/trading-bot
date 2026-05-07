from stats import init_stats, save_trade
import yfinance as yf
import pandas as pd
import time
import requests
import os
import csv
from datetime import datetime, UTC
from scanner import rank_tickers

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
# LOOP PRINCIPALE
# =========================================

while True:

    try:

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

        # =========================================
        # SUBSET TICKER
        # =========================================

        subset = rank_tickers(TICKERS)

        print(f"🔥 Top Ranked: {subset}")

        # =========================================
        # LOOP TICKER
        # =========================================

        for ticker in subset:

            print(f"🔍 Analizzo {ticker}")

            # =========================================
            # DATI 5M
            # =========================================

            df = yf.download(

                ticker,

                period="5d",

                interval="5m",

                progress=False

            )

            if df is None or df.empty or len(df) < 50:

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
            # ATR
            # =========================================

            df["ATR"] = compute_atr(df)

            # =========================================
            # INDICATORI
            # =========================================

            df = compute_indicators(df)

            last = df.iloc[-1]

            price = last["Close"]

            atr = last["ATR"]

            if pd.isna(atr) or atr == 0:

                continue

            # =========================================
            # DATI 1H
            # =========================================

            df_htf = yf.download(

                ticker,

                period="1mo",

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

            if score_buy >= 3 and htf_up:

                side = "BUY"

                score = score_buy

                print(f"🟢 BUY READY -> {ticker}")

            elif score_sell >= 3 and htf_down:

                side = "SELL"

                score = score_sell

                print(f"🔴 SELL READY -> {ticker}")

            else:

                print(f"⚠️ SKIP -> {ticker}")

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

            if rr < 1.5:

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

            send_telegram(

                f"🚀 {side} {ticker}\n\n"

                f"📊 Fase: {fase}\n\n"

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

                    # break even
                    if price_now >= trade["entry"] + atr_now:

                        trade["stop"] = max(
                            trade["stop"],
                            trade["entry"]
                        )

                    # trailing stop
                    trade["stop"] = max(

                        trade["stop"],

                        price_now - atr_now * 1.5
                    )

                    # stop
                    if price_now <= trade["stop"]:

                        pnl = (
                            price_now
                            - trade["entry"]
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

                    # target
                    elif price_now >= trade["target"]:

                        pnl = (
                            price_now
                            - trade["entry"]
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

                        price_now + atr_now * 1.5
                    )

                    # =========================================
                    # STOP SELL
                    # =========================================

                    if price_now >= trade["stop"]:

                        pnl = (
                            trade["entry"]
                            - price_now
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

                    # =========================================
                    # TARGET SELL
                    # =========================================

                    elif price_now <= trade["target"]:

                        pnl = (
                            trade["entry"]
                            - price_now
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

        time.sleep(10)


         
