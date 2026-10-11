"""Analyses of past decisions, computed from the ledger and daily prices (all USD).

Every function is pure: transactions (engine.Tx), asset metadata and daily price series in,
plain numbers / DataFrames out, so the app can render them and they can be tested offline.
"""
from __future__ import annotations

import pandas as pd

import accounts as A

# Assets whose price is driven mostly by bitcoin (direct BTC, ETFs, leveraged ETFs and
# "bitcoin treasury" companies).
BTC_LINKED = {"BTC", "IBIT", "MSTR", "MSTX", "MTPLF", "MPJPY", "ASST", "TSWCF"}


def _day(t) -> pd.Timestamp:
    return pd.Timestamp(t.timestamp).normalize().tz_localize(None)


def _px(series: pd.Series, day: pd.Timestamp) -> float:
    s = series[series.index <= day]
    return float(s.iloc[-1]) if len(s) else float(series.iloc[0])


def per_asset_pnl(txs, assets, prices) -> pd.DataFrame:
    """Total result per asset: what it's worth now + what came back - what was paid (fees included)."""
    rows = {}
    for t in txs:
        if t.type not in ("BUY", "SELL"):
            continue
        r = rows.setdefault(t.asset_id, {"paid": 0.0, "back": 0.0, "qty": 0.0})
        g, f = float(t.amount * t.price_per_unit), float(t.fee)
        if t.type == "BUY":
            r["paid"] += g + f
            r["qty"] += float(t.amount)
        else:
            r["back"] += g - f
            r["qty"] -= float(t.amount)
    out = []
    for aid, r in rows.items():
        now = max(r["qty"], 0.0) * float(prices[aid].iloc[-1])
        pnl = now + r["back"] - r["paid"]
        out.append({"asset": assets[aid][0], "paid": r["paid"], "now": now, "back": r["back"],
                    "pnl": pnl, "pct": pnl / r["paid"] * 100 if r["paid"] else 0.0, "open": r["qty"] > 1e-9})
    return pd.DataFrame(out).sort_values("pnl", ascending=False) if out else pd.DataFrame()


def sells_vs_holding(txs, assets, prices) -> pd.DataFrame:
    """For each sale: the money received vs. what the same units would be worth today."""
    out = []
    for t in txs:
        if t.type != "SELL" or float(t.price_per_unit) == 0:      # skip network-fee rows (price 0)
            continue
        got = float(t.amount * t.price_per_unit - t.fee)
        if_held = float(t.amount) * float(prices[t.asset_id].iloc[-1])
        out.append({"date": _day(t).date(), "asset": assets[t.asset_id][0], "qty": float(t.amount),
                    "got": got, "if_held": if_held, "saved": got - if_held,
                    "account": A.account_of(t.notes)})
    return pd.DataFrame(out)


def rotations(txs, assets, prices, window_days: int = 14) -> pd.DataFrame:
    """Sold X and used the money to buy Y (same account, within `window_days`).

    Sale proceeds go into a pool; each later buy of a different asset in that account takes
    from the oldest proceeds first, never more than was received. For every switched dollar:
    how Y did from purchase until today vs. how X did over the same period."""
    pool, out = [], []          # pool: [account, sold asset id, dollars left, sale day]
    order = sorted((t for t in txs if t.type in ("BUY", "SELL") and float(t.price_per_unit) > 0),
                   key=lambda t: (_day(t), t.type != "SELL"))
    for t in order:
        acct, day = A.account_of(t.notes), _day(t)
        if t.type == "SELL":
            pool.append([acct, t.asset_id, float(t.amount * t.price_per_unit - t.fee), day])
            continue
        need = float(t.amount * t.price_per_unit)
        for p in pool:
            if need <= 0:
                break
            if p[0] != acct or p[1] == t.asset_id or p[2] <= 0 or (day - p[3]).days > window_days:
                continue
            use = min(need, p[2])
            p[2] -= use
            need -= use
            px, py = prices[p[1]], prices[t.asset_id]
            x_ret = float(px.iloc[-1]) / _px(px, day) - 1             # what was sold, since the switch
            y_ret = float(py.iloc[-1]) / float(t.price_per_unit) - 1   # what was bought, since purchase
            out.append({"date": day.date(), "sold": assets[p[1]][0], "bought": assets[t.asset_id][0],
                        "account": acct, "amount": use, "sold_ret": x_ret, "bought_ret": y_ret,
                        "edge": (y_ret - x_ret) * use})
    df = pd.DataFrame(out)
    if df.empty:
        return df
    g = df.groupby(["sold", "bought", "account"], sort=False)
    res = g.agg(date=("date", "min"), amount=("amount", "sum"), edge=("edge", "sum")).reset_index()
    w = lambda col: g.apply(lambda x: (x[col] * x.amount).sum() / x.amount.sum() * 100, include_groups=False).values
    res["sold_ret"], res["bought_ret"] = w("sold_ret"), w("bought_ret")
    return res.sort_values("date")


