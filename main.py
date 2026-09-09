import random
import os
import csv
import time
import signal
import logging
import threading
import gc
from datetime import datetime, timezone, timedelta
import io
import base64
import requests
import json
import pandas as pd
import yfinance as yf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from flask import Flask, jsonify, render_template_string, send_file

# =========================================
# LOGGING
# =========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")

# =========================================
# ENV VARS
# =========================================
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
CAPITALE_PER_TRADE = int(os.getenv("CAPITALE_PER_TRADE", "1000"))
MAX_TRADES = int(os.getenv("MAX_TRADES", "5"))
MIN_VOLUME_RATIO = float(os.getenv("MIN_VOLUME_RATIO", "0.7"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "60"))
LOOP_INTERVAL = int(os.getenv("LOOP_INTERVAL", "180"))
SIGNAL_TIMEOUT_DAYS = int(os.getenv("SIGNAL_TIMEOUT_DAYS", "5"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "200"))
BOT_ENABLED = True

# =========================================
# FASE 1: ALERT, PAPER TRADING & SCREENER
# =========================================
PAPER_MODE = False
ALERTS_FILE = "price_alerts.json"
PAPER_HISTORY_FILE = "paper_trade_history.csv"
price_alerts = {}

# =========================================
# STATE
# =========================================
state_lock = threading.Lock()
active_trades = {}
cooldown_tickers = {}
traded_today = set()       # 🆕 Traccia i trade chiusi oggi
logged_today = set()       # 🆕 Traccia i ticker già loggati oggi (Anti-Duplicati)
bad_tickers = set()
stats = {"wins": 0, "losses": 0, "pnl": 0.0}
daily_stats = {"date": datetime.now().date(), "pnl": 0.0, "trades": 0}

# =========================================
# TICKERS
# =========================================
def load_tickers(path="tickers.txt"):
    if not os.path.exists(path):
        log.warning("tickers.txt non trovato, uso lista default")
        return ["AAPL", "MSFT", "NVDA"]
    with open(path) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]

TICKERS = load_tickers()
BLACKLIST = {"ARKK", "XBI", "UVXY", "SQQQ"}
MAX_TICKERS = int(os.getenv("MAX_TICKERS", "240"))
TICKERS = TICKERS[:MAX_TICKERS]
log.info(f"Caricati {len(TICKERS)} tickers")

# =========================================
# TELEGRAM
# =========================================
def send_telegram(msg: str, parse_mode="Markdown"):
    if not TOKEN or not CHAT_ID:
        log.warning("Telegram non configurato")
        return
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        r = requests.get(url, params={"chat_id": CHAT_ID, "text": msg, "parse_mode": parse_mode}, timeout=10)
        log.info(f"Telegram status: {r.status_code}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

def send_telegram_photo(image_bytes: bytes, caption: str = ""):
    if not TOKEN or not CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
        files = {'photo': ('chart.png', image_bytes, 'image/png')}
        data = {'chat_id': CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}
        requests.post(url, files=files, data=data, timeout=15)
    except Exception as e:
        log.error(f"Telegram photo error: {e}")

def send_telegram_document(file_path: str, caption: str = ""):
    if not TOKEN or not CHAT_ID: return
    if not os.path.exists(file_path): return
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendDocument"
        with open(file_path, 'rb') as f:
            files = {'document': (os.path.basename(file_path), f, 'text/csv')}
            data = {'chat_id': CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}
            requests.post(url, files=files, data=data, timeout=30)
    except Exception as e:
        log.error(f"Telegram document error: {e}")

# =========================================
# TRADE HISTORY
# =========================================
HISTORY_FILE = "trade_history.csv"

def save_trade(ticker, side, entry, exit_price, pnl, rr, result, exit_reason):
    """Salva il trade chiuso nel file trade_history.csv"""
    qty = max(1, int(CAPITALE_PER_TRADE / entry))
    pnl_pct = (pnl / (entry * qty)) * 100
    row = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "ticker": ticker,
        "side": side,
        "entry": round(entry, 2),
        "exit": round(exit_price, 2),
        "pnl": round(pnl, 2),
        "pnl_percent": round(pnl_pct, 2),
        "status": result,
        "exit_reason": exit_reason
    }
    exists = os.path.exists(HISTORY_FILE)
    pd.DataFrame([row]).to_csv(HISTORY_FILE, mode="a", header=not exists, index=False,
        columns=["date", "ticker", "side", "entry", "exit", "pnl", "pnl_percent", "status", "exit_reason"])

