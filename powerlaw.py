"""Bitcoin Power Law (Giovanni Santostasi) and percentile bands around it.

Trend:  price = A * d^N,  d = days since the genesis block (2009-01-03).
Bands:  how far the price sits from the trend (log10 of price / trend) is compared with
        its whole history; the k bands (deciles for k=10, quintiles for k=5) split that
        history into equal parts. Band 1 = the cheapest 10% (or 20%) of days relative to
        the trend, band k = the most expensive.

Used by the app (charts, cards) and by the daily alert (scripts/powerlaw_alert.py).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

GENESIS = pd.Timestamp("2009-01-03")
A, N = 1.0117e-17, 5.82          # Santostasi's published fit


EARLY_CSV = Path(__file__).resolve().parent / "data" / "btc_early.csv"


def with_early_history(recent: pd.Series) -> pd.Series:
    """Daily BTC/USD from the first exchange prices (Mt.Gox, 27/07/2010) up to today.

    Yahoo's BTC-USD starts 17/09/2014; the earlier days come from data/btc_early.csv
    (daily closes 27/07/2010-16/09/2014, from a public historical dataset)."""
    early = pd.read_csv(EARLY_CSV, parse_dates=["date"]).set_index("date")["close"]
    recent = recent.dropna()
    if len(recent):
        early = early[early.index < recent.index[0]]
    return pd.concat([early, recent]).sort_index()


def days(index) -> np.ndarray:
    return np.asarray((pd.DatetimeIndex(index) - GENESIS).days, dtype=float)


def trend(index) -> pd.Series:
    """Power-law fair value (USD) for each date."""
    return pd.Series(A * days(index) ** N, index=pd.DatetimeIndex(index))


def deviation(prices: pd.Series) -> pd.Series:
    """log10(price / trend): 0 = on the trend, +0.3 ~ 2x above, -0.3 ~ half."""
    p = prices.dropna()
    p = p[p > 0]
    return np.log10(p / trend(p.index))


def edges(prices: pd.Series, k: int = 10) -> np.ndarray:
    """Deviation values that split the price history into k equal bands (k-1 inner edges)."""
    return np.quantile(deviation(prices).values, np.linspace(0, 1, k + 1)[1:-1])


def band_of(dev: float, cuts: np.ndarray) -> int:
    """1 = lowest band ... len(cuts)+1 = highest."""
    return int(np.searchsorted(cuts, dev, side="right")) + 1


def percentile_of(dev: float, prices: pd.Series) -> float:
    """Share of history (0-100) that was cheaper relative to the trend than `dev`."""
    d = deviation(prices).values
    return float((d < dev).mean() * 100)


def band_prices(index, cuts: np.ndarray) -> pd.DataFrame:
    """Price level of every band edge on each date (columns e1..e{k-1}) plus the trend."""
    t = trend(index)
    out = pd.DataFrame({f"e{i + 1}": t * 10 ** c for i, c in enumerate(cuts)}, index=t.index)
    out["trend"] = t
    return out


def status(prices: pd.Series, k: int = 10, now_price: float | None = None) -> dict:
    """Where the latest price stands: band, percentile, trend value and today's band edges."""
    p = prices.dropna()
    cuts = edges(p, k)
    day = p.index[-1]
    price = float(now_price if now_price is not None else p.iloc[-1])
    fair = float(trend([day]).iloc[0])
    dev = float(np.log10(price / fair))
    return {"date": day, "price": price, "trend": fair, "ratio": price / fair, "k": k,
            "band": band_of(dev, cuts), "percentile": percentile_of(dev, p),
            "edges_today": [fair * 10 ** c for c in cuts]}
