import random
import os
import csv
import time
import signal
import logging
import threading
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
        r = requests.get(url, params={
            "chat_id": CHAT_ID,
            "text": msg,
            "parse_mode": parse_mode
        }, timeout=10)
        log.info(f"Telegram status: {r.status_code}")
    except Exception as e:
        log.error(f"Telegram error: {e}")

def send_telegram_photo(image_bytes: bytes, caption: str = ""):
    if not TOKEN or not CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
        files = {'photo': ('chart.png', image_bytes, 'image/png')}
        data = {'chat_id': CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}
        r = requests.post(url, files=files, data=data, timeout=15)
        log.info(f"Telegram photo status: {r.status_code}")
    except Exception as e:
        log.error(f"Telegram photo error: {e}")

def send_telegram_document(file_path: str, caption: str = ""):
    if not TOKEN or not CHAT_ID:
        log.warning("Telegram non configurato")
        return
    if not os.path.exists(file_path):
        log.warning(f"File non trovato: {file_path}")
        return
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendDocument"
        with open(file_path, 'rb') as f:
            files = {'document': (os.path.basename(file_path), f, 'text/csv')}
            data = {'chat_id': CHAT_ID, 'caption': caption, 'parse_mode': 'Markdown'}
            r = requests.post(url, files=files, data=data, timeout=30)
        log.info(f"Telegram document status: {r.status_code}")
    except Exception as e:
        log.error(f"Telegram document error: {e}")

# =========================================
# TRADE HISTORY
# =========================================
HISTORY_FILE = "trade_history.csv"

def save_trade(ticker, side, entry, exit_price, pnl, rr, result):
    row = {
        "ticker": ticker,
        "side": side,
        "entry": round(entry, 2),
        "exit": round(exit_price, 2),
        "pnl": round(pnl, 2),
        "rr": rr,
        "result": result,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    exists = os.path.exists(HISTORY_FILE)
    pd.DataFrame([row]).to_csv(HISTORY_FILE, mode="a", header=not exists, index=False)

# =========================================
# SIGNALS LOG
# =========================================
SIGNALS_FILE = "signals_log.csv"

def log_signal(ticker, side, entry, stop, target):
    row = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "ticker": ticker,
        "side": side,
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "result": "PENDING",
        "exit_price": "",
        "pnl": "",
        "pnl_pct": "",
        "exit_timestamp": "",
        "exit_reason": "",
    }
    exists = os.path.exists(SIGNALS_FILE)
    pd.DataFrame([row]).to_csv(SIGNALS_FILE, mode="a", header=not exists, index=False)
    log.info(f"📝 Segnale registrato: {side} {ticker} @ {entry}")

def get_signals_stats():
    if not os.path.exists(SIGNALS_FILE):
        return {"total": 0, "wins": 0, "losses": 0, "pending": 0, "expired": 0,
                "winrate": 0, "profit_factor": 0, "avg_win": 0, "avg_loss": 0,
                "total_pnl": 0, "signals": []}
    try:
        df = pd.read_csv(SIGNALS_FILE)
        if df.empty:
            return {"total": 0, "signals": []}
        
        if "result" not in df.columns:
            df["result"] = "PENDING"
        
        wins = df[df["result"] == "WIN"]
        losses = df[df["result"] == "LOSS"]
        pending = df[df["result"] == "PENDING"]
        expired = df[df["result"] == "EXPIRED"]
        total_closed = len(wins) + len(losses)
        winrate = round((len(wins) / total_closed) * 100, 1) if total_closed > 0 else 0
        avg_win = round(wins["pnl"].astype(float).mean(), 2) if len(wins) > 0 else 0
        avg_loss = round(abs(losses["pnl"].astype(float).mean()), 2) if len(losses) > 0 else 0
        profit_factor = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0
        total_pnl = round(df["pnl"].astype(float).sum(), 2) if "pnl" in df.columns else 0
        
        return {
            "total": len(df),
            "wins": len(wins),
            "losses": len(losses),
            "pending": len(pending),
            "expired": len(expired),
            "winrate": winrate,
            "profit_factor": profit_factor,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "total_pnl": total_pnl,
            "signals": df.tail(50).to_dict("records"),
        }
    except Exception as e:
        log.error(f"Error reading signals: {e}")
        return {"total": 0, "signals": []}

def monitor_signals():
    log.info("🔍 Signal monitor started")
    while not stop_event.is_set():
        try:
            if not os.path.exists(SIGNALS_FILE):
                stop_event.wait(300)
                continue
            
            df = pd.read_csv(SIGNALS_FILE)
            if df.empty:
                stop_event.wait(300)
                continue
            
            if "result" not in df.columns:
                df["result"] = "PENDING"
            
            pending = df[df["result"] == "PENDING"]
            if pending.empty:
                stop_event.wait(600)
                continue
            
            now = datetime.now(timezone.utc)
            for idx, row in pending.iterrows():
                ticker = row["ticker"]
                side = row["side"]
                entry = float(row["entry"])
                stop = float(row["stop"])
                target = float(row["target"])
                signal_time = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                
                if (now - signal_time).days >= SIGNAL_TIMEOUT_DAYS:
                    df.at[idx, "result"] = "EXPIRED"
                    df.at[idx, "exit_reason"] = "TIMEOUT"
                    df.at[idx, "exit_timestamp"] = now.strftime("%Y-%m-%d %H:%M:%S")
                    continue
                
                try:
                    current_df = download_ticker(ticker)
                    if current_df is None or current_df.empty:
                        continue
                    last = current_df.iloc[-1]
                    high, low = float(last["High"]), float(last["Low"])
                except Exception as e:
                    log.warning(f"Error downloading {ticker} for signal monitor: {e}")
                    continue
                
                hit = None
                exit_price = None
                exit_reason = None
                
                if side == "BUY":
                    if low <= stop:
                        hit, exit_price, exit_reason = "LOSS", stop, "STOP"
                    elif high >= target:
                        hit, exit_price, exit_reason = "WIN", target, "TARGET"
                else:
                    if high >= stop:
                        hit, exit_price, exit_reason = "LOSS", stop, "STOP"
                    elif low <= target:
                        hit, exit_price, exit_reason = "WIN", target, "TARGET"
                
                if hit:
                    pnl = (exit_price - entry) if side == "BUY" else (entry - exit_price)
                    pnl_pct = (pnl / entry) * 100
                    df.at[idx, "result"] = hit
                    df.at[idx, "exit_price"] = round(exit_price, 2)
                    df.at[idx, "pnl"] = round(pnl, 2)
                    df.at[idx, "pnl_pct"] = round(pnl_pct, 2)
                    df.at[idx, "exit_timestamp"] = now.strftime("%Y-%m-%d %H:%M:%S")
                    df.at[idx, "exit_reason"] = exit_reason
                    log.info(f"✅ Segnale chiuso: {side} {ticker} -> {hit} ({round(pnl, 2)}€)")
            
            df.to_csv(SIGNALS_FILE, index=False)
        except Exception as e:
            log.error(f"Signal monitor error: {e}")
        stop_event.wait(600)

# =========================================
# MARKET FILTER
# =========================================
def check_market_conditions():
    try:
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
            return False, f"🔴 Mercato in forte ribasso ({spy_change:.2f}%)"
        elif vix_level > 30:
            return False, f" Volatilità troppo alta (VIX: {vix_level:.1f})"
        elif spy_change < -1.0:
            return True, f"🟡 Mercato in leggero ribasso ({spy_change:.2f}%) - Cautela"
        else:
            return True, f" Mercato OK (SPY: {spy_change:+.2f}%, VIX: {vix_level:.1f})"
    except Exception as e:
        log.error(f"Error checking market conditions: {e}")
        return True, "Errore controllo mercato - Procedo in sicurezza"

