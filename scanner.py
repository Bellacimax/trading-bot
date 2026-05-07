```python
import yfinance as yf
import pandas as pd

# =========================================
# SCANNER AI
# =========================================

def rank_tickers(tickers):

    ranking = []

    for ticker in tickers:

        try:

            df = yf.download(

                ticker,

                period="5d",

                interval="30m",

                progress=False

            )

            if df is None or df.empty or len(df) < 30:

                continue

            if isinstance(df.columns, pd.MultiIndex):

                df.columns = df.columns.get_level_values(0)

            price = df["Close"].iloc[-1]

            volume = df["Volume"].iloc[-1]

            volume_avg = df["Volume"].rolling(20).mean().iloc[-1]

            # =========================================
            # MOVIMENTO %
            # =========================================

            move = abs(

                (
                    df["Close"].iloc[-1]

                    - df["Close"].iloc[-10]

                )

                / price

            )

            # =========================================
            # VOLUME SCORE
            # =========================================

            volume_score = volume / volume_avg

            # =========================================
            # EMA TREND
            # =========================================

            ema50 = df["Close"].ewm(span=50).mean().iloc[-1]

            ema200 = df["Close"].ewm(span=200).mean().iloc[-1]

            trend_score = 1 if ema50 > ema200 else 0

            # =========================================
            # SCORE FINALE
            # =========================================

            score = (

                move * 100

                + volume_score

                + trend_score * 2

            )

            ranking.append(

                {

                    "ticker": ticker,

                    "score": score

                }

            )

        except:

            pass

    # =========================================
    # SORT
    # =========================================

    ranking = sorted(

        ranking,

        key=lambda x: x["score"],

        reverse=True

    )

    # top 20
    top = [

        x["ticker"]

        for x in ranking[:20]

    ]

    return top
```

