"""On-chain capitulation metrics from the bitcoin-data.com (BGeometrics) API.

Three short-term-holder (STH, coins moved within ~155 days) metrics that mark the buy zones:
- STH-SOPR: the profit ratio of coins STHs sell. Below 1 = they sell at a loss (capitulation).
- STH realized price: the average cost of STH coins. Price below it = new buyers are under water,
  which historically was a good accumulation zone.
- STH realized loss (USD): the dollar losses STHs lock in. Sharp spikes mark capitulation.

The free plan allows only about 8 requests an hour and 15 a day, and the "Refresh prices" button
clears Streamlit's cache, so every series is also saved to a file and reused for CACHE_HOURS.
If a call fails (rate limit, network), the last saved copy is used, however old.
SOPR and the realized price are delayed by about a week on the free plan; realized loss is not.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.parse
import urllib.request

import pandas as pd

BASE = "https://bitcoin-data.com/v1/"
CACHE_HOURS = 12
CACHE_DIR = os.path.join(tempfile.gettempdir(), "onchain_cache")

# key -> (endpoint, value field in the JSON)
SERIES = {"sopr": ("sth-sopr", "sthSopr"),
          "rp": ("sth-realized-price", "sthRealizedPrice"),
          "loss": ("realized_loss_sth", "realizedLossSth")}

LOSS_SPIKE = 90.0          # a 7-day STH loss above this percentile of the last 4 years = capitulation


def _download(endpoint: str, token: str | None):
    """The raw JSON list; with the token as ?token= first, and without it if that call fails."""
    urls = ([f"{BASE}{endpoint}?{urllib.parse.urlencode({'token': token})}"] if token else []) + [BASE + endpoint]
    err = None
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "portfolio-app"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
            if isinstance(data, dict):                          # an error message or a {"data": [...]} wrapper
                data = next((v for v in data.values() if isinstance(v, list)), None)
            if data:
                return data
            err = "empty response"
        except Exception as e:                                  # noqa: BLE001 - try the next url / the saved copy
            err = str(e)
    raise RuntimeError(err)


def _parse(rows, field: str) -> pd.Series:
    dates, vals = [], []
    for row in rows:
        if not isinstance(row, dict) or row.get(field) in (None, ""):
            continue
        try:
            dates.append(pd.Timestamp(row["d"]).normalize())
            vals.append(float(row[field]))                      # values sometimes come as strings
        except (KeyError, ValueError, TypeError):
            continue
    s = pd.Series(vals, index=pd.DatetimeIndex(dates), dtype=float)
    return s[~s.index.duplicated(keep="last")].sort_index()


def _cached(key: str, token: str | None) -> tuple[pd.Series, float]:
    """(series, age of the data in hours). Downloads only when the saved copy is older than CACHE_HOURS."""
    endpoint, field = SERIES[key]
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{key}.json")
    saved = None
    if os.path.exists(path):
        try:
            with open(path) as f:
                saved = json.load(f)
        except Exception:                                       # noqa: BLE001
            saved = None
    if saved and time.time() - saved["t"] < CACHE_HOURS * 3600:
        return _parse(saved["rows"], field), (time.time() - saved["t"]) / 3600
    try:
        rows = _download(endpoint, token)
        with open(path, "w") as f:
            json.dump({"t": time.time(), "rows": rows}, f)
        return _parse(rows, field), 0.0
    except Exception:
        if saved:                                               # rate limit / network: use the old copy
            return _parse(saved["rows"], field), (time.time() - saved["t"]) / 3600
        raise


def fetch(token: str | None = None) -> dict:
    """{"sopr", "rp", "loss": Series (loss as a positive USD amount), "errors": {key: message}}."""
    out, errors = {}, {}
    for key in SERIES:
        try:
            s, _ = _cached(key, token)
            out[key] = s.abs() if key == "loss" else s
        except Exception as e:                                  # noqa: BLE001
            out[key], errors[key] = None, str(e)
    out["errors"] = errors
    return out


def status(data: dict, price: float) -> dict:
    """Today's reading of each metric and whether it is in its buy / capitulation zone."""
    st = {"signals": 0, "available": 0}
    sopr = data.get("sopr")
    if sopr is not None and len(sopr):
        avg7 = float(sopr.iloc[-7:].mean())
        st["sopr"] = {"value": float(sopr.iloc[-1]), "avg7": avg7, "date": sopr.index[-1], "on": avg7 < 1}
        st["available"] += 1
        st["signals"] += avg7 < 1
    rp = data.get("rp")
    if rp is not None and len(rp):
        v = float(rp.iloc[-1])
        st["rp"] = {"value": v, "date": rp.index[-1], "ratio": price / v, "on": price < v}
        st["available"] += 1
        st["signals"] += price < v
    loss = data.get("loss")
    if loss is not None and len(loss) > 30:
        w = loss.rolling(7).sum().dropna()
        now = float(w.iloc[-1])
        pct = float((w <= now).mean() * 100)
        st["loss"] = {"week": now, "day": float(loss.iloc[-1]), "date": loss.index[-1], "percentile": pct,
                      "spike": float(w.quantile(LOSS_SPIKE / 100)), "on": pct >= LOSS_SPIKE}
        st["available"] += 1
        st["signals"] += pct >= LOSS_SPIKE
    return st