# =========================================
# DAILY REPORT
# =========================================
def send_daily_report():
    log.info("📊 Generazione report giornaliero...")
    today = datetime.now().date()
    trades_today = 0
    pnl_today = 0.0
    wins_today = 0
    losses_today = 0
    
    if os.path.exists(HISTORY_FILE):
        try:
            df = pd.read_csv(HISTORY_FILE)
            if not df.empty and "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
                df["date_only"] = df["date"].dt.date
                today_trades = df[df["date_only"] == today]
                trades_today = len(today_trades)
                pnl_today = today_trades["pnl"].sum()
                wins_today = len(today_trades[today_trades["result"] == "WIN"])
                losses_today = len(today_trades[today_trades["result"] == "LOSS"])
        except Exception as e:
            log.error(f"Error reading history for report: {e}")
    
    winrate = round((wins_today / trades_today) * 100, 1) if trades_today > 0 else 0
    
    msg = (
        f"📊 *REPORT GIORNALIERO*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 Data: {today.strftime('%d/%m/%Y')}\n"
        f"🎯 Trade: {trades_today}\n"
        f"✅ Wins: {wins_today}\n"
        f"❌ Losses: {losses_today}\n"
        f" Winrate: {winrate}%\n"
        f"💰 PnL Oggi: {round(pnl_today, 2)} €\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💵 PnL Totale: {round(stats['pnl'], 2)} €\n"
        f"🏆 Record: {stats['wins']}W - {stats['losses']}L"
    )
    send_telegram(msg)
    log.info(f"📊 Report giornaliero inviato: {trades_today} trade, PnL: {round(pnl_today, 2)}€")
    
    if os.path.exists(SIGNALS_FILE):
        try:
            df_signals = pd.read_csv(SIGNALS_FILE)
            total_signals = len(df_signals)
            pending = len(df_signals[df_signals["result"] == "PENDING"]) if "result" in df_signals.columns else 0
            wins = len(df_signals[df_signals["result"] == "WIN"]) if "result" in df_signals.columns else 0
            losses = len(df_signals[df_signals["result"] == "LOSS"]) if "result" in df_signals.columns else 0
            
            backup_msg = (
                f" *BACKUP SEGNALI*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📝 Totale segnali: {total_signals}\n"
                f"✅ WIN: {wins}\n"
                f"❌ LOSS: {losses}\n"
                f"⏳ PENDING: {pending}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📎 In allegato il file CSV completo"
            )
            send_telegram(backup_msg)
            send_telegram_document(SIGNALS_FILE, f"💾 Backup segnali del {today.strftime('%d/%m/%Y')}")
            log.info(f"💾 Backup CSV inviato: {total_signals} segnali")
        except Exception as e:
            log.error(f"Error sending CSV backup: {e}")
    else:
        send_telegram("💾 Nessun segnale registrato oggi")
    
    if os.path.exists(HISTORY_FILE):
        try:
            send_telegram_document(HISTORY_FILE, f" Backup trade history del {today.strftime('%d/%m/%Y')}")
            log.info("💾 Backup trade history inviato")
        except Exception as e:
            log.error(f"Error sending trade history backup: {e}")
    
    equity_img = generate_equity_curve()
    if equity_img:
        send_telegram_photo(equity_img, "📈 Equity Curve - Andamento PnL")
        log.info("📈 Equity curve inviata")

def daily_report_loop():
    log.info("📊 Daily report scheduler started")
    last_report_date = None
    while not stop_event.is_set():
        try:
            now = datetime.now()
            if now.hour == 22 and now.minute == 0 and last_report_date != now.date():
                send_daily_report()
                last_report_date = now.date()
                with state_lock:
                    daily_stats["date"] = now.date()
                    daily_stats["pnl"] = 0.0
                    daily_stats["trades"] = 0
        except Exception as e:
            log.error(f"Daily report error: {e}")
        stop_event.wait(60)

def keep_alive_loop():
    log.info("🔄 Keep-alive loop started")
    while not stop_event.is_set():
        try:
            requests.get("http://localhost:10000/health", timeout=5)
            log.info("🔄 Keep-alive ping OK")
        except Exception as e:
            log.warning(f"Keep-alive ping fallito: {e}")
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
                        message = update.get("message", {})
                        text = message.get("text", "")
                        if text.startswith("/"):
                            handle_command(text)
        except Exception as e:
            log.error(f"Telegram commands error: {e}")
        stop_event.wait(5)

def handle_command(text: str):
    global BOT_ENABLED, PAPER_MODE
    cmd = text.lower().strip()
    
    if cmd == "/status":
        market_ok, market_msg = check_market_conditions()
        msg = (
            f"🤖 *STATO BOT*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{'🟢 ATTIVO' if BOT_ENABLED else '🔴 DISATTIVATO'}\n"
            f"Modalità: {'PAPER 📝' if PAPER_MODE else 'REAL 💰'}\n"
            f"📊 Trade attivi: {len(active_trades)}/{MAX_TRADES}\n"
            f"💰 PnL totale: {round(stats['pnl'], 2)} €\n"
            f"🏆 Record: {stats['wins']}W - {stats['losses']}L\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{market_msg}"
        )
        send_telegram(msg)
    elif cmd == "/stop":
        BOT_ENABLED = False
        send_telegram(" Bot DISATTIVATO - Non aprirò nuovi trade")
    elif cmd == "/start":
        BOT_ENABLED = True
        send_telegram(" Bot ATTIVATO - Riprendo a operare")
    elif cmd == "/stats":
        total = stats["wins"] + stats["losses"]
        winrate = round((stats["wins"] / total) * 100, 1) if total > 0 else 0
        msg = (
            f"📊 *STATISTICHE*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 PnL: {round(stats['pnl'], 2)} €\n"
            f"🎯 Winrate: {winrate}%\n"
            f"🏆 Wins: {stats['wins']}\n"
            f"❌ Losses: {stats['losses']}\n"
            f"📦 Trade attivi: {len(active_trades)}"
        )
        send_telegram(msg)
    elif cmd == "/backup":
        send_telegram("💾 Generazione backup in corso...")
        if os.path.exists(SIGNALS_FILE):
            send_telegram_document(SIGNALS_FILE, "💾 Backup segnali richiesto")
        else:
            send_telegram("❌ Nessun file segnali trovato")
        if os.path.exists(HISTORY_FILE):
            send_telegram_document(HISTORY_FILE, "💾 Backup trade history")
        else:
            send_telegram("❌ Nessun file trade history trovato")
    elif cmd == "/backtest":
        send_telegram("🔬 Avvio backtesting in corso... (può richiedere alcuni minuti)")
        try:
            test_tickers = TICKERS[:50]
            report = run_backtest(test_tickers, days=30)
            msg = (
                f"📊 *REPORT BACKTEST*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🎯 Trade totali: {report['total_trades']}\n"
                f"✅ Wins: {report['wins']}\n"
                f"❌ Losses: {report['losses']}\n"
                f"📈 Winrate: {report['winrate']}%\n"
                f"💰 PnL totale: {report['total_pnl']}%\n"
                f"📊 PnL medio: {report['avg_pnl']}%\n"
                f"📉 Max Drawdown: {report['max_drawdown']}%\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f" *Migliori Ticker:*\n"
            )
            for t in report['best_tickers'][:5]:
                msg += f"\n{t['ticker']}: {t['pnl']}% ({t['winrate']}% WR)"
            msg += f"\n\n📎 File completo in allegato"
            send_telegram(msg)
            if os.path.exists("backtest_report.json"):
                send_telegram_document("backtest_report.json", " Report backtest completo")
        except Exception as e:
            send_telegram(f"❌ Errore backtest: {e}")
            log.error(f"Backtest command error: {e}")
    elif cmd == "/screener":
        send_telegram("🔍 Avvio screener in corso...")
        run_screener()
    elif cmd == "/paper":
        PAPER_MODE = not PAPER_MODE
        status = "🟢 ATTIVATA" if PAPER_MODE else "🔴 DISATTIVATA"
        send_telegram(f"📝 Paper Trading Mode: {status}")
    elif cmd == "/alerts":
        if price_alerts:
            msg = "🔔 *ALERT ATTIVI*\n━━━━━━━━━━━━━━━━━━\n"
            for t, p in price_alerts.items():
                msg += f"• {t}: ${p}\n"
            send_telegram(msg)
        else:
            send_telegram("Nessun alert attivo.")
    elif cmd.startswith("/alert "):
        parts = text.split()
        if len(parts) == 3:
            ticker = parts[1].upper()
            try:
                price = float(parts[2])
                price_alerts[ticker] = price
                save_alerts()
                send_telegram(f"✅ Alert impostato: {ticker} a ${price}")
            except ValueError:
                send_telegram("❌ Formato errato. Usa: /alert AAPL 150")
        else:
            send_telegram("❌ Uso: /alert TICKER PREZZO")
    elif cmd == "/help":
        msg = (
            f"🤖 *COMANDI DISPONIBILI*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"/status - Stato del bot\n"
            f"/start - Attiva il bot\n"
            f"/stop - Ferma il bot\n"
            f"/stats - Statistiche\n"
            f"/backup - Invia backup CSV\n"
            f"/backtest - Esegui backtest\n"
            f"/screener - Scansione RSI\n"
            f"/paper - Toggle Paper Trading\n"
            f"/alert TICKER PREZZO - Imposta alert\n"
            f"/alerts - Vedi alert attivi\n"
            f"/help - Questo messaggio"
        )
        send_telegram(msg)

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