# =========================================
# SIGNALS LOG
# =========================================
SIGNALS_FILE = "signals_log.csv"

def log_signal(ticker, side, entry, stop, target):
    row = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "ticker": ticker, "side": side, "entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2),
        "result": "PENDING", "exit_price": "", "pnl": "", "pnl_pct": "", "exit_timestamp": "", "exit_reason": "",
    }
    exists = os.path.exists(SIGNALS_FILE)
    pd.DataFrame([row]).to_csv(SIGNALS_FILE, mode="a", header=not exists, index=False)
    log.info(f"📝 Segnale registrato: {side} {ticker} @ {entry}")

def get_signals_stats():
    if not os.path.exists(SIGNALS_FILE):
        return {"total": 0, "wins": 0, "losses": 0, "pending": 0, "expired": 0, "winrate": 0, "profit_factor": 0, "avg_win": 0, "avg_loss": 0, "total_pnl": 0, "signals": []}
    try:
        df = pd.read_csv(SIGNALS_FILE)
        if df.empty: return {"total": 0, "signals": []}
        if "result" not in df.columns: df["result"] = "PENDING"
        
        wins = df[df["result"] == "WIN"]; losses = df[df["result"] == "LOSS"]
        pending = df[df["result"] == "PENDING"]; expired = df[df["result"] == "EXPIRED"]
        total_closed = len(wins) + len(losses)
        winrate = round((len(wins) / total_closed) * 100, 1) if total_closed > 0 else 0
        avg_win = round(wins["pnl"].astype(float).mean(), 2) if len(wins) > 0 else 0
        avg_loss = round(abs(losses["pnl"].astype(float).mean()), 2) if len(losses) > 0 else 0
        profit_factor = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0
        total_pnl = round(df["pnl"].astype(float).sum(), 2) if "pnl" in df.columns else 0
        
        return {"total": len(df), "wins": len(wins), "losses": len(losses), "pending": len(pending), "expired": len(expired),
                "winrate": winrate, "profit_factor": profit_factor, "avg_win": avg_win, "avg_loss": avg_loss, "total_pnl": total_pnl, "signals": df.tail(50).to_dict("records")}
    except Exception as e:
        log.error(f"Error reading signals: {e}")
        return {"total": 0, "signals": []}

def monitor_signals():
    log.info("🔍 Signal monitor started (solo gestione TIMEOUT)")
    while not stop_event.is_set():
        try:
            if not os.path.exists(SIGNALS_FILE):
                stop_event.wait(600); continue
            df = pd.read_csv(SIGNALS_FILE)
            if df.empty or "result" not in df.columns:
                stop_event.wait(600); continue
            pending = df[df["result"] == "PENDING"]
            if pending.empty:
                stop_event.wait(600); continue
            
            now = datetime.now(timezone.utc)
            updated = False
            for idx, row in pending.iterrows():
                signal_time = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if (now - signal_time).days >= SIGNAL_TIMEOUT_DAYS:
                    df.at[idx, "result"] = "EXPIRED"
                    df.at[idx, "exit_reason"] = "TIMEOUT"
                    df.at[idx, "exit_timestamp"] = now.strftime("%Y-%m-%d %H:%M:%S")
                    updated = True
                    log.info(f"⏰ Segnale {row['ticker']} scaduto (TIMEOUT)")
            if updated:
                df.to_csv(SIGNALS_FILE, index=False)
        except Exception as e:
            log.error(f"Signal monitor error: {e}")
        stop_event.wait(600)

