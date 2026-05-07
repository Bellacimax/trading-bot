import csv
import os
from datetime import datetime

FILE_NAME = "trade_history.csv"

# =========================================
# CREA FILE
# =========================================

def init_stats():

    if not os.path.exists(FILE_NAME):

        with open(FILE_NAME, "w", newline="") as f:

            writer = csv.writer(f)

            writer.writerow([

                "time",
                "ticker",
                "side",
                "entry",
                "exit",
                "pnl",
                "rr",
                "result"

            ])

# =========================================
# SALVA TRADE
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

    with open(FILE_NAME, "a", newline="") as f:

        writer = csv.writer(f)

        writer.writerow([

            datetime.now(),

            ticker,

            side,

            round(entry, 2),

            round(exit_price, 2),

            round(pnl, 2),

            rr,

            result

        ])