def check_sr_breakout(ticker, df, prev_sr=None):
    if prev_sr is None:
        return
    last = df.iloc[-1]
    price = float(last["Close"])
    sr = prev_sr
    breakout_msg = None
    
    for level_name in ["R1", "R2", "R3"]:
        if level_name in sr:
            level = sr[level_name]
            if price > level and prev_sr.get(f"prev_{level_name}", level) <= level:
                breakout_msg = f"🔥 {ticker} ha ROTTO la resistenza {level_name} @ {level}!"
                break
    
    if not breakout_msg:
        for level_name in ["S1", "S2", "S3"]:
            if level_name in sr:
                level = sr[level_name]
                if price < level and prev_sr.get(f"prev_{level_name}", level) >= level:
                    breakout_msg = f"⚠️ {ticker} ha ROTTO il supporto {level_name} @ {level}!"
                    break
    
    if not breakout_msg:
        for level_name in ["R1", "R2", "R3", "S1", "S2", "S3"]:
            if level_name in sr:
                level = sr[level_name]
                distance = abs(price - level) / level * 100
                if distance < 0.5 and prev_sr.get(f"prev_{level_name}", level) != level:
                    side = "resistenza" if "R" in level_name else "supporto"
                    breakout_msg = f"🔔 {ticker} sta TESTANDO {side} {level_name} @ {level}"
                    break
    
    if breakout_msg:
        send_telegram(breakout_msg)
        log.info(f"SR Alert: {breakout_msg}")
    
    new_prev_sr = sr.copy()
    for level_name in ["R1", "R2", "R3", "S1", "S2", "S3"]:
        if level_name in sr:
            new_prev_sr[f"prev_{level_name}"] = sr[level_name]
    return new_prev_sr

def check_trade_alerts(ticker, trade, df):
    if trade is None:
        return
    last = df.iloc[-1]
    price = float(last["Close"])
    entry = float(trade["entry"])
    stop = float(trade["stop"])
    target = float(trade["target"])
    side = trade["side"]
    
    if side == "BUY":
        pnl_pct = ((price - entry) / entry) * 100
        dist_to_target = ((target - price) / target) * 100 if target > price else 0
        dist_to_stop = ((price - stop) / stop) * 100 if price > stop else 0
    else:
        pnl_pct = ((entry - price) / entry) * 100
        dist_to_target = ((price - target) / price) * 100 if price > target else 0
        dist_to_stop = ((stop - price) / stop) * 100 if stop > price else 0
    
    if 0 < dist_to_target <= 10 and not trade.get("alerted_90_target", False):
        msg = f"🔥 {ticker} sta per raggiungere il TARGET!\n📊 Mancano solo {dist_to_target:.1f}%\n💰 PnL: {pnl_pct:+.2f}%"
        send_telegram(msg)
        trade["alerted_90_target"] = True
        log.info(f"Alert 90% target: {ticker}")
    
    if 40 <= dist_to_target <= 60 and not trade.get("alerted_50_target", False):
        msg = f"💰 {ticker} a metà strada dal TARGET\n📊 PnL: {pnl_pct:+.2f}%"
        send_telegram(msg)
        trade["alerted_50_target"] = True
        log.info(f"Alert 50% target: {ticker}")
    
    if 0 < dist_to_stop <= 5 and not trade.get("alerted_near_stop", False):
        msg = f"⚠️ {ticker} si avvicina allo STOP LOSS!\n Mancano {dist_to_stop:.1f}%\n💰 PnL: {pnl_pct:+.2f}%"
        send_telegram(msg)
        trade["alerted_near_stop"] = True
        log.info(f"Alert near stop: {ticker}")

def generate_equity_curve():
    if not os.path.exists(HISTORY_FILE):
        return None
    try:
        df = pd.read_csv(HISTORY_FILE)
        if df.empty or len(df) < 2:
            return None
        
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")
        
        df["cumulative_pnl"] = df["pnl"].cumsum()
        
        fig, ax = plt.subplots(figsize=(12, 6))
        ax.plot(df["date"], df["cumulative_pnl"], color="#10b981", linewidth=2, label="Equity Curve")
        ax.fill_between(df["date"], df["cumulative_pnl"], alpha=0.3, color="#10b981")
        ax.axhline(y=0, color="#64748b", linestyle="--", linewidth=1, alpha=0.5)
        
        wins = df[df["result"] == "WIN"]
        losses = df[df["result"] == "LOSS"]
        if not wins.empty:
            ax.scatter(wins["date"], wins["cumulative_pnl"], color="#10b981", s=50, marker="o", label="WIN", zorder=5)
        if not losses.empty:
            ax.scatter(losses["date"], losses["cumulative_pnl"], color="#ef4444", s=50, marker="x", label="LOSS", zorder=5)
        
        ax.set_title("📈 Equity Curve - Andamento PnL nel Tempo", fontsize=14, fontweight="bold", color="#e2e8f0")
        ax.set_xlabel("Data", fontsize=12, color="#e2e8f0")
        ax.set_ylabel("PnL Cumulativo (€)", fontsize=12, color="#e2e8f0")
        ax.legend(loc="upper left", fontsize=10)
        ax.grid(True, alpha=0.3)
        
        fig.patch.set_facecolor('#0f172a')
        ax.set_facecolor('#1e293b')
        ax.tick_params(colors='#e2e8f0')
        ax.xaxis.label.set_color('#e2e8f0')
        ax.yaxis.label.set_color('#e2e8f0')
        ax.title.set_color('#e2e8f0')
        
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d/%m'))
        plt.xticks(rotation=45)
        plt.tight_layout()
        
        img_buf = io.BytesIO()
        plt.savefig(img_buf, format='png', dpi=100, bbox_inches='tight', facecolor=fig.get_facecolor())
        img_buf.seek(0)
        plt.close()
        return img_buf.getvalue()
    except Exception as e:
        log.error(f"Error generating equity curve: {e}")
        return None