def load_active_trades_from_csv():
    """Recupera i trade PENDING dal CSV all'avvio del bot"""
    global active_trades, logged_today
    if not os.path.exists(SIGNALS_FILE):
        log.info("Nessun file signals_log.csv trovato")
        return
    try:
        df = pd.read_csv(SIGNALS_FILE)
        if df.empty: return
        
        pending = df[df['result'] == 'PENDING']
        if pending.empty: return
        
        loaded_count = 0
        for idx, row in pending.iterrows():
            ticker = str(row['ticker']).strip()
            if ticker not in active_trades:
                active_trades[ticker] = {
                    "side": str(row['side']).strip(),
                    "entry": float(row['entry']),
                    "stop": float(row['stop']),
                    "target": float(row['target']),
                    "qty": max(1, int(CAPITALE_PER_TRADE / float(row['entry']))),
                    "ts": str(row['timestamp'])
                }
                loaded_count += 1
                logged_today.add(ticker)
        
        if loaded_count > 0:
            log.info(f"♻️ Recupero {loaded_count} trade pendenti dal CSV all'avvio")
            send_telegram(f"♻️ Recuperati {loaded_count} trade pendenti all'avvio")
    except Exception as e:
        log.error(f"Errore recupero trade pendenti: {e}")

def update_signal_log(ticker, exit_price, pnl, result, exit_reason):
    """Aggiorna il file signals_log.csv quando un trade si chiude"""
    if not os.path.exists(SIGNALS_FILE): return
    try:
        df = pd.read_csv(SIGNALS_FILE)
        mask = (df['ticker'] == ticker) & (df['result'] == 'PENDING')
        if mask.any():
            idx = df[mask].index[-1]
            df.at[idx, 'result'] = result
            df.at[idx, 'exit_price'] = round(exit_price, 2)
            df.at[idx, 'pnl'] = round(pnl, 2)
            entry = float(df.at[idx, 'entry'])
            qty = max(1, int(CAPITALE_PER_TRADE / entry))
            pnl_pct = (pnl / (entry * qty)) * 100
            df.at[idx, 'pnl_pct'] = round(pnl_pct, 2)
            df.at[idx, 'exit_timestamp'] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            df.at[idx, 'exit_reason'] = exit_reason
            df.to_csv(SIGNALS_FILE, index=False)
            log.info(f"📝 signals_log.csv aggiornato per {ticker}: {result}")
    except Exception as e:
        log.error(f"Errore aggiornamento signals_log: {e}")

# =========================================
# MARKET FILTER
# =========================================
def check_market_conditions():
    try:
        if TWELVE_DATA_API_KEY:
            # Usa Twelve Data per SPY
            url_spy = f"https://api.twelvedata.com/time_series?symbol=SPY&interval=1day&outputsize=5&apikey={TWELVE_DATA_API_KEY}"
            r_spy = requests.get(url_spy, timeout=10)
            data_spy = r_spy.json()
            
            if "values" not in data_spy or len(data_spy["values"]) < 2:
                return True, "Dati SPY non disponibili"
            
            spy_last = float(data_spy["values"][0]["close"])
            spy_prev = float(data_spy["values"][1]["close"])
            spy_change = ((spy_last - spy_prev) / spy_prev) * 100
            
            # Usa Twelve Data per VIX
            url_vix = f"https://api.twelvedata.com/time_series?symbol=VIX&interval=1day&outputsize=1&apikey={TWELVE_DATA_API_KEY}"
            r_vix = requests.get(url_vix, timeout=10)
            data_vix = r_vix.json()
            
            vix_level = 20.0
            if "values" in data_vix and len(data_vix["values"]) > 0:
                vix_level = float(data_vix["values"][0]["close"])
        else:
            # Fallback a Yahoo Finance (solo se non hai Twelve Data)
            time.sleep(2)
            spy = yf.Ticker("SPY").history(period="5d")
            if spy.empty or len(spy) < 2:
                return True, "Dati SPY non disponibili"
            time.sleep(2)
            spy_last = float(spy['Close'].iloc[-1])
            spy_prev = float(spy['Close'].iloc[-2])
            spy_change = ((spy_last - spy_prev) / spy_prev) * 100
            
            vix = yf.Ticker("^VIX").history(period="5d")
            vix_level = float(vix['Close'].iloc[-1]) if not vix.empty else 20.0
        
        if spy_change < -2.0:
            return False, f" Mercato in forte ribasso ({spy_change:.2f}%)"
        elif vix_level > 30:
            return False, f"🔴 Volatilità troppo alta (VIX: {vix_level:.1f})"
        elif spy_change < -1.0:
            return True, f"🟡 Mercato in leggero ribasso ({spy_change:.2f}%) - Cautela"
        else:
            return True, f"🟢 Mercato OK (SPY: {spy_change:+.2f}%, VIX: {vix_level:.1f})"
    except Exception as e:
        log.error(f"Error checking market conditions: {e}")
        return True, "Errore controllo mercato - Procedo in sicurezza"

