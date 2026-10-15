"""Checkmate-style "bottom fifth" view: where today's price sits against several mean-reversion
models, measured as a percentile of the whole price history.

For each model the ratio price / model is computed for every day; the percentile is the share
of days (with that model available) on which the ratio was at or below today's. Checkmate
(James Check) treats "every model in the bottom 20% of days" as the deep-value zone where
he keeps DCA-ing, since a bottom is a process rather than a single day.

Realized price (the average on-chain cost basis of all coins, Checkmate's main model) comes from
the Bitbo API when BITBO_API_KEY is set in the Secrets (full history), otherwise from the free
bitcoin-data.com API (no key, about a week delayed, shorter history).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

import powerlaw as PL

BOTTOM = 20.0            # "bottom fifth" threshold, in percent of days

# (key, Hebrew name, short explanation)
RP = ("rp", "Realized Price", "מחיר העלות הממוצע של כל המטבעות on-chain")
MODELS = [("pl", "Power Law", "קו המגמה של Santostasi"),
          ("ma200w", "ממוצע 200 שבוע", "1,400 ימים – רצפת השוק הדובי לאורך השנים"),
          ("ma200d", "ממוצע 200 יום", "המגמה לטווח הבינוני")]


def _get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": "portfolio-app", **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _to_series(payload) -> pd.Series:
    """Turn a time-series JSON payload into a date-indexed Series, whatever its exact shape:
    a list (or {"data": list}) of [timestamp, value] pairs or of {date-ish, value-ish} objects."""
    rows = payload
    if isinstance(rows, dict):
        rows = next((v for k, v in rows.items() if isinstance(v, list)), [])
    dates, vals = [], []
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            t, v = row[0], row[1]
        elif isinstance(row, dict):
            t = next((row[k] for k in ("d", "date", "day", "time", "timestamp", "t", "unixTs") if k in row), None)
            v = next((row[k] for k in ("realizedPrice", "realized_price", "value", "v", "y") if k in row), None)
            if v is None:                                   # fall back to the first numeric field
                v = next((x for k, x in row.items() if isinstance(x, (int, float)) and k not in ("unixTs", "timestamp", "t")), None)
        else:
            continue
        if t is None or v is None:
            continue
        if isinstance(t, (int, float)):
            t = pd.to_datetime(t, unit="ms" if t > 1e11 else "s")
        dates.append(pd.Timestamp(t).tz_localize(None).normalize() if pd.Timestamp(t).tzinfo else pd.Timestamp(t).normalize())
        vals.append(float(v))
    s = pd.Series(vals, index=pd.DatetimeIndex(dates), dtype=float)
    return s[~s.index.duplicated(keep="last")].sort_index()


def realized_price(bitbo_key: str | None = None) -> tuple[pd.Series, str]:
    """Daily realized price (USD) and the name of the source it came from.
    Bitbo when a key is given (falls back to the free source if that call fails)."""
    if bitbo_key:
        try:
            q = urllib.parse.urlencode({"start_date": "2010-07-17", "api_key": bitbo_key})
            s = _to_series(_get(f"https://charts.bitbo.io/api/v1/realized-price/?{q}",
                                {"Authorization": f"Bearer {bitbo_key}"}))
            if len(s):
                return s, "Bitbo"
        except Exception:
            pass
    s = _to_series(_get("https://bitcoin-data.com/api/v1/realized-price"))
    return s, "bitcoin-data.com"


def daily(prices: pd.Series) -> pd.Series:
    """One price per calendar day (gaps carried forward), so day-based averages mean what they say."""
    p = prices.dropna()
    p = p[p > 0]
    return p.resample("D").last().ffill()


def models(realized: pd.Series | None = None) -> list:
    return ([RP] if realized is not None and len(realized) else []) + MODELS


def model_lines(prices: pd.Series, realized: pd.Series | None = None) -> pd.DataFrame:
    """The model value (USD) of every model on every day; NaN until a model has enough history.
    Realized price is carried forward over its last few (delayed) days."""
    p = daily(prices)
    df = pd.DataFrame({"pl": PL.trend(p.index).values,
                       "ma200w": p.rolling(1400).mean().values,
                       "ma200d": p.rolling(200).mean().values}, index=p.index)
    if realized is not None and len(realized):
        r = realized[realized > 0].resample("D").last()
        df["rp"] = r.reindex(df.index).ffill().where(df.index >= r.index[0])
    return df


def ratios(prices: pd.Series, realized: pd.Series | None = None) -> pd.DataFrame:
    p = daily(prices)
    return model_lines(p, realized).rdiv(p, axis=0)


def _pct(series: pd.Series, value: float) -> float:
    s = series.dropna().values
    return float((s <= value).mean() * 100) if len(s) else float("nan")


def status(prices: pd.Series, now_price: float | None = None, realized: pd.Series | None = None) -> dict:
    """Today's value, ratio and percentile for every model, plus how many sit in the bottom fifth."""
    p = daily(prices)
    lines = model_lines(p, realized)
    r = lines.rdiv(p, axis=0)
    price = float(now_price if now_price is not None else p.iloc[-1])
    out = {"price": price, "date": p.index[-1], "models": []}
    for key, name, note in models(realized):
        value = float(lines[key].iloc[-1])
        ratio = price / value
        # the price level at which this model would enter the bottom fifth today
        edge = value * float(np.nanpercentile(r[key].dropna().values, BOTTOM))
        out["models"].append({"key": key, "name": name, "note": note, "value": value, "ratio": ratio,
                              "percentile": _pct(r[key], ratio), "edge": edge,
                              "since": r[key].first_valid_index()})
    out["in_bottom"] = sum(m["percentile"] <= BOTTOM for m in out["models"])
    return out


def percentile_history(prices: pd.Series, since: str | None = None, step: int = 7,
                       realized: pd.Series | None = None) -> pd.DataFrame:
    """Each day's percentile (against all history up to today) for every model, every `step` days."""
    r = ratios(prices, realized)
    out = {}
    for key, _, _ in models(realized):
        s = r[key].dropna()
        ranks = s.rank(pct=True) * 100            # share of days at or below that day's ratio
        out[key] = ranks
    df = pd.DataFrame(out)
    if since:
        df = df[df.index >= pd.Timestamp(since)]
    return df.iloc[::-1][::step].iloc[::-1]       # keep the latest day in the sample