def run_backtest(tickers, days=30):
    log.info(f"🔬 Esecuzione backtest su {len(tickers)} ticker per {days} giorni...")
    results = []
    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_pnl = 0.0
    max_drawdown = 0.0
    peak_pnl = 0.0
    
    for ticker in tickers:
        try:
            df = yf.download(ticker, period=f"{days}d", interval="1d", progress=False)
            if df.empty or len(df) < 200:
                continue
            
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]
            df = compute_indicators(df)
            df["ATR"] = compute_atr(df)
            
            ticker_trades = 0
            ticker_wins = 0
            ticker_losses = 0
            ticker_pnl = 0.0
            
            for i in range(200, len(df)):
                subset = df.iloc[i-200:i+1].copy()
                signal = analyze_signal(ticker, subset)
                if signal and signal["action"] != "NEUTRAL":
                    ticker_trades += 1
                    total_trades += 1
                    entry_price = float(subset.iloc[-1]["Close"])
                    
                    for j in range(1, 6):
                        if i+j < len(df):
                            exit_price = float(df.iloc[i+j]["Close"])
                            high_price = float(df.iloc[i+j]["High"])
                            low_price = float(df.iloc[i+j]["Low"])
                            atr = float(subset.iloc[-1]["ATR"])
                            
                            if signal["action"] == "BUY":
                                stop = entry_price - (2 * atr)
                                target = entry_price + (4 * atr)
                                if low_price <= stop:
                                    pnl = -2
                                    ticker_losses += 1
                                    total_losses += 1
                                    break
                                elif high_price >= target:
                                    pnl = 4
                                    ticker_wins += 1
                                    total_wins += 1
                                    break
                            else:
                                stop = entry_price + (2 * atr)
                                target = entry_price - (4 * atr)
                                if high_price >= stop:
                                    pnl = -2
                                    ticker_losses += 1
                                    total_losses += 1
                                    break
                                elif low_price <= target:
                                    pnl = 4
                                    ticker_wins += 1
                                    total_wins += 1
                                    break
                    
                    ticker_pnl += pnl
                    total_pnl += pnl
                    peak_pnl = max(peak_pnl, total_pnl)
                    drawdown = ((peak_pnl - total_pnl) / peak_pnl * 100) if peak_pnl > 0 else 0
                    max_drawdown = max(max_drawdown, drawdown)
            
            if ticker_trades > 0:
                winrate = (ticker_wins / ticker_trades) * 100
                results.append({
                    "ticker": ticker,
                    "trades": ticker_trades,
                    "wins": ticker_wins,
                    "losses": ticker_losses,
                    "winrate": round(winrate, 1),
                    "pnl": round(ticker_pnl, 2),
                    "avg_pnl": round(ticker_pnl / ticker_trades, 2)
                })
        except Exception as e:
            log.error(f"Backtest error for {ticker}: {e}")
            continue
    
    winrate = (total_wins / total_trades * 100) if total_trades > 0 else 0
    avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
    
    report = {
        "total_trades": total_trades,
        "wins": total_wins,
        "losses": total_losses,
        "winrate": round(winrate, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(avg_pnl, 2),
        "max_drawdown": round(max_drawdown, 1),
        "best_tickers": sorted(results, key=lambda x: x["pnl"], reverse=True)[:10],
        "worst_tickers": sorted(results, key=lambda x: x["pnl"])[:10],
        "all_results": results
    }
    
    import json
    with open("backtest_report.json", "w") as f:
        json.dump(report, f, indent=2)
    
    log.info(f"✅ Backtest completato: {total_trades} trade, Winrate: {winrate:.1f}%")
    return report

def detect_patterns(df, ticker):
    if len(df) < 3:
        return
    last = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]
    
    o, h, l, c = float(last["Open"]), float(last["High"]), float(last["Low"]), float(last["Close"])
    po, ph, pl, pc = float(prev["Open"]), float(prev["High"]), float(prev["Low"]), float(prev["Close"])
    po2, ph2, pl2, pc2 = float(prev2["Open"]), float(prev2["High"]), float(prev2["Low"]), float(prev2["Close"])
    
    body = abs(c - o)
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l
    total_range = h - l
    
    if total_range == 0:
        return
    
    patterns_found = []
    
    if body < total_range * 0.3 and lower_shadow > body * 2 and upper_shadow < body * 0.5:
        if pc < po:
            patterns_found.append("🔨 Hammer (rialzista)")
    
    if body < total_range * 0.3 and upper_shadow > body * 2 and lower_shadow < body * 0.5:
        if pc > po:
            patterns_found.append("⭐ Shooting Star (ribassista)")
    
    if pc < po and c > o and o <= pc and c >= po:
        patterns_found.append(" Bullish Engulfing (rialzista)")
    
    if pc > po and c < o and o >= pc and c <= po:
        patterns_found.append("🔴 Bearish Engulfing (ribassista)")
    
    if body < total_range * 0.1:
        patterns_found.append("➕ Doji (indecisione)")
    
    if pc2 < po2 and abs(pc - po) < (ph2 - pl2) * 0.1 and c > o and c > (po2 + pc2) / 2:
        patterns_found.append(" Morning Star (rialzista)")
    
    if pc2 > po2 and abs(pc - po) < (ph2 - pl2) * 0.1 and c < o and c < (po2 + pc2) / 2:
        patterns_found.append("🌇 Evening Star (ribassista)")
    
    if patterns_found:
        pattern_list = "\n".join([f"  • {p}" for p in patterns_found])
        msg = (
            f"🎯 *PATTERN TROVATO su {ticker}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{pattern_list}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 Prezzo: {round(c, 2)}"
        )
        send_telegram(msg)
        log.info(f"Pattern trovato su {ticker}: {', '.join(patterns_found)}")

# =========================================
# ALERT & SCREENER
# =========================================
def load_alerts():
    global price_alerts
    if os.path.exists(ALERTS_FILE):
        try:
            with open(ALERTS_FILE, 'r') as f:
                price_alerts = json.load(f)
            log.info(f"🔔 Caricati {len(price_alerts)} alert")
        except Exception as e:
            log.error(f"Error loading alerts: {e}")
            price_alerts = {}

def save_alerts():
    try:
        with open(ALERTS_FILE, 'w') as f:
            json.dump(price_alerts, f, indent=2)
    except Exception as e:
        log.error(f"Error saving alerts: {e}")

def check_price_alerts(ticker, current_price):
    if ticker in price_alerts:
        target = price_alerts[ticker]
        if current_price >= target or current_price <= target:
            msg = f"🔔 *ALERT PREZZO*\n"
            msg += f"━━━━━━━━━━━━━━━━━━\n"
            msg += f"{ticker} ha raggiunto ${target}!\n"
            msg += f"💰 Prezzo attuale: ${current_price:.2f}"
            send_telegram(msg)
            del price_alerts[ticker]
            save_alerts()
            log.info(f"✅ Alert triggerato: {ticker} @ ${target}")

def run_screener():
    log.info("🔍 Avvio Screener Avanzato...")
    opportunities = []
    
    for ticker in TICKERS[:50]:
        try:
            df = yf.download(ticker, period="5d", interval="1h", progress=False)
            if df.empty or len(df) < 14:
                continue
            
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]
            delta = df["Close"].diff()
            gain = delta.where(delta > 0, 0.0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            last_rsi = float(rsi.iloc[-1].values[0]) if hasattr(rsi.iloc[-1], 'values') else float(rsi.iloc[-1])
            price = float(df["Close"].iloc[-1].values[0]) if hasattr(df["Close"].iloc[-1], 'values') else float(df["Close"].iloc[-1])
            
            if last_rsi < 30:
                opportunities.append(f"🟢 {ticker}: RSI {last_rsi:.1f} (Ipervenduto) @ ${price:.2f}")
            elif last_rsi > 70:
                opportunities.append(f"🔴 {ticker}: RSI {last_rsi:.1f} (Ipercomprato) @ ${price:.2f}")
        except Exception as e:
            log.error(f"Screener error for {ticker}: {e}")
            continue
    
    if opportunities:
        msg = "🔍 *SCREENER REPORT*\n"
        msg += "━━━━━━━━━━━━━━━━━━\n"
        msg += "\n".join(opportunities[:10])
        send_telegram(msg)
        log.info(f"🔍 Screener: trovate {len(opportunities)} opportunità")
    else:
        send_telegram("🔍 Screener: Nessuna opportunità immediata trovata.")

def calculate_support_resistance(df):
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    H, L, C = float(prev["High"]), float(prev["Low"]), float(prev["Close"])
    PP = (H + L + C) / 3
    
    S1_pivot = (2 * PP) - H
    R1_pivot = (2 * PP) - L
    S2_pivot = PP - (H - L)
    R2_pivot = PP + (H - L)
    S3_pivot = L - 2 * (H - PP)
    R3_pivot = H + 2 * (PP - L)
    
    lookback = min(20, len(df) - 1)
    recent = df.tail(lookback)
    swing_high = float(recent["High"].max())
    swing_low = float(recent["Low"].min())
    
    range_high = swing_high
    range_low = swing_low
    fib_range = range_high - range_low
    fib_236 = range_high - (fib_range * 0.236)
    fib_382 = range_high - (fib_range * 0.382)
    fib_500 = range_high - (fib_range * 0.500)
    fib_618 = range_high - (fib_range * 0.618)
    fib_786 = range_high - (fib_range * 0.786)
    
    price = float(last["Close"])
    atr = float(last["ATR"]) if "ATR" in last else (price * 0.02)
    
    supports = sorted([s for s in [S1_pivot, S2_pivot, S3_pivot, swing_low, fib_618, fib_786] if s < price], reverse=True)[:3]
    resistances = sorted([r for r in [R1_pivot, R2_pivot, R3_pivot, swing_high, fib_382, fib_236] if r > price])[:3]
    
    while len(supports) < 3:
        supports.append(price - atr * (len(supports) + 2))
    while len(resistances) < 3:
        resistances.append(price + atr * (len(resistances) + 2))
    
    return {
        "S1": round(supports[0], 2),
        "S2": round(supports[1], 2),
        "S3": round(supports[2], 2),
        "R1": round(resistances[0], 2),
        "R2": round(resistances[1], 2),
        "R3": round(resistances[2], 2),
        "fib_236": round(fib_236, 2),
        "fib_382": round(fib_382, 2),
        "fib_500": round(fib_500, 2),
        "fib_618": round(fib_618, 2),
    }

# =========================================
# CHART GENERATION
# =========================================
def generate_chart_image(ticker: str, df, side: str, entry: float, stop: float, target: float):
    try:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), height_ratios=[3, 1], gridspec_kw={'hspace': 0.3})
        
        ax1.plot(df.index[-50:], df["Close"].iloc[-50:], label="Close", linewidth=1.5)
        ax1.plot(df.index[-50:], df["EMA50"].iloc[-50:], label="EMA50", linewidth=1, alpha=0.7)
        ax1.plot(df.index[-50:], df["EMA200"].iloc[-50:], label="EMA200", linewidth=1, alpha=0.7)
        
        ax1.axhline(y=entry, color="blue", linestyle="--", linewidth=2, label=f"Entry: {entry:.2f}")
        ax1.axhline(y=stop, color="red", linestyle="--", linewidth=2, label=f"Stop: {stop:.2f}")
        ax1.axhline(y=target, color="green", linestyle="--", linewidth=2, label=f"Target: {target:.2f}")
        
        ax1.set_title(f"{ticker} - {side}", fontsize=14, fontweight="bold")
        ax1.legend(loc="upper left")
        ax1.grid(True, alpha=0.3)
        ax1.set_ylabel("Price")
        
        colors = ["green" if df["Close"].iloc[i] >= df["Open"].iloc[i] else "red" for i in range(-50, 0)]
        ax2.bar(df.index[-50:], df["Volume"].iloc[-50:], color=colors, alpha=0.6)
        ax2.set_ylabel("Volume")
        ax2.grid(True, alpha=0.3)
        
        plt.subplots_adjust(hspace=0.3)
        
        img_buf = io.BytesIO()
        plt.savefig(img_buf, format='png', dpi=100, bbox_inches='tight')
        img_buf.seek(0)
        plt.close()
        return img_buf.getvalue()
    except Exception as e:
        log.error(f"Error generating chart: {e}")
        return None

