"""Streamlit dashboard. Run:  streamlit run app.py
Env (optional): DATABASE_URL (Postgres/Supabase), USER_ID. Without them the app runs on demo data.
`assets.api_id` is treated as a Yahoo Finance ticker (e.g. SPY, BTC-USD, ETH-USD); 'CASH' = price 1.
"""
import os
from datetime import datetime
from decimal import Decimal as D

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

import engine as E

st.set_page_config(page_title="Portfolio Tracker", layout="wide", page_icon="📈")

clean = lambda s: "".join(ch for ch in (s or "") if ch.isascii() and not ch.isspace())


# ------------------------------ data layer ------------------------------
def demo_data():
    assets = {1: ("BTC", "Bitcoin", "crypto", "BTC-USD"), 2: ("SPY", "S&P 500 ETF", "equity", "SPY"),
              3: ("ETH", "Ethereum", "crypto", "ETH-USD"), 4: ("CASH", "Cash", "cash", "CASH")}
    t = lambda a, ty, amt, p, fee, d: E.Tx(a, ty, D(amt), D(p), D(fee), "USD", datetime.fromisoformat(d))
    txs = [t(2, "BUY", "20", "400", "2", "2023-02-01"), t(1, "BUY", "0.3", "23000", "5", "2023-03-10"),
           t(3, "BUY", "3", "1700", "3", "2023-06-01"), t(2, "BUY", "10", "480", "2", "2024-04-01"),
           t(1, "BUY", "0.2", "60000", "8", "2024-06-15"), t(2, "DIVIDEND", "30", "1.6", "0", "2024-12-20"),
           t(3, "SELL", "1", "3500", "4", "2025-02-01"), t(1, "STAKING_REWARD", "0.005", "95000", "0", "2025-05-01"),
           t(4, "BUY", "5000", "1", "0", "2025-01-01")]
    return assets, txs


@st.cache_data(ttl=300, show_spinner="טוען נתונים...")
def load_ledger():
    url, uid = clean(os.getenv("DATABASE_URL")), clean(os.getenv("USER_ID"))
    if not (url and uid):
        return demo_data(), True
    import psycopg
    with psycopg.connect(url) as c:
        a = c.execute("SELECT id, symbol, name, asset_class::text, api_id FROM assets").fetchall()
        r = c.execute("""SELECT asset_id, type::text, amount, price_per_unit, fee, currency, "timestamp", coalesce(notes,'')
                         FROM transactions WHERE user_id=%s ORDER BY "timestamp" """, (uid,)).fetchall()
    return ({x[0]: x[1:] for x in a}, [E.Tx(*x) for x in r]), False


@st.cache_data(ttl=3600, show_spinner="מושך מחירים...")
def history(ticker: str, start: str) -> pd.Series:
    if ticker == "CASH":
        return pd.Series(1.0, index=pd.date_range(start, datetime.now().date()))
    h = yf.Ticker(ticker).history(start=start, auto_adjust=True)["Close"]
    h.index = h.index.tz_localize(None).normalize()
    return h[~h.index.duplicated()]


def signed_qty(txs, aid, idx):
    d = {}
    for t in txs:
        if t.asset_id == aid and t.type in ("BUY", "SELL", "STAKING_REWARD"):
            k = pd.Timestamp(t.timestamp).normalize().tz_localize(None)
            d[k] = d.get(k, 0.0) + float(t.amount) * (-1 if t.type == "SELL" else 1)
    return pd.Series(d, dtype=float).sort_index().cumsum().reindex(idx, method="ffill").fillna(0.0) if d \
        else pd.Series(0.0, index=idx)


# ------------------------------ load & compute ------------------------------
(assets, txs), is_demo = load_ledger()
if not txs:
    st.info("אין עסקאות עדיין. הוסף בעמוד add transaction בתפריט הצד")
    st.stop()

start = min(t.timestamp for t in txs).strftime("%Y-%m-%d")
idx = pd.date_range(start, datetime.now().date(), freq="D")

with st.sidebar:
    st.title("⚙️ הגדרות")
    ccy = st.radio("מטבע בסיס", ["USD", "ILS"], horizontal=True)
    rng = st.select_slider("טווח זמן", ["1M", "6M", "1Y", "YTD", "ALL"], value="ALL")
    classes = sorted({a[2] for a in assets.values()})
    sel = st.multiselect("סינון לפי סוג נכס", classes, default=classes)
    bench = st.multiselect("מדדי ייחוס", ["SPY", "BTC-USD"], default=["SPY", "BTC-USD"])
    st.subheader("הקצאת יעד (%)")
    default_t = {"equity": 50, "crypto": 35, "cash": 15}
    targets = {c: st.slider(c, 0, 100, default_t.get(c, 0)) for c in classes}
    if sum(targets.values()) != 100:
        st.warning(f"סכום היעדים: {sum(targets.values())}% (אמור להיות 100%)")
    if is_demo:
        st.info("מצב דמו – הגדר DATABASE_URL ו-USER_ID לנתונים אמיתיים")

