"""Streamlit dashboard. Run:  streamlit run app.py
Env (optional): DATABASE_URL (Postgres/Supabase), USER_ID. Without them the app runs on demo data.
`assets.api_id` is treated as a Yahoo Finance ticker (e.g. SPY, BTC-USD, ETH-USD); 'CASH' = price 1.
"""
import json
import os
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
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


@st.cache_data(ttl=300, show_spinner="מושך מחירים...")
def history(ticker: str, start: str) -> pd.Series:
    if ticker == "CASH":
        return pd.Series(1.0, index=pd.date_range(start, datetime.now().date()))
    h = yf.Ticker(ticker).history(start=start, auto_adjust=True)
    if h.empty:
        return pd.Series(dtype=float)
    s = h["Close"]
    s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
    return s[~s.index.duplicated()]


CG = {"BTC": "bitcoin", "ETH": "ethereum"}   # asset symbol -> CoinGecko id


@st.cache_data(ttl=60)
def live_prices(ids: tuple) -> dict:
    """Near-real-time USD prices from CoinGecko (free, no key). Returns {} on any failure."""
    if not ids:
        return {}
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?vs_currencies=usd&ids=" + ",".join(ids)
        req = urllib.request.Request(url, headers={"User-Agent": "portfolio-app"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return {k: float(v["usd"]) for k, v in json.load(r).items()}
    except Exception:
        return {}


@st.cache_data(ttl=300)
def loaded_at():
    return datetime.now(ZoneInfo("Asia/Jerusalem"))


def daily(s: pd.Series, idx) -> pd.Series:
    if s.empty:
        return pd.Series(float("nan"), index=idx)
    return s.reindex(idx, method="ffill").bfill()


def signed_qty(txs, aid, idx):
    d = {}
    for t in txs:
        if t.asset_id == aid and t.type in ("BUY", "SELL", "STAKING_REWARD"):
            k = pd.Timestamp(t.timestamp).normalize().tz_localize(None)
            d[k] = d.get(k, 0.0) + float(t.amount) * (-1 if t.type == "SELL" else 1)
    return pd.Series(d, dtype=float).sort_index().cumsum().reindex(idx, method="ffill").fillna(0.0) if d \
        else pd.Series(0.0, index=idx)


def contributions(txs, idx):
    """Daily money put in (BUY cost incl. fee) minus money taken out (SELL proceeds), in USD."""
    d = {}
    for t in txs:
        g, f = float(t.amount * t.price_per_unit), float(t.fee)
        v = g + f if t.type == "BUY" else -(g - f) if t.type == "SELL" else 0.0
        if v:
            k = pd.Timestamp(t.timestamp).normalize().tz_localize(None)
            d[k] = d.get(k, 0.0) + v
    return pd.Series(d, dtype=float).reindex(idx).fillna(0.0)


def avg_fx(txs, idx, fx):
    """Weighted-average USD/ILS rate at which the given BUYs were made (1.0 in USD view)."""
    c = contributions([t for t in txs if t.type == "BUY"], idx)
    usd = float(c.sum())
    return float((c * fx).sum()) / usd if usd else 1.0


# ------------------------------ load ------------------------------
(assets, txs), is_demo = load_ledger()
if not txs:
    st.info("אין עסקאות עדיין. הוסף בעמוד add transaction בתפריט הצד")
    st.stop()

start = min(t.timestamp for t in txs).strftime("%Y-%m-%d")
idx = pd.date_range(start, datetime.now().date(), freq="D")

with st.sidebar:
    st.title("⚙️ הגדרות")
    classes = sorted({a[2] for a in assets.values()})
    sel = st.multiselect("סינון לפי סוג נכס", classes, default=classes)
    bench = st.multiselect("מדדי ייחוס", ["SPY", "QQQ", "BTC-USD"], default=["SPY", "QQQ", "BTC-USD"])
    st.subheader("הקצאת יעד (%)")
    default_t = {"equity": 50, "crypto": 35, "cash": 15}
    targets = {c: st.slider(c, 0, 100, default_t.get(c, 0)) for c in classes}
    if sum(targets.values()) != 100:
        st.warning(f"סכום היעדים: {sum(targets.values())}% (אמור להיות 100%)")
    if is_demo:
        st.info("מצב דמו – הגדר DATABASE_URL ו-USER_ID לנתונים אמיתיים")

st.title("📈 Portfolio Tracker")
c1, c2, c3 = st.columns([2, 3, 2])
ccy = c1.radio("מטבע תצוגה", ["USD", "ILS"], horizontal=True)
rng = c2.select_slider("טווח זמן", ["1M", "6M", "1Y", "YTD", "ALL"], value="ALL")
if c3.button("🔄 רענן מחירים"):
    st.cache_data.clear()
    st.rerun()

# ------------------------------ compute ------------------------------
if ccy == "ILS":
    fxs = history("ILS=X", start)
    if fxs.empty:
        st.warning("לא נמצא שער דולר-שקל. משתמש ב-3.0 בקירוב")
        fx = pd.Series(3.0, index=idx)
    else:
        fx = daily(fxs, idx)
else:
    fx = pd.Series(1.0, index=idx)
fx_now = float(fx.iloc[-1])
sym = "₪" if ccy == "ILS" else "$"

live = live_prices(tuple(sorted({CG[a[0]] for a in assets.values() if a[0] in CG})))
prices, vals = {}, {}
for aid, (s, _, cls, api) in assets.items():
    p = daily(history(api, start), idx)
    if p.isna().all():
        st.warning(f"אין מחירים עבור {s} ({api}). בדוק את ה-Ticker בטבלת הנכסים.")
        p = p.fillna(0.0)
    if CG.get(s) in live:
        p.iloc[-1] = live[CG[s]]          # live price for today
    prices[aid] = p
    vals[aid] = signed_qty(txs, aid, idx) * p

tx_f = [t for t in txs if assets[t.asset_id][2] in sel]
ids = [a for a in assets if assets[a][2] in sel]
nw_usd = sum(vals[a] for a in ids)
flows = E.external_flows(tx_f)
twr = E.twr_index(nw_usd, flows)
positions = E.build_positions(tx_f)
summ = E.summarize(positions, {a: D(str(prices[a].iloc[-1])) for a in ids})

real_tx = [t for t in tx_f if not t.notes.startswith("Network fee")]
realized_sales = sum((p.realized_pnl for p in E.build_positions(real_tx).values()), D(0))
net_fee_loss = summ.realized - realized_sales
no_onchain = [t for t in tx_f if not t.notes.startswith("Network fee (on-chain)")]
realized_no_onchain = sum((p.realized_pnl for p in E.build_positions(no_onchain).values()), D(0))
onchain_loss = summ.realized - realized_no_onchain
withdrawal_loss = net_fee_loss - onchain_loss

inv_cum = (contributions(tx_f, idx) * fx).cumsum()      # money invested so far, at each purchase-date rate
invested_now = float(inv_cum.iloc[-1])
nw_now = float(summ.net_worth) * fx_now

afx, unreal_ccy, cost_ccy = {}, 0.0, 0.0
for a in ids:
    if a not in positions:
        continue
    afx[a] = avg_fx([t for t in tx_f if t.asset_id == a], idx, fx)
    cost_a = float(positions[a].cost_basis) * afx[a]
    unreal_ccy += float(summ.by_asset[a]["value"]) * fx_now - cost_a
    cost_ccy += cost_a
unreal_pct = unreal_ccy / cost_ccy * 100 if cost_ccy else 0.0

# ------------------------------ UI ------------------------------
live_txt = " · ".join(f"{s} ${live[CG[s]]:,.0f}" for s in (a[0] for a in assets.values()) if CG.get(s) in live)
st.caption(f"עודכן {loaded_at():%H:%M}" + (f" · מחיר חי (CoinGecko): {live_txt}" if live_txt else "")
           + " · שאר המחירים מ-Yahoo עם עיכוב של כמה דקות")
money = lambda v: f"{'-' if v < 0 else ''}{sym}{abs(v):,.0f}"
pl = nw_now - invested_now
k = st.columns(4)
k[0].metric("שווי נקי", money(nw_now))
k[1].metric("סה״כ הושקע", money(invested_now), f"{'+' if pl >= 0 else '-'}{sym}{abs(pl):,.0f} רווח/הפסד כולל")
k[2].metric("רווח צף (טרם נמכר)", money(unreal_ccy), f"{unreal_pct:+.1f}%")
k[3].metric("רווח ממומש (ממכירות)", money(float(realized_sales) * fx_now))
k2 = st.columns(2)
k2[0].metric("עמלות רשת (on-chain)", money(float(onchain_loss) * fx_now))
k2[1].metric("עמלות משיכה מהבורסות", money(float(withdrawal_loss) * fx_now))
k3 = st.columns(2)
k3[0].metric("TWR – תשואה כוללת", f"{(twr.iloc[-1] - 1) * 100:+.1f}%", f"{E.cagr(twr) * 100:+.1f}% לשנה (CAGR)")
k3[1].metric("Max Drawdown", f"{E.max_drawdown(twr) * 100:.1f}%")
held = [a for a in ids if summ.by_asset[a]["qty"] > 0 and assets[a][2] != "cash"]
if held:
    kc = st.columns(min(len(held), 3))
    for i, a in enumerate(held):
        b = summ.by_asset[a]
        avg = float(b["avg_cost"]) * afx[a]
        now = float(b["price"]) * fx_now
        kc[i % len(kc)].metric(f"מחיר רכישה ממוצע – {assets[a][0]}", f"{sym}{avg:,.0f}",
                               f"{(now / avg - 1) * 100:+.1f}% (מחיר היום {sym}{now:,.0f})")
if ccy == "ILS":
    st.caption("בשקלים: עלות הקנייה מומרת לפי שער הדולר ביום כל קנייה והשווי לפי השער היום, "
               "ולכן הרווח הצף וההפסד הכולל כוללים גם את השפעת שער החליפין. TWR והשוואה למדדים בדולרים.")
with st.expander("ℹ️ מה המדדים אומרים?"):
    st.markdown("""
- **סה״כ הושקע**: כל הכסף ששילמת על קניות מאז ההתחלה, כולל עמלות בורסה.
- **מחיר רכישה ממוצע**: כמה שילמת בממוצע על יחידה אחת (למשל BTC אחד), כולל עמלות בורסה ומשוקלל לפי הכמות שקנית בכל עסקה. בשקלים לפי ממוצע השערים בימי הקנייה.
- **רווח צף**: רווח או הפסד "על הנייר". שווי מה שיש לך היום פחות מה ששילמת עליו. הוא משתנה עם המחיר ומתממש רק כשמוכרים. בתצוגת שקלים: שווי היום בשקלים פחות עלות הקנייה בשקלים, כולל השפעת השער.
- **רווח ממומש**: רווח או הפסד שנסגר במכירה בפועל. אם לא מכרת, הוא 0.
- **עמלות רשת (on-chain)**: מה ששילמת לכורים בהעברות מהארנק שלך, לפי הבלוקצ'יין. בדרך כלל סכום זעיר.
- **עמלות משיכה מהבורסות**: BTC שהבורסה ניכתה ממך כשמשכת לארנק. זו עמלת שירות של הבורסה, לא עמלת רשת, והיא יכולה להיות פרופורציונלית לסכום.
- **TWR**: התשואה של ההשקעה עצמה, בלי קשר לכמה כסף הוספת ומתי. אם כל שקל שהושקע שווה היום 1% פחות, זה -1.0%. מתאים להשוואה מול SPY, QQQ ו-BTC.
- **CAGR**: אותה תשואה מתורגמת לקצב שנתי ממוצע. אם עברו כשנה וחצי ו-TWR הוא -1.0%, ה-CAGR יהיה בערך -0.6% בשנה.
- **Max Drawdown**: הירידה הגדולה ביותר משיא לשפל לפני התאוששות, כלומר כמה כואב היה הרגע הכי גרוע.
""")

t1, t2, t3, t4 = st.tabs(["📊 שווי היסטורי", "🆚 השוואה למדדים", "🥧 הקצאה", "📋 נכסים"])

with t1:
    cmp_df = pd.DataFrame({"שווי": nw_usd * fx, "הושקע": inv_cum})
    f = px.line(E.slice_range(cmp_df, rng), title=f"שווי מול סכום שהושקע ({ccy})",
                labels={"value": f"({ccy})", "index": "", "variable": ""})
    f.update_layout(height=420)
    st.plotly_chart(f, use_container_width=True)
    stack = pd.DataFrame({assets[a][0]: vals[a] * fx for a in ids})
    st.plotly_chart(px.area(E.slice_range(stack, rng), height=350, title="שווי לפי נכס"), use_container_width=True)

with t2:
    sl = E.slice_range(twr, rng)
    fig = go.Figure(go.Scatter(x=sl.index, y=E.rebased(sl), name="Portfolio (TWR)", line=dict(width=3)))
    for b in bench:
        h = history(b, start)
        if h.empty:
            st.warning(f"לא נמצאו מחירים עבור {b}")
            continue
        s = E.slice_range(daily(h, idx), rng)
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
        cost_c = float(positions[a].cost_basis) * afx[a]
        unreal_c = float(b["value"]) * fx_now - cost_c
        held = p[vals[a] > 0] if (vals[a] > 0).any() else p
        rows.append({"נכס": assets[a][0], "סוג": assets[a][2], "כמות": float(b["qty"]),
                     "עלות ממוצעת": float(b["avg_cost"]) * afx[a], "מחיר": float(b["price"]) * fx_now,
                     "שווי": float(b["value"]) * fx_now, "רווח צף": unreal_c,
                     "רווח צף %": unreal_c / cost_c * 100 if cost_c else 0.0, "רווח ממומש": float(b["realized"]) * fx_now,
                     "הכנסות": float(b["income"]) * fx_now,
                     "מרחק מ-ATH %": E.distance_from_ath(p) * 100, "Max DD %": E.max_drawdown(held) * 100})
    st.dataframe(pd.DataFrame(rows).style.format(precision=2), use_container_width=True, hide_index=True)

with st.expander("📒 ספר עסקאות"):
    st.dataframe(pd.DataFrame([{**t.__dict__, "asset": assets[t.asset_id][0]} for t in tx_f]),
                 use_container_width=True, hide_index=True)