def all_in_btc(txs, btc: pd.Series) -> dict:
    """Same money, same dates, but every buy goes into BTC and every sale takes the same dollars out."""
    qty = cash = 0.0
    for t in sorted(txs, key=lambda t: t.timestamp):
        g, f = float(t.amount * t.price_per_unit), float(t.fee)
        p = _px(btc, _day(t))
        if t.type == "BUY":
            spend = g + f
            use = min(cash, spend)
            cash -= use
            qty += spend / p                       # (cash reused first, but all of it buys BTC)
        elif t.type == "SELL" and g > 0:
            out = g - f
            take = min(out / p, qty)
            qty -= take
            cash += take * p
    return {"btc_qty": qty, "value": qty * float(btc.iloc[-1]) + cash}


def fee_summary(txs) -> dict:
    trade = sum(float(t.fee) for t in txs if t.type in ("BUY", "SELL") and not t.notes.startswith("Network fee"))
    account = sum(float(t.fee) for t in txs if t.type == "FEE")
    paid = sum(float(t.amount * t.price_per_unit + t.fee) for t in txs if t.type == "BUY")
    n = sum(1 for t in txs if t.type in ("BUY", "SELL") and float(t.price_per_unit) > 0)
    return {"trade": trade, "account": account, "paid": paid, "trades": n}


def account_fee_drag(txs, value_by_account: dict) -> pd.DataFrame:
    """Account fees over the last 12 months as % of each account's current value."""
    if not txs:
        return pd.DataFrame()
    end = max(_day(t) for t in txs)
    since = end - pd.DateOffset(years=1)
    rows = []
    for acct, val in value_by_account.items():
        fee = sum(float(t.fee) for t in txs if t.type == "FEE" and A.account_of(t.notes) == acct and _day(t) > since)
        if fee:
            rows.append({"account": acct, "fees_12m": fee, "value": val, "pct": fee / val * 100 if val else 0.0})
    return pd.DataFrame(rows)


def concentration(values: dict) -> dict:
    """values: asset symbol -> current USD value."""
    total = sum(v for v in values.values() if v > 0)
    if not total:
        return {}
    top = max(values, key=values.get)
    linked = sum(v for s, v in values.items() if s in BTC_LINKED and v > 0)
    return {"total": total, "top": top, "top_pct": values[top] / total * 100, "btc_linked_pct": linked / total * 100}


# ---------------------------------------------------------------- timing ----

def _close(series: pd.Series, day: pd.Timestamp, offset_days: int = 0):
    """Close on (day + offset); None if that date is still in the future / outside the data."""
    d = day + pd.Timedelta(days=offset_days)
    if d > series.index[-1] or d < series.index[0]:
        return None
    return _px(series, d)


def trade_timing(txs, assets, prices, kind: str, window: int = 30) -> pd.DataFrame:
    """One row per BUY or SELL (kind) with timing measures, all from daily closes:
    - range_pct: where that day's close sat within the lowest..highest close of +-`window` days
                 (0 = the cheapest day of those two months, 100 = the most expensive)
    - before_30: price change over the 30 days before the trade (did you buy after a rise or a dip?)
    - after_30 / after_90: price change in the 30 / 90 days after (None if not that long ago yet)
    - from_ath: distance of that day's close from the highest close so far
    """
    out = []
    for t in txs:
        if t.type != kind or float(t.price_per_unit) <= 0:
            continue
        s, day = prices[t.asset_id], _day(t)
        p = _px(s, day)
        if not p:
            continue
        win = s[(s.index >= day - pd.Timedelta(days=window)) & (s.index <= day + pd.Timedelta(days=window))]
        lo, hi = float(win.min()), float(win.max())
        b30, a30, a90 = _close(s, day, -30), _close(s, day, 30), _close(s, day, 90)
        ath = float(s[s.index <= day].max())
        out.append({"date": day, "asset": assets[t.asset_id][0], "account": A.account_of(t.notes),
                    "usd": float(t.amount * t.price_per_unit), "price": float(t.price_per_unit),
                    "range_pct": (p - lo) / (hi - lo) * 100 if hi > lo else 50.0,
                    "before_30": (p / b30 - 1) * 100 if b30 else None,
                    "after_30": (a30 / p - 1) * 100 if a30 else None,
                    "after_90": (a90 / p - 1) * 100 if a90 else None,
                    "from_ath": (p / ath - 1) * 100 if ath else 0.0})
    return pd.DataFrame(out)


def wavg(df: pd.DataFrame, col: str) -> float | None:
    d = df.dropna(subset=[col])
    return float((d[col] * d.usd).sum() / d.usd.sum()) if len(d) and d.usd.sum() else None


def avg_price_vs_period(txs, prices, aid) -> dict | None:
    """Your average buy price vs. the plain average daily close over the period you were buying."""
    buys = [t for t in txs if t.type == "BUY" and t.asset_id == aid and float(t.price_per_unit) > 0]
    if len(buys) < 2:
        return None
    qty = sum(float(t.amount) for t in buys)
    paid = sum(float(t.amount * t.price_per_unit) for t in buys)
    first, last = min(_day(t) for t in buys), max(_day(t) for t in buys)
    s = prices[aid]
    period = s[(s.index >= first) & (s.index <= last)]
    if period.empty:
        return None
    mine, avg = paid / qty, float(period.mean())
    return {"mine": mine, "period_avg": avg, "diff_pct": (mine / avg - 1) * 100, "first": first, "last": last,
            "n": len(buys)}