fx = history("ILS=X", start).reindex(idx, method="ffill").bfill() if ccy == "ILS" else pd.Series(1.0, index=idx)
fx_now = float(fx.iloc[-1])
sym = "₪" if ccy == "ILS" else "$"

prices, vals = {}, {}
for aid, (s, _, cls, api) in assets.items():
    p = history(api, start).reindex(idx, method="ffill").bfill()
    prices[aid] = p
    vals[aid] = signed_qty(txs, aid, idx) * p

tx_f = [t for t in txs if assets[t.asset_id][2] in sel]
ids = [a for a in assets if assets[a][2] in sel]
nw_usd = sum(vals[a] for a in ids)
flows = E.external_flows(tx_f)
twr = E.twr_index(nw_usd, flows)
positions = E.build_positions(tx_f)
summ = E.summarize(positions, {a: D(str(prices[a].iloc[-1])) for a in ids})

# ------------------------------ UI ------------------------------
st.title("📈 Portfolio Tracker")
k = st.columns(5)
k[0].metric("שווי נקי", f"{sym}{float(summ.net_worth) * fx_now:,.0f}")
k[1].metric("רווח צף", f"{sym}{float(summ.unrealized) * fx_now:,.0f}", f"{float(summ.unrealized_pct):.1f}%")
k[2].metric("רווח ממומש", f"{sym}{float(summ.realized) * fx_now:,.0f}")
k[3].metric("TWR / CAGR", f"{(twr.iloc[-1] - 1) * 100:.1f}%", f"CAGR {E.cagr(twr) * 100:.1f}%")
k[4].metric("Max Drawdown", f"{E.max_drawdown(twr) * 100:.1f}%")

t1, t2, t3, t4 = st.tabs(["📊 שווי היסטורי", "🆚 השוואה למדדים", "🥧 הקצאה", "📋 נכסים"])

with t1:
    nw = E.slice_range(nw_usd * fx, rng)
    f = px.area(nw, labels={"value": f"שווי ({ccy})", "index": ""})
    f.update_layout(showlegend=False, height=420)
    st.plotly_chart(f, use_container_width=True)
    stack = pd.DataFrame({assets[a][0]: vals[a] * fx for a in ids})
    st.plotly_chart(px.area(E.slice_range(stack, rng), height=350, title="שווי לפי נכס"), use_container_width=True)

with t2:
    sl = E.slice_range(twr, rng)
    fig = go.Figure(go.Scatter(x=sl.index, y=E.rebased(sl), name="Portfolio (TWR)", line=dict(width=3)))
    for b in bench:
        s = E.slice_range(history(b, start).reindex(idx, method="ffill").bfill(), rng)
        fig.add_trace(go.Scatter(x=s.index, y=E.rebased(s), name=b))
    fig.update_layout(height=450, yaxis_title="Rebased = 100", hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)

with t3:
    cls_val = {}
    for a in ids:
        cls_val[assets[a][2]] = cls_val.get(assets[a][2], D(0)) + summ.by_asset[a]["value"]
    cur = E.allocation(cls_val)
    drift = E.rebalancing_drift(cur, targets, float(summ.net_worth) * fx_now)
    c1, c2 = st.columns(2)
    c1.plotly_chart(px.pie(names=list(cur), values=list(cur.values()), hole=.45, title="הקצאה נוכחית"),
                    use_container_width=True)
    bar = go.Figure([go.Bar(x=drift.index, y=drift.current_pct, name="נוכחי"),
                     go.Bar(x=drift.index, y=drift.target_pct, name="יעד")])
    bar.update_layout(barmode="group", title="נוכחי מול יעד")
    c2.plotly_chart(bar, use_container_width=True)
    st.dataframe(drift.style.format({"current_pct": "{:.1f}", "target_pct": "{:.1f}",
                                     "drift_pp": "{:+.1f}", "rebalance_usd": f"{sym}{{:+,.0f}}"})
                 .background_gradient(subset=["drift_pp"], cmap="RdYlGn_r"), use_container_width=True)

with t4:
    rows = []
    for a in ids:
        b, p = summ.by_asset[a], prices[a]
        held = p[vals[a] > 0] if (vals[a] > 0).any() else p
        rows.append({"נכס": assets[a][0], "סוג": assets[a][2], "כמות": float(b["qty"]),
                     "עלות ממוצעת": float(b["avg_cost"]) * fx_now, "מחיר": float(b["price"]) * fx_now,
                     "שווי": float(b["value"]) * fx_now, "רווח צף": float(b["unrealized"]) * fx_now,
                     "רווח צף %": float(b["unrealized_pct"]), "רווח ממומש": float(b["realized"]) * fx_now,
                     "הכנסות": float(b["income"]) * fx_now,
                     "מרחק מ-ATH %": E.distance_from_ath(p) * 100, "Max DD %": E.max_drawdown(held) * 100})
    st.dataframe(pd.DataFrame(rows).style.format(precision=2), use_container_width=True, hide_index=True)

with st.expander("📒 ספר עסקאות"):
    st.dataframe(pd.DataFrame([{**t.__dict__, "asset": assets[t.asset_id][0]} for t in tx_f]),
                 use_container_width=True, hide_index=True)