# =========================================
# DATA DOWNLOAD
# =========================================
def download_ticker(ticker: str, max_retries=3):
    cache_key = f"{ticker}_{datetime.now().date()}"
    if not hasattr(download_ticker, 'cache'):
        download_ticker.cache = {}
    
    if cache_key in download_ticker.cache:
        return download_ticker.cache[cache_key]
    
    if TWELVE_DATA_API_KEY:
        url = f"https://api.twelvedata.com/time_series?symbol={ticker}&interval=1day&outputsize=180&apikey={TWELVE_DATA_API_KEY}"
        for attempt in range(max_retries):
            try:
                r = requests.get(url, timeout=(5, 15))
                r.raise_for_status()
                data = r.json()
                if "values" in data:
                    rows = [{"Open": float(x["open"]), "High": float(x["high"]), "Low": float(x["low"]), "Close": float(x["close"]), "Volume": float(x["volume"])} for x in reversed(data["values"])]
                    df = pd.DataFrame(rows)
                    download_ticker.cache[cache_key] = df
                    return df
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                continue
    
    for attempt in range(max_retries):
        try:
            delay = 1 + random.random() * 2
            time.sleep(delay)
            df = yf.download(ticker, period="1y", interval="1d", progress=False)
            if df.empty:
                return None
            df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]
            download_ticker.cache[cache_key] = df
            return df
        except Exception as e:
            if "Rate limited" in str(e) or "Too Many Requests" in str(e):
                wait_time = 10 * (attempt + 1)
                log.warning(f"Rate limit per {ticker}, attendo {wait_time}s")
                time.sleep(wait_time)
            elif attempt < max_retries - 1:
                wait_time = 2 ** attempt + random.random()
                log.warning(f"Retry {attempt+1}/{max_retries} per {ticker}, attendo {wait_time:.1f}s")
                time.sleep(wait_time)
            else:
                log.error(f"Download error {ticker} dopo {max_retries} tentativi: {e}")
                return None
    return None

# =========================================
# MARKET FILTERS
# =========================================
def is_market_open():
    now_utc = datetime.now(timezone.utc)
    now_ny = now_utc - timedelta(hours=4)
    if now_ny.weekday() >= 5:
        return False
    return 9 <= now_ny.hour < 16

# =========================================
# POSITION MONITOR
# =========================================
def check_positions(df_by_ticker):
    to_close = []
    with state_lock:
        for ticker, pos in list(active_trades.items()):
            df = df_by_ticker.get(ticker)
            
            if df is not None and not df.empty:
                current_price = float(df.iloc[-1]["Close"])
                check_price_alerts(ticker, current_price)
            
            if df is None or df.empty:
                continue
            
            last = df.iloc[-1]
            high, low = float(last["High"]), float(last["Low"])
            hit, exit_price = None, None
            
            if pos["side"] == "BUY":
                if low <= pos["stop"]:
                    hit, exit_price = "STOP", pos["stop"]
                elif high >= pos["target"]:
                    hit, exit_price = "TARGET", pos["target"]
            else:
                if high >= pos["stop"]:
                    hit, exit_price = "STOP", pos["stop"]
                elif low <= pos["target"]:
                    hit, exit_price = "TARGET", pos["target"]
            
            if hit:
                qty = pos["qty"]
                pnl = (exit_price - pos["entry"]) * qty if pos["side"] == "BUY" else (pos["entry"] - exit_price) * qty
                result = "WIN" if pnl > 0 else "LOSS"
                to_close.append((ticker, pos, exit_price, pnl, result, hit))
    
    for ticker, pos, exit_price, pnl, result, hit in to_close:
        active_trades.pop(ticker, None)
        stats["wins" if result == "WIN" else "losses"] += 1
        stats["pnl"] += pnl
        daily_stats["pnl"] += pnl
        daily_stats["trades"] += 1
        save_trade(ticker, pos["side"], pos["entry"], exit_price, pnl, 2, result)
        
        emoji = "✅" if result == "WIN" else "❌"
        pnl_pct = (pnl / (pos["entry"] * pos["qty"])) * 100
        msg = (
            f"{emoji} *{hit} {pos['side']} {ticker}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📥 *Entry:* {round(pos['entry'], 2)}\n"
            f"📤 *Exit:* {round(exit_price, 2)}\n"
            f"💵 *PnL:* {round(pnl, 2)} € ({pnl_pct:+.2f}%)\n"
            f"💰 *Totale:* {round(stats['pnl'], 2)} €\n"
            f"🏆 *Record:* {stats['wins']}W - {stats['losses']}L"
        )
        send_telegram(msg)
        log.info(f"{hit} {ticker} -> {result} ({round(pnl, 2)}€)")
        
        if daily_stats["pnl"] <= -MAX_DAILY_LOSS:
            log.warning(f"🚨 MAX DAILY LOSS raggiunto: {round(daily_stats['pnl'], 2)}€")
            send_telegram(f" *ATTENZIONE*\nPerdita giornaliera massima raggiunta: {round(daily_stats['pnl'], 2)}€\nBot fermato automaticamente.")
            global BOT_ENABLED
            BOT_ENABLED = False

# =========================================
# SIGNAL GENERATOR
# =========================================
def analyze_signal(ticker, df):
    """Funzione helper per backtest"""
    if len(df) < 50:
        return {"action": "NEUTRAL"}
    
    last = df.iloc[-1]
    if pd.isna(last.get("ATR", None)) or last.get("ATR", 0) == 0:
        return {"action": "NEUTRAL"}
    
    score_buy = 0
    score_sell = 0
    
    if last["EMA50"] > last["EMA200"]:
        score_buy += 1
    else:
        score_sell += 1
    
    if last["RSI"] > 55:
        score_buy += 1
    elif last["RSI"] < 45:
        score_sell += 1
    
    if last["MACD"] > last["MACD_signal"]:
        score_buy += 1
    else:
        score_sell += 1
    
    if score_buy >= 2:
        return {"action": "BUY"}
    elif score_sell >= 2:
        return {"action": "SELL"}
    return {"action": "NEUTRAL"}