# =========================================
# DAILY REPORT
# =========================================
def send_daily_report():
    log.info("📊 Generazione report giornaliero...")
    today = datetime.now().date()
    trades_today = pnl_today = wins_today = losses_today = 0
    
    if os.path.exists(HISTORY_FILE):
        try:
            df = pd.read_csv(HISTORY_FILE)
            if not df.empty and "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"]); df["date_only"] = df["date"].dt.date
                today_trades = df[df["date_only"] == today]
                trades_today = len(today_trades); pnl_today = today_trades["pnl"].sum()
                wins_today = len(today_trades[today_trades["status"] == "WIN"])
                losses_today = len(today_trades[today_trades["status"] == "LOSS"])
        except Exception as e: log.error(f"Error reading history for report: {e}")
    
    winrate = round((wins_today / trades_today) * 100, 1) if trades_today > 0 else 0
    msg = (f"📊 *REPORT GIORNALIERO*\n━━━━━━━━━━━━━━━━━━\n📅 Data: {today.strftime('%d/%m/%Y')}\n"
           f"🎯 Trade: {trades_today}\n✅ Wins: {wins_today}\n❌ Losses: {losses_today}\n📈 Winrate: {winrate}%\n"
           f"💰 PnL Oggi: {round(pnl_today, 2)} €\n━━━━━━━━━━━━━━━━━━\n💵 PnL Totale: {round(stats['pnl'], 2)} €\n"
           f"🏆 Record: {stats['wins']}W - {stats['losses']}L")
    send_telegram(msg)
    
    if os.path.exists(SIGNALS_FILE):
        send_telegram_document(SIGNALS_FILE, f"💾 Backup segnali del {today.strftime('%d/%m/%Y')}")
    if os.path.exists(HISTORY_FILE):
        send_telegram_document(HISTORY_FILE, f"💾 Backup trade history del {today.strftime('%d/%m/%Y')}")
    
    equity_img = generate_equity_curve()
    if equity_img: send_telegram_photo(equity_img, "📈 Equity Curve - Andamento PnL")

def daily_report_loop():
    log.info("📊 Daily report scheduler started")
    last_report_date = None
    while not stop_event.is_set():
        try:
            now = datetime.now()
            if now.hour == 22 and now.minute == 0 and last_report_date != now.date():
                send_daily_report(); last_report_date = now.date()
                with state_lock:
                    daily_stats["date"] = now.date(); daily_stats["pnl"] = 0.0; daily_stats["trades"] = 0
                    traded_today.clear(); logged_today.clear()
                if hasattr(download_ticker, 'cache'): download_ticker.cache.clear()
                gc.collect()
        except Exception as e: log.error(f"Daily report error: {e}")
        stop_event.wait(60)

def keep_alive_loop():
    log.info("🔄 Keep-alive loop started")
    while not stop_event.is_set():
        try:
            requests.get("http://localhost:10000/health", timeout=5)
            log.info("🔄 Keep-alive ping OK")
        except: pass
        stop_event.wait(300)

