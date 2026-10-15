"""Checkmate-style "bottom fifth" view: where today's price sits against several mean-reversion
models, measured as a percentile of the whole price history.

For each model the ratio price / model is computed for every day; the percentile is the share
of days (with that model available) on which the ratio was at or below today's. Checkmate
(James Check) treats "every model in the bottom 20% of days" as the deep-value zone where
he keeps DCA-ing, since a bottom is a process rather than a single day.

Realized price (on-chain cost basis) isn't included: it needs on-chain data the app doesn't have.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import powerlaw as PL

BOTTOM = 20.0            # "bottom fifth" threshold, in percent of days

# (key, Hebrew name, short explanation)
MODELS = [("pl", "Power Law", "קו המגמה של Santostasi"),
          ("ma200w", "ממוצע 200 שבוע", "1,400 ימים – רצפת השוק הדובי לאורך השנים"),
          ("ma200d", "ממוצע 200 יום", "המגמה לטווח הבינוני")]


def daily(prices: pd.Series) -> pd.Series:
    """One price per calendar day (gaps carried forward), so day-based averages mean what they say."""
    p = prices.dropna()
    p = p[p > 0]
    return p.resample("D").last().ffill()


def model_lines(prices: pd.Series) -> pd.DataFrame:
    """The model value (USD) of every model on every day; NaN until a model has enough history."""
    p = daily(prices)
    return pd.DataFrame({"pl": PL.trend(p.index).values,
                         "ma200w": p.rolling(1400).mean().values,
                         "ma200d": p.rolling(200).mean().values}, index=p.index)


def ratios(prices: pd.Series) -> pd.DataFrame:
    p = daily(prices)
    return model_lines(p).rdiv(p, axis=0)


def _pct(series: pd.Series, value: float) -> float:
    s = series.dropna().values
    return float((s <= value).mean() * 100) if len(s) else float("nan")


def status(prices: pd.Series, now_price: float | None = None) -> dict:
    """Today's value, ratio and percentile for every model, plus how many sit in the bottom fifth."""
    p = daily(prices)
    lines = model_lines(p)
    r = lines.rdiv(p, axis=0)
    price = float(now_price if now_price is not None else p.iloc[-1])
    out = {"price": price, "date": p.index[-1], "models": []}
    for key, name, note in MODELS:
        value = float(lines[key].iloc[-1])
        ratio = price / value
        # the price level at which this model would enter the bottom fifth today
        edge = value * float(np.nanpercentile(r[key].dropna().values, BOTTOM))
        out["models"].append({"key": key, "name": name, "note": note, "value": value, "ratio": ratio,
                              "percentile": _pct(r[key], ratio), "edge": edge})
    out["in_bottom"] = sum(m["percentile"] <= BOTTOM for m in out["models"])
    return out


def percentile_history(prices: pd.Series, since: str | None = None, step: int = 7) -> pd.DataFrame:
    """Each day's percentile (against all history up to today) for every model, every `step` days."""
    r = ratios(prices)
    out = {}
    for key, _, _ in MODELS:
        s = r[key].dropna()
        ranks = s.rank(pct=True) * 100            # share of days at or below that day's ratio
        out[key] = ranks
    df = pd.DataFrame(out)
    if since:
        df = df[df.index >= pd.Timestamp(since)]
    return df.iloc[::-1][::step].iloc[::-1]       # keep the latest day in the sample
