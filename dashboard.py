from flask import Flask
import pandas as pd
import os

app = Flask(__name__)


# =========================================
# DASHBOARD
# =========================================

@app.route("/")

def home():

    pnl = 0
    total = 0
    winrate = 0

    table_html = "<p>Nessun trade</p>"

    if os.path.exists("trade_history.csv"):

        df = pd.read_csv("trade_history.csv")

        total = len(df)

        if total > 0:

            wins = len(df[df["pnl"] > 0])

            pnl = round(df["pnl"].sum(), 2)

            winrate = round((wins / total) * 100, 1)

            table_html = df.tail(20).to_html(index=False)

    html = f"""

    <html>

    <head>

        <title>Trading Bot Dashboard</title>

        <style>

            body {{
                background: #111;
                color: white;
                font-family: Arial;
                padding: 30px;
            }}

            h1 {{
                color: #00ff99;
            }}

            .card {{
                background: #1e1e1e;
                padding: 20px;
                border-radius: 10px;
                margin-bottom: 20px;
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

            <p>💰 Total PnL: {pnl} €</p>

            <p>📈 Winrate: {winrate}%</p>

            <p>🎯 Trades: {total}</p>

        </div>

        <div class="card">

            <h2>📜 Ultimi Trade</h2>

            {table_html}

        </div>

    </body>

    </html>

    """

    return html

# =========================================
# START
# =========================================

app.run(host="0.0.0.0", port=8080)