# =========================================
# TELEGRAM COMMANDS
# =========================================
def handle_telegram_commands():
    log.info("🤖 Telegram commands handler started")
    last_update_id = None
    while not stop_event.is_set():
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
            r = requests.get(url, params={"offset": last_update_id, "timeout": 10}, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        last_update_id = update["update_id"] + 1
                        text = update.get("message", {}).get("text", "")
                        if text.startswith("/"): handle_command(text)
        except: pass
        stop_event.wait(5)

def handle_command(text: str):
    global BOT_ENABLED, PAPER_MODE
    cmd = text.lower().strip()
    if cmd == "/status":
        _, market_msg = check_market_conditions()
        send_telegram(f"🤖 *STATO BOT*\n━━━━━━━━━━━━━━━━━━\n{'🟢 ATTIVO' if BOT_ENABLED else '🔴 DISATTIVATO'}\n"
                      f"Modalità: {'PAPER 📝' if PAPER_MODE else 'REAL 💰'}\n📊 Trade attivi: {len(active_trades)}/{MAX_TRADES}\n"
                      f"💰 PnL totale: {round(stats['pnl'], 2)} €\n🏆 Record: {stats['wins']}W - {stats['losses']}L\n━━━━━━━━━━━━━━━━━━\n{market_msg}")
    elif cmd == "/trades":
        if not active_trades:
            send_telegram("📊 *TRADE ATTIVI*\n━━━━━━━━━━━━━━━━━━\nNessun trade aperto al momento")
            return
        msg = f"📊 *TRADE ATTIVI ({len(active_trades)}/{MAX_TRADES})*\n━━━━━━━━━━━━━━━━━━\n"
        for ticker, pos in active_trades.items():
            emoji = "🟢" if pos["side"] == "BUY" else "🔴"
            msg += f"{emoji} *{pos['side']} {ticker}*\n  💰 Entry: {round(pos['entry'], 2)}\n  🛑 Stop: {round(pos['stop'], 2)}\n" \
                   f"  🎯 Target: {round(pos['target'], 2)}\n  📦 Qty: {pos['qty']} azioni\n  ⏰ Aperto: {pos['ts'][:16]}\n━━━━━━━━━━━━━━━━━━\n"
        send_telegram(msg)
    elif cmd == "/stop": BOT_ENABLED = False; send_telegram("🔴 Bot fermato")
    elif cmd == "/start": BOT_ENABLED = True; send_telegram("🟢 Bot avviato")
    elif cmd == "/help": send_telegram("🤖 *COMANDI*\n/status, /trades, /start, /stop, /backup, /help")

# =========================================
# INDICATORS
# =========================================
def compute_atr(df, period=14):
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def compute_indicators(df):
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()
    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = gain / loss
    df["RSI"] = 100 - (100 / (1 + rs))
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    return df

def check_price_alerts(ticker, current_price):
    if ticker in price_alerts:
        target = price_alerts[ticker]
        if current_price >= target or current_price <= target:
            send_telegram(f"🔔 *ALERT PREZZO*\n━━━━━━━━━━━━━━━━━━\n{ticker} ha raggiunto ${target}!\n💰 Prezzo attuale: ${current_price:.2f}")
            del price_alerts[ticker]
            with open(ALERTS_FILE, 'w') as f: json.dump(price_alerts, f, indent=2)

def calculate_support_resistance(df):
    last = df.iloc[-1]; prev = df.iloc[-2] if len(df) > 1 else last
    H, L, C = float(prev["High"]), float(prev["Low"]), float(prev["Close"])
    PP = (H + L + C) / 3
    S1, R1 = (2 * PP) - H, (2 * PP) - L
    S2, R2 = PP - (H - L), PP + (H - L)
    recent = df.tail(min(20, len(df) - 1))
    swing_high, swing_low = float(recent["High"].max()), float(recent["Low"].min())
    price = float(last["Close"]); atr = float(last["ATR"]) if "ATR" in last else (price * 0.02)
    supports = sorted([s for s in [S1, S2, swing_low] if s < price], reverse=True)[:3]
    resistances = sorted([r for r in [R1, R2, swing_high] if r > price])[:3]
    while len(supports) < 3: supports.append(price - atr * (len(supports) + 2))
    while len(resistances) < 3: resistances.append(price + atr * (len(resistances) + 2))
    return {"S1": round(supports[0], 2), "S2": round(supports[1], 2), "R1": round(resistances[0], 2), "R2": round(resistances[1], 2)}

def generate_equity_curve():
    if not os.path.exists(HISTORY_FILE): return None
    try:
        df = pd.read_csv(HISTORY_FILE)
        if df.empty or len(df) < 2: return None
        df["date"] = pd.to_datetime(df["date"]); df = df.sort_values("date")
        df["cumulative_pnl"] = df["pnl"].cumsum()
        fig, ax = plt.subplots(figsize=(12, 6))
        ax.plot(df["date"], df["cumulative_pnl"], color="#10b981", linewidth=2)
        ax.fill_between(df["date"], df["cumulative_pnl"], alpha=0.3, color="#10b981")
        ax.axhline(y=0, color="#64748b", linestyle="--", linewidth=1, alpha=0.5)
        fig.patch.set_facecolor('#0f172a'); ax.set_facecolor('#1e293b')
        img_buf = io.BytesIO(); plt.savefig(img_buf, format='png', dpi=100, bbox_inches='tight', facecolor=fig.get_facecolor())
        img_buf.seek(0); plt.close(); return img_buf.getvalue()
    except: return None

# =========================================
# CHART GENERATION
# =========================================
def generate_chart_image(ticker: str, df, side: str, entry: float, stop: float, target: float):
    try:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), height_ratios=[3, 1], gridspec_kw={'hspace': 0.3})
        ax1.plot(df.index[-50:], df["Close"].iloc[-50:], label="Close", linewidth=1.5)
        ax1.axhline(y=entry, color="blue", linestyle="--", linewidth=2, label=f"Entry: {entry:.2f}")
        ax1.axhline(y=stop, color="red", linestyle="--", linewidth=2, label=f"Stop: {stop:.2f}")
        ax1.axhline(y=target, color="green", linestyle="--", linewidth=2, label=f"Target: {target:.2f}")
        ax1.set_title(f"{ticker} - {side}", fontsize=14, fontweight="bold"); ax1.legend(loc="upper left"); ax1.grid(True, alpha=0.3)
        colors = ["green" if df["Close"].iloc[i] >= df["Open"].iloc[i] else "red" for i in range(-50, 0)]
        ax2.bar(df.index[-50:], df["Volume"].iloc[-50:], color=colors, alpha=0.6); ax2.set_ylabel("Volume"); ax2.grid(True, alpha=0.3)
        img_buf = io.BytesIO(); plt.savefig(img_buf, format='png', dpi=100, bbox_inches='tight'); img_buf.seek(0); plt.close()
        return img_buf.getvalue()
    except: return None