def analyze_ticker(ticker, df):
    if not BOT_ENABLED:
        return
    if ticker in bad_tickers or not ticker.isalpha() or len(ticker) > 5:
        return
    
    with state_lock:
        if ticker in cooldown_tickers:
            if (datetime.now() - cooldown_tickers[ticker]).total_seconds() / 60 < COOLDOWN_MINUTES:
                return
    
    if df is None or len(df) < 50:
        return
    
    df["ATR"] = compute_atr(df)
    df = compute_indicators(df)
    last = df.iloc[-1]
    
    if pd.isna(last["ATR"]) or last["ATR"] == 0:
        return
    
    price, atr = float(last["Close"]), float(last["ATR"])
    score_buy = 0
    score_sell = 0
    
    if last["EMA50"] > last["EMA200"]:
        score_buy += 1
    else:
        score_sell += 1
    
    if last["RSI"] > 55:
        score_buy += 1
    elif last["RSI"] < 45:
        score_sell += 1
    
    if last["MACD"] > last["MACD_signal"]:
        score_buy += 1
    else:
        score_sell += 1
    
    vol_mean = df["Volume"].rolling(20).mean().iloc[-1]
    volume_ratio = df["Volume"].iloc[-1] / vol_mean if vol_mean else 0
    
    side = None
    if score_buy >= 2 and volume_ratio > MIN_VOLUME_RATIO:
        side = "BUY"
    elif score_sell >= 2 and volume_ratio > MIN_VOLUME_RATIO:
        side = "SELL"
    
    if not side:
        return
    
    stop = price - atr if side == "BUY" else price + atr
    target = price + (atr * 2) if side == "BUY" else price - (atr * 2)
    qty = max(1, int(CAPITALE_PER_TRADE / price))
    
    log_signal(ticker, side, price, stop, target)
    
    with state_lock:
        if ticker in active_trades or len(active_trades) >= MAX_TRADES:
            log.info(f"⏸️ Segnale {side} {ticker} non tradato (slot pieni)")
            cooldown_tickers[ticker] = datetime.now()
            return
        
        active_trades[ticker] = {
            "side": side, "entry": price, "stop": stop, "target": target,
            "qty": qty, "ts": datetime.now().isoformat()
        }
        cooldown_tickers[ticker] = datetime.now()
    
    sr = calculate_support_resistance(df)
    stop_pct = ((stop - price) / price) * 100 if side == "BUY" else ((price - stop) / price) * 100
    target_pct = ((target - price) / price) * 100 if side == "BUY" else ((price - target) / price) * 100
    
    msg = (
        f" *{side} {ticker}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Entry:* {round(price, 2)}\n"
        f"🛑 *Stop Loss:* {round(stop, 2)} ({stop_pct:+.2f}%)\n"
        f"🎯 *Take Profit:* {round(target, 2)} ({target_pct:+.2f}%)\n"
        f"📊 *R/R:* 1:2 | 💼 *Qty:* {qty}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 *Supporti:*\n"
        f"   • S1: {sr['S1']}\n"
        f"   • S2: {sr['S2']}\n"
        f"📉 *Resistenze:*\n"
        f"   • R1: {sr['R1']}\n"
        f"   • R2: {sr['R2']}"
    )
    send_telegram(msg)
    
    chart_img = generate_chart_image(ticker, df, side, price, stop, target)
    if chart_img:
        send_telegram_photo(chart_img, f"📊 {side} {ticker} @ {round(price, 2)}")
    
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
            if not is_market_open():
                stop_event.wait(600)
                continue
            
            market_ok, market_msg = check_market_conditions()
            if not market_ok:
                log.warning(f"⚠️ {market_msg} - Skip trading")
                stop_event.wait(600)
                continue
            
            subset = [t for t in TICKERS if t not in BLACKLIST]
            if not subset:
                stop_event.wait(60)
                continue
            
            batch_size = 25
            batch = subset[idx * batch_size : (idx + 1) * batch_size]
            if not batch:
                idx = 0
                batch = subset[:batch_size]
            idx += 1
            
            log.info(f"📊 Analisi batch {idx}: {len(batch)} ticker")
            
            df_by_ticker = {}
            threads = []
            
            def download_single(ticker):
                df = download_ticker(ticker)
                if df is not None:
                    with state_lock:
                        df_by_ticker[ticker] = df
            
            for t in batch:
                thread = threading.Thread(target=download_single, args=(t,))
                threads.append(thread)
                thread.start()
                time.sleep(0.5)
            
            for thread in threads:
                thread.join(timeout=60)
            
            check_positions(df_by_ticker)
            
            for t in batch:
                if t in df_by_ticker:
                    try:
                        analyze_ticker(t, df_by_ticker[t])
                    except Exception as e:
                        log.error(f"Error {t}: {e}")
            
            stop_event.wait(180)
        except Exception as e:
            log.error(f"Loop error: {e}")
            stop_event.wait(60)

# =========================================
# DASHBOARD HTML (CORRETTO)
# =========================================
DASHBOARD_HTML = """
<!doctype html>
<html>
<head>
<meta charset='utf-8'>
<meta http-equiv="refresh" content="30">
<title>Trading Bot</title>
<style>
body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;padding:20px;margin:0}
.card{background:#1e293b;padding:20px;border-radius:10px;margin:15px 0}
table{border-collapse:collapse;width:100%}th,td{padding:12px;border:1px solid #334155;text-align:left}
th{background:#334155}.stat{display:inline-block;margin-right:30px;font-size:18px}
.stat-value{font-weight:bold;color:#10b981}.stat-negative{color:#ef4444}
h1{color:#10b981}h2{color:#3b82f6;margin-top:0}
.badge{padding:4px 8px;border-radius:4px;font-size:12px;font-weight:bold;display:inline-block}
.badge-buy{background:#10b981;color:white}.badge-sell{background:#ef4444;color:white}
.badge-win{background:#10b981;color:white}.badge-loss{background:#ef4444;color:white}
.badge-pending{background:#f59e0b;color:white}.badge-expired{background:#6b7280;color:white}
.nav{margin-bottom:20px;background:#1e293b;padding:15px;border-radius:10px}
.nav a{color:#3b82f6;margin-right:20px;text-decoration:none;font-weight:bold}
.nav a:hover{text-decoration:underline}
.chart-controls{display:flex;gap:10px;margin-bottom:15px;flex-wrap:wrap;align-items:center}
.chart-controls select, .chart-controls button{
background:#334155;color:#e2e8f0;border:1px solid #475569;
padding:8px 15px;border-radius:6px;font-size:14px;cursor:pointer
}
.chart-controls button:hover{background:#475569}
.chart-container{width:100%;height:500px;border-radius:10px;overflow:hidden;border:1px solid #334155}
.btn-chart{background:#3b82f6;color:white;padding:6px 12px;border-radius:4px;text-decoration:none;font-size:12px;display:inline-block}
.btn-chart:hover{background:#2563eb}
.ticker-pills{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.ticker-pill{background:#334155;padding:6px 12px;border-radius:20px;cursor:pointer;font-size:13px;border:1px solid transparent}
.ticker-pill:hover, .ticker-pill.active{background:#3b82f6;border-color:#60a5fa}
</style>
</head>
<body>
<h1>🚀 Trading Bot Dashboard</h1>
<div class="nav">
<a href="/">🏠 Home</a>
<a href="/signals"> Analisi Segnali</a>
<a href="/download-signals">📥 Download CSV</a>
</div>
<div class="card">
<h2>📈 Grafico Live</h2>
<div class="chart-controls">
<select id="tickerSelect" onchange="updateChart()">
{% for ticker in tickers[:30] %}
<option value="{{ ticker }}">{{ ticker }}</option>
{% endfor %}
</select>
<select id="intervalSelect" onchange="updateChart()">
<option value="D">1 Giorno</option>
<option value="60">1 Ora</option>
<option value="15">15 Minuti</option>
<option value="5">5 Minuti</option>
</select>
<button onclick="updateChart()">🔄 Aggiorna</button>
</div>
<div class="ticker-pills" id="tickerPills"></div>
<div class="chart-container" id="tradingview_chart"></div>
</div>
<div class="card">
<h2>📊 Statistiche Trade</h2>
<span class="stat">💰 PnL: <span class="stat-value {% if stats.pnl < 0 %}stat-negative{% endif %}">{{ stats.pnl|round(2) }} €</span></span>
<span class="stat"> Winrate: <span class="stat-value">{{ winrate }}%</span></span>
<span class="stat">🏆 Wins: <span class="stat-value">{{ stats.wins }}</span></span>
<span class="stat">❌ Losses: <span class="stat-value">{{ stats.losses }}</span></span>
<span class="stat">📦 Active: <span class="stat-value">{{ active_trades|length }}/{{ max_trades }}</span></span>
</div>
<div class="card">
<h2>📡 Trade Attivi</h2>
<table>
<tr><th>Ticker</th><th>Side</th><th>Entry</th><th>Stop</th><th>Target</th><th>Qty</th><th>Time</th><th>Azioni</th></tr>
{% for ticker, tr in active_trades.items() %}
<tr>
<td><strong>{{ ticker }}</strong></td>
<td><span class="badge badge-{{ tr.side|lower }}">{{ tr.side }}</span></td>
<td>{{ tr.entry|round(2) }}</td>
<td style="color:#ef4444;">{{ tr.stop|round(2) }}</td>
<td style="color:#10b981;">{{ tr.target|round(2) }}</td>
<td>{{ tr.qty }}</td>
<td>{{ tr.ts[:16] }}</td>
<td><a href="/chart/{{ ticker }}" class="btn-chart">📊 Grafico</a></td>
</tr>
{% else %}
<tr><td colspan="8" style="text-align:center;">Nessun trade attivo</td></tr>
{% endfor %}
</table>
</div>
<div class="card">
<h2>📜 Trade History</h2>
{{ history_html|safe }}
</div>
<div class="card" style="text-align:center;color:#64748b;font-size:14px;">
Auto-refresh 30s | {{ now }}
</div>
<script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
<script>
function updateChart() {
const symbol = document.getElementById('tickerSelect').value;
const interval = document.getElementById('intervalSelect').value;
document.getElementById('tradingview_chart').innerHTML = '';

new TradingView.widget({
"autosize": true,
"symbol": symbol,
"interval": interval,
"timezone": "Europe/Rome",
"theme": "dark",
"style": "1",
"locale": "it",
"toolbar_bg": "#1e293b",
"enable_publishing": false,
"hide_top_toolbar": false,
"hide_legend": false,
"save_image": false,
"container_id": "tradingview_chart",
"studies": [
"MAExp@tv-basicstudies",
"RSI@tv-basicstudies",
"MACD@tv-basicstudies",
"Volume@tv-basicstudies"
],
"show_popup_button": true,
"popup_width": "1000",
"popup_height": "650"
});
}

const activeTrades = {{ active_trades|tojson|safe }};
const pills = document.getElementById('tickerPills');
Object.keys(activeTrades).forEach(ticker => {
const pill = document.createElement('div');
pill.className = 'ticker-pill';
pill.textContent = '🎯 ' + ticker;
pill.onclick = () => {
document.getElementById('tickerSelect').value = ticker;
updateChart();
document.querySelectorAll('.ticker-pill').forEach(p => p.classList.remove('active'));
pill.classList.add('active');
};
pills.appendChild(pill);
});

updateChart();
</script>
</body>
</html>
"""