# =========================================
# DATA DOWNLOAD (🆕 OTTIMIZZATA PER MEMORIA)
# =========================================
def download_ticker(ticker: str, max_retries=3):
    cache_key = f"{ticker}_{datetime.now().date()}"
    if not hasattr(download_ticker, 'cache'): download_ticker.cache = {}
    
    # 🆕 FIX MEMORIA CRITICO: Limita la cache agli ultimi 60 ticker.
    if len(download_ticker.cache) > 60:
        oldest_keys = list(download_ticker.cache.keys())[:len(download_ticker.cache) - 60]
        for key in oldest_keys: del download_ticker.cache[key]
    
    if cache_key in download_ticker.cache: return download_ticker.cache[cache_key]
    
    if TWELVE_DATA_API_KEY:
        url = f"https://api.twelvedata.com/time_series?symbol={ticker}&interval=1day&outputsize=180&apikey={TWELVE_DATA_API_KEY}"
        for attempt in range(max_retries):
            try:
                r = requests.get(url, timeout=(5, 15)); r.raise_for_status(); data = r.json()
                if "values" in data:
                    rows = [{"Open": float(x["open"]), "High": float(x["high"]), "Low": float(x["low"]), "Close": float(x["close"]), "Volume": float(x["volume"])} for x in reversed(data["values"])]
                    df = pd.DataFrame(rows); download_ticker.cache[cache_key] = df; return df
            except:
                if attempt < max_retries - 1: time.sleep(2 ** attempt)
                continue
    
    for attempt in range(max_retries):
        try:
            time.sleep(1 + random.random() * 2)
            # 🆕 FIX MEMORIA: Ridotto periodo da "1y" a "6mo"
            df = yf.download(ticker, period="6mo", interval="1d", progress=False)
            if df.empty: return None
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]
            download_ticker.cache[cache_key] = df; return df
        except Exception as e:
            if attempt < max_retries - 1: time.sleep(2 ** attempt + random.random())
            else: log.error(f"Download error {ticker}: {e}"); return None
    return None