# =========================================
# CHART PAGE HTML
# =========================================
CHART_HTML = """
<!doctype html>
<html>
<head>
<meta charset='utf-8'>
<title>Chart {{ ticker }}</title>
<style>
body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:20px}
.header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;flex-wrap:wrap;gap:15px}
h1{color:#10b981;margin:0}
.back-btn{background:#3b82f6;color:white;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:bold}
.back-btn:hover{background:#2563eb}
.chart-container{width:100%;height:600px;border-radius:10px;overflow:hidden;border:1px solid #334155;margin-bottom:20px}
.trade-info{background:#1e293b;padding:20px;border-radius:10px;display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:15px}
.info-item{background:#0f172a;padding:15px;border-radius:8px}
.info-label{color:#64748b;font-size:12px;text-transform:uppercase;margin-bottom:5px}
.info-value{font-size:20px;font-weight:bold}
.info-value.buy{color:#10b981}.info-value.sell{color:#ef4444}
.info-value.stop{color:#ef4444}.info-value.target{color:#10b981}
.controls{margin-bottom:15px;display:flex;gap:10px;align-items:center}
.controls select{background:#334155;color:#e2e8f0;border:1px solid #475569;padding:8px 15px;border-radius:6px;font-size:14px}
.signal-badge{display:inline-block;padding:6px 12px;border-radius:6px;font-weight:bold;margin-left:10px}
.signal-buy{background:#10b981;color:white}.signal-sell{background:#ef4444;color:white}
</style>
</head>
<body>
<div class="header">
<h1>📊 {{ ticker }}
{% if trade %}
<span class="signal-badge signal-{{ trade.side|lower }}">{{ trade.side }}</span>
{% elif signal_data %}
<span class="signal-badge signal-{{ signal_data.side|lower }}">{{ signal_data.side }}</span>
{% endif %}
</h1>
<a href="/" class="back-btn">← Torna alla Dashboard</a>
</div>
<div class="controls">
<select id="intervalSelect" onchange="updateChart()">
<option value="D">1 Giorno</option>
<option value="60">1 Ora</option>
<option value="15">15 Minuti</option>
<option value="5">5 Minuti</option>
<option value="1">1 Minuto</option>
</select>
</div>
<div class="chart-container" id="tradingview_chart"></div>
{% if trade or signal_data %}
<div class="trade-info">
{% set data = trade if trade else signal_data %}
<div class="info-item">
<div class="info-label">Side</div>
<div class="info-value {{ data.side|lower }}">{{ data.side }}</div>
</div>
<div class="info-item">
<div class="info-label">Entry Price</div>
<div class="info-value">{{ data.entry|round(2) if data.entry else '-' }} $</div>
</div>
<div class="info-item">
<div class="info-label">Stop Loss</div>
<div class="info-value stop">{{ data.stop|round(2) if data.stop else '-' }} $</div>
</div>
<div class="info-item">
<div class="info-label">Take Profit</div>
<div class="info-value target">{{ data.target|round(2) if data.target else '-' }} $</div>
</div>
{% if trade %}
<div class="info-item">
<div class="info-label">Quantità</div>
<div class="info-value">{{ trade.qty }}</div>
</div>
<div class="info-item">
<div class="info-label">Aperto il</div>
<div class="info-value" style="font-size:16px">{{ trade.ts[:16] }}</div>
</div>
{% endif %}
</div>
{% else %}
<div class="trade-info">
<div class="info-item" style="grid-column:1/-1;text-align:center">
<div class="info-label">Stato</div>
<div class="info-value" style="color:#64748b">Nessun segnale recente su questo ticker</div>
</div>
</div>
{% endif %}
<script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
<script>
function updateChart() {
const interval = document.getElementById('intervalSelect').value;
document.getElementById('tradingview_chart').innerHTML = '';

const studies = [
"MAExp@tv-basicstudies",
"RSI@tv-basicstudies",
"MACD@tv-basicstudies",
"Volume@tv-basicstudies",
"BB@tv-basicstudies"
];

{% if trade or signal_data %}
{% set data = trade if trade else signal_data %}
const entryPrice = {{ data.entry|round(2) if data.entry else 'null' }};
const stopPrice = {{ data.stop|round(2) if data.stop else 'null' }};
const targetPrice = {{ data.target|round(2) if data.target else 'null' }};

if (entryPrice) {
studies.push({
"id": "hline@tv-basicstudies",
"inputs": {
"price": entryPrice,
"text": "ENTRY",
"color": "#10b981",
"linewidth": 2,
"linestyle": 2
}
});
}
if (stopPrice) {
studies.push({
"id": "hline@tv-basicstudies",
"inputs": {
"price": stopPrice,
"text": "STOP",
"color": "#ef4444",
"linewidth": 2,
"linestyle": 2
}
});
}
if (targetPrice) {
studies.push({
"id": "hline@tv-basicstudies",
"inputs": {
"price": targetPrice,
"text": "TARGET",
"color": "#10b981",
"linewidth": 2,
"linestyle": 2
}
});
}
{% endif %}

new TradingView.widget({
"autosize": true,
"symbol": "{{ ticker }}",
"interval": interval,
"timezone": "Europe/Rome",
"theme": "dark",
"style": "1",
"locale": "it",
"toolbar_bg": "#1e293b",
"enable_publishing": false,
"hide_top_toolbar": false,
"hide_legend": false,
"save_image": false,
"container_id": "tradingview_chart",
"studies": studies,
"show_popup_button": true,
"popup_width": "1200",
"popup_height": "800"
});
}
updateChart();
</script>
</body>
</html>
"""

# =========================================
# SIGNALS HTML
# =========================================
SIGNALS_HTML = """
<!doctype html>
<html>
<head>
<meta charset='utf-8'>
<meta http-equiv="refresh" content="60">
<title>Analisi Segnali</title>
<style>
body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;padding:20px}
.card{background:#1e293b;padding:20px;border-radius:10px;margin:15px 0}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:10px;border:1px solid #334155;text-align:left}
th{background:#334155}
.stat{display:inline-block;margin-right:30px;font-size:18px}
.stat-value{font-weight:bold;color:#10b981}
.stat-negative{color:#ef4444}
.stat-warning{color:#f59e0b}
h1{color:#10b981}h2{color:#3b82f6;margin-top:0}
.badge{padding:4px 8px;border-radius:4px;font-size:12px;font-weight:bold}
.badge-buy{background:#10b981;color:white}
.badge-sell{background:#ef4444;color:white}
.badge-win{background:#10b981;color:white}
.badge-loss{background:#ef4444;color:white}
.badge-pending{background:#f59e0b;color:white}
.badge-expired{background:#6b7280;color:white}
.nav{margin-bottom:20px;background:#1e293b;padding:15px;border-radius:10px}
.nav a{color:#3b82f6;margin-right:20px;text-decoration:none;font-weight:bold}
.nav a:hover{text-decoration:underline}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:15px;margin-bottom:20px}
.metric{background:#0f172a;padding:15px;border-radius:8px;text-align:center}
.metric-label{color:#64748b;font-size:12px;text-transform:uppercase}
.metric-value{font-size:24px;font-weight:bold;margin-top:5px}
</style>
</head>
<body>
<h1>📊 Analisi Segnali</h1>
<div class="nav">
<a href="/">🏠 Home</a>
<a href="/signals">📊 Analisi Segnali</a>
<a href="/download-signals">📥 Download CSV</a>
</div>
<div class="card">
<h2>📈 Statistiche Generali</h2>
<div class="grid">
<div class="metric"><div class="metric-label">Totale Segnali</div><div class="metric-value">{{ stats.total }}</div></div>
<div class="metric"><div class="metric-label">Winrate</div><div class="metric-value" style="color:#10b981">{{ stats.winrate }}%</div></div>
<div class="metric"><div class="metric-label">Profit Factor</div><div class="metric-value">{{ stats.profit_factor }}</div></div>
<div class="metric"><div class="metric-label">PnL Totale</div><div class="metric-value {% if stats.total_pnl < 0 %}stat-negative{% else %}stat-value{% endif %}">{{ stats.total_pnl }} €</div></div>
<div class="metric"><div class="metric-label">Vittorie</div><div class="metric-value" style="color:#10b981">{{ stats.wins }}</div></div>
<div class="metric"><div class="metric-label">Perdite</div><div class="metric-value" style="color:#ef4444">{{ stats.losses }}</div></div>
<div class="metric"><div class="metric-label">In Attesa</div><div class="metric-value" style="color:#f59e0b">{{ stats.pending }}</div></div>
<div class="metric"><div class="metric-label">Scaduti</div><div class="metric-value" style="color:#6b7280">{{ stats.expired }}</div></div>
<div class="metric"><div class="metric-label">Win Medio</div><div class="metric-value" style="color:#10b981">{{ stats.avg_win }} €</div></div>
<div class="metric"><div class="metric-label">Loss Medio</div><div class="metric-value" style="color:#ef4444">{{ stats.avg_loss }} €</div></div>
</div>
</div>
<div class="card">
<h2>📜 Ultimi 50 Segnali</h2>
<table>
<tr><th>Data</th><th>Ticker</th><th>Side</th><th>Entry</th><th>Stop</th><th>Target</th><th>Risultato</th><th>Exit</th><th>PnL</th><th>PnL %</th><th>Motivo</th></tr>
{% for s in stats.signals|reverse %}
<tr>
<td>{{ s.timestamp[:16] if s.timestamp else '-' }}</td>
<td><strong>{{ s.ticker }}</strong></td>
<td><span class="badge badge-{{ s.side|lower }}">{{ s.side }}</span></td>
<td>{{ s.entry }}</td>
<td style="color:#ef4444;">{{ s.stop }}</td>
<td style="color:#10b981;">{{ s.target }}</td>
<td><span class="badge badge-{{ s.result|lower }}">{{ s.result }}</span></td>
<td>{{ s.exit_price if s.exit_price else '-' }}</td>
<td {% if s.pnl and s.pnl > 0 %}style="color:#10b981;font-weight:bold"{% elif s.pnl and s.pnl < 0 %}style="color:#ef4444;font-weight:bold"{% endif %}>
{{ s.pnl if s.pnl else '-' }} €</td>
<td>{{ s.pnl_pct if s.pnl_pct else '-' }}%</td>
<td>{{ s.exit_reason if s.exit_reason else '-' }}</td>
</tr>
{% else %}
<tr><td colspan="11" style="text-align:center;">Nessun segnale registrato</td></tr>
{% endfor %}
</table>
</div>
<div class="card" style="text-align:center;color:#64748b;font-size:14px;">
Auto-refresh 60s | {{ now }}
</div>
</body>
</html>
"""

# =========================================
# FLASK APP
# =========================================
app = Flask(__name__)

@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})

@app.route("/")
def home():
    with state_lock:
        total = stats["wins"] + stats["losses"]
        winrate = round((stats["wins"] / total) * 100, 1) if total > 0 else 0
        history_html = "<p style='text-align:center;color:#64748b;'>Nessun trade chiuso</p>"
        
        if os.path.exists(HISTORY_FILE):
            try:
                df = pd.read_csv(HISTORY_FILE)
                if not df.empty:
                    df["result"] = df["result"].apply(lambda x: f'<span class="badge badge-{x.lower()}">{x}</span>')
                    history_html = df.tail(20).to_html(index=False, escape=False, classes="table")
            except Exception as e:
                log.error(f"Error reading history: {e}")
        
        return render_template_string(
            DASHBOARD_HTML,
            stats=stats,
            winrate=winrate,
            active_trades=active_trades,
            max_trades=MAX_TRADES,
            history_html=history_html,
            now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            tickers=TICKERS
        )

@app.route("/chart/<ticker>")
def chart_page(ticker):
    ticker = ticker.upper()
    trade = active_trades.get(ticker)
    signal_data = None
    
    if os.path.exists(SIGNALS_FILE):
        try:
            df_signals = pd.read_csv(SIGNALS_FILE)
            if not df_signals.empty:
                ticker_signals = df_signals[df_signals["ticker"] == ticker].tail(1)
                if not ticker_signals.empty:
                    signal_data = ticker_signals.iloc[0].to_dict()
        except:
            pass
    
    return render_template_string(
        CHART_HTML,
        ticker=ticker,
        trade=trade,
        signal_data=signal_data,
        now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    )

@app.route("/signals")
def signals_page():
    sig_stats = get_signals_stats()
    return render_template_string(SIGNALS_HTML, stats=sig_stats, now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))

@app.route("/download-signals")
def download_signals():
    if not os.path.exists(SIGNALS_FILE):
        return "Nessun segnale registrato", 404
    return send_file(SIGNALS_FILE, as_attachment=True, download_name="signals_log.csv")

@app.route("/api/stats")
def api_stats():
    with state_lock:
        return jsonify({"stats": stats, "active_trades": active_trades, "timestamp": datetime.now(timezone.utc).isoformat()})

@app.route("/api/signals")
def api_signals():
    return jsonify(get_signals_stats())

# =========================================
# SHUTDOWN & MAIN
# =========================================
def handle_sigterm(*_):
    log.info("Shutting down...")
    stop_event.set()

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

if __name__ == "__main__":
    load_alerts()
    log.info("🚀 BOT AVVIATO (Versione 5 - Complete)")
    send_telegram("🚀 BOT ONLINE - Versione 5 completa \n\n📸 Grafici nei messaggi\n🤖 Comandi Telegram\n📊 Report giornaliero\n🛡️ Max Drawdown Protection\n🌐 Filtro mercato")
    
    threading.Thread(target=trading_loop, daemon=True).start()
    threading.Thread(target=monitor_signals, daemon=True).start()
    threading.Thread(target=daily_report_loop, daemon=True).start()
    threading.Thread(target=handle_telegram_commands, daemon=True).start()
    threading.Thread(target=keep_alive_loop, daemon=True).start()
    
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