# =========================================
# POSITION MONITOR (UNIFICATA E CORRETTA)
# =========================================
def check_positions(df_by_ticker):
    """Controlla se i trade attivi hanno toccato Stop o Target"""
    to_close = []
    with state_lock:
        for ticker, pos in list(active_trades.items()):
            df = df_by_ticker.get(ticker)
            if df is not None and not df.empty:
                check_price_alerts(ticker, float(df.iloc[-1]["Close"]))
            if df is None or df.empty: continue
            
            last = df.iloc[-1]; high, low = float(last["High"]), float(last["Low"])
            hit, exit_price = None, None
            
            if pos["side"] == "BUY":
                if low <= pos["stop"]: hit, exit_price = "STOP", pos["stop"]
                elif high >= pos["target"]: hit, exit_price = "TARGET", pos["target"]
            else:
                if high >= pos["stop"]: hit, exit_price = "STOP", pos["stop"]
                elif low <= pos["target"]: hit, exit_price = "TARGET", pos["target"]
            
            if hit:
                qty = pos["qty"]
                pnl = (exit_price - pos["entry"]) * qty if pos["side"] == "BUY" else (pos["entry"] - exit_price) * qty
                result = "WIN" if pnl > 0 else "LOSS"
                to_close.append((ticker, pos, exit_price, pnl, result, hit))
    
    for ticker, pos, exit_price, pnl, result, hit in to_close:
        with state_lock:
            active_trades.pop(ticker, None)
            traded_today.add(ticker)
            stats["wins" if result == "WIN" else "losses"] += 1
            stats["pnl"] += pnl; daily_stats["pnl"] += pnl; daily_stats["trades"] += 1
            
            save_trade(ticker, pos["side"], pos["entry"], exit_price, pnl, 2, result, hit)
            update_signal_log(ticker, exit_price, pnl, result, hit)
            
        send_telegram(f"{'✅' if result=='WIN' else '❌'} *{hit} {pos['side']} {ticker}*\nEntry: {round(pos['entry'],2)} | Exit: {round(exit_price,2)}\n💵 PnL: {round(pnl, 2)} €")
        log.info(f"{hit} {ticker} -> {result} ({round(pnl, 2)}€)")
        
        if daily_stats["pnl"] <= -MAX_DAILY_LOSS:
            global BOT_ENABLED; BOT_ENABLED = False
            send_telegram(f"🚨 *MAX DAILY LOSS*\nPerdita giornaliera massima raggiunta: {round(daily_stats['pnl'], 2)}€\nBot fermato.")

# =========================================
# SIGNAL GENERATOR
# =========================================
def analyze_ticker(ticker, df):
    if not BOT_ENABLED or ticker in bad_tickers or not ticker.isalpha() or len(ticker) > 5: return
    
    with state_lock:
        if ticker in logged_today: return  # 🆕 Anti-Duplicati
        if ticker in cooldown_tickers and (datetime.now() - cooldown_tickers[ticker]).total_seconds() / 60 < COOLDOWN_MINUTES: return
    
    if df is None or len(df) < 50: return
    df["ATR"] = compute_atr(df); df = compute_indicators(df)
    last = df.iloc[-1]
    if pd.isna(last["ATR"]) or last["ATR"] == 0: return
    
    price, atr = float(last["Close"]), float(last["ATR"])
    score_buy, score_sell = 0, 0
    if last["EMA50"] > last["EMA200"]: score_buy += 1
    else: score_sell += 1
    if last["RSI"] > 55: score_buy += 1
    elif last["RSI"] < 45: score_sell += 1
    if last["MACD"] > last["MACD_signal"]: score_buy += 1
    else: score_sell += 1
    
    vol_mean = df["Volume"].rolling(20).mean().iloc[-1]
    volume_ratio = df["Volume"].iloc[-1] / vol_mean if vol_mean else 0
    
    side = None
    if score_buy >= 2 and volume_ratio > MIN_VOLUME_RATIO: side = "BUY"
    elif score_sell >= 2 and volume_ratio > MIN_VOLUME_RATIO: side = "SELL"
    if not side: return
    
    stop = price - atr if side == "BUY" else price + atr
    target = price + (atr * 2) if side == "BUY" else price - (atr * 2)
    qty = max(1, int(CAPITALE_PER_TRADE / price))
    
    # 🆕 CONTROLLO SLOT PRIMA DI REGISTRARE NEL CSV
    with state_lock:
        if ticker in active_trades or len(active_trades) >= MAX_TRADES:
            log.info(f"⏸️ Slot pieni ({len(active_trades)}/{MAX_TRADES}) - Skip {ticker}")
            return
        
        log_signal(ticker, side, price, stop, target)
        logged_today.add(ticker)
        active_trades[ticker] = {"side": side, "entry": price, "stop": stop, "target": target, "qty": qty, "ts": datetime.now().isoformat()}
        cooldown_tickers[ticker] = datetime.now()
    
    sr = calculate_support_resistance(df)
    msg = (f"🚀 *{side} {ticker}*\n━━━━━━━━━━━━━━━━━━\n💰 *Entry:* {round(price, 2)}\n🛑 *Stop:* {round(stop, 2)}\n"
           f"🎯 *Target:* {round(target, 2)}\n📊 *R/R:* 1:2 | 💼 *Qty:* {qty}\n━━━━━━━━━━━━━━━━━━\n"
           f"📈 *S1:* {sr['S1']} | *S2:* {sr['S2']}\n📉 *R1:* {sr['R1']} | *R2:* {sr['R2']}")
    send_telegram(msg)
    
    chart_img = generate_chart_image(ticker, df, side, price, stop, target)
    if chart_img: send_telegram_photo(chart_img, f"📊 {side} {ticker} @ {round(price, 2)}")
    log.info(f"🚀 {side} {ticker} @ {round(price, 2)}")

# =========================================
# TRADING LOOP
# =========================================
stop_event = threading.Event()

def trading_loop():
    log.info("Trading loop started")
    idx = 0
    while not stop_event.is_set():
        try:
            now_utc = datetime.now(timezone.utc); now_ny = now_utc - timedelta(hours=4)
            if now_ny.weekday() >= 5 or not (9 <= now_ny.hour < 16):
                stop_event.wait(600); continue
            
            market_ok, _ = check_market_conditions()
            if not market_ok: stop_event.wait(600); continue
            
            subset = [t for t in TICKERS if t not in BLACKLIST]
            batch_size = 25; batch = subset[idx * batch_size : (idx + 1) * batch_size]
            if not batch: idx = 0; batch = subset[:batch_size]
            idx += 1
            
            log.info(f"📊 Analisi batch {idx}: {len(batch)} ticker")
            df_by_ticker = {}; threads = []
            
            def download_single(t):
                df = download_ticker(t)
                if df is not None:
                    with state_lock: df_by_ticker[t] = df
            
            for t in batch:
                thread = threading.Thread(target=download_single, args=(t,))
                threads.append(thread); thread.start(); time.sleep(0.5)
            for thread in threads: thread.join(timeout=60)
            
            check_positions(df_by_ticker)
            for t in batch:
                if t in df_by_ticker:
                    try: analyze_ticker(t, df_by_ticker[t])
                    except Exception as e: log.error(f"Error {t}: {e}")
            
            df_by_ticker.clear(); gc.collect()
            stop_event.wait(180)
        except Exception as e: log.error(f"Loop error: {e}"); stop_event.wait(60)

# =========================================
# FLASK APP (Semplificata per stabilità)
# =========================================
app = Flask(__name__)

@app.route("/health")
def health(): return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})

@app.route("/")
def home():
    with state_lock:
        total = stats["wins"] + stats["losses"]
        winrate = round((stats["wins"] / total) * 100, 1) if total > 0 else 0
        return jsonify({"status": "ok", "active_trades": len(active_trades), "max_trades": MAX_TRADES, "pnl": stats["pnl"], "winrate": winrate})

# =========================================
# SHUTDOWN & MAIN
# =========================================
def handle_sigterm(*_):
    log.info("Shutting down..."); stop_event.set()

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

if __name__ == "__main__":
    log.info("🚀 BOT AVVIATO (Versione 13 - Fix Memoria & Recupero Trade)")
    load_active_trades_from_csv()  # 🆕 RECUPERA I TRADE APERTI DAL CSV
    send_telegram("🚀 BOT ONLINE - Versione 13\n\n✅ Twelve Data API attiva\n✅ Recupero trade pendenti all'avvio\n✅ Monitoraggio Stop/Target attivo\n✅ Anti-duplicati in memoria")
    
    threading.Thread(target=trading_loop, daemon=True).start()
    threading.Thread(target=monitor_signals, daemon=True).start()
    threading.Thread(target=daily_report_loop, daemon=True).start()
    threading.Thread(target=handle_telegram_commands, daemon=True).start()
    threading.Thread(target=keep_alive_loop, daemon=True).start()
    
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
