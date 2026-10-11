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

import accounts as A
import engine as E
import insights as I

st.set_page_config(page_title="Portfolio Tracker", layout="wide", page_icon="📈")

import ui
ui.rtl()
import gate
gate.require_login()  # nothing below runs without the password

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
    """Daily money put in (BUY cost incl. fee, account fees) minus money taken out (SELL proceeds), in USD."""
    d = {}
    for t in txs:
        g, f = float(t.amount * t.price_per_unit), float(t.fee)
        v = g + f if t.type == "BUY" else -(g - f) if t.type == "SELL" else f if t.type == "FEE" else 0.0
        if v:
            k = pd.Timestamp(t.timestamp).normalize().tz_localize(None)
            d[k] = d.get(k, 0.0) + v
    return pd.Series(d, dtype=float).reindex(idx).fillna(0.0)


def cash_model(txs, idx):
    """Money from outside vs. money recycled inside the portfolio.

    Selling and buying something else a few days later is not new money: the proceeds wait
    as cash. New money is needed only when spending goes above everything put in so far.
    Returns (daily new money in, USD; idle cash balance, USD)."""
    spent = contributions(txs, idx).cumsum()          # net spend so far (buys + fees - sells)
    funded = spent.cummax().clip(lower=0.0)           # most that was ever needed from outside
    return funded.diff().fillna(funded.iloc[0]), funded - spent


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

st.title("📈 Portfolio Tracker")

# ------------------------------ account sections ------------------------------
acct_txs = {a: [t for t in txs if A.account_of(t.notes) == a] for a in A.ORDER}
present = [a for a in A.ORDER if acct_txs[a]]
chosen = present
if len(present) > 1:
    if hasattr(st, "pills"):
        chosen = st.pills("תיקים", present, selection_mode="multi", default=present) or []
    else:
        chosen = st.multiselect("תיקים", present, default=present)
    if not chosen:
        st.info("בחר לפחות תיק אחד")
        st.stop()
    chosen = [a for a in present if a in chosen]
    txs = [t for a in chosen for t in acct_txs[a]]
    assets = {a: v for a, v in assets.items() if any(t.asset_id == a for t in txs)}

start = min(t.timestamp for t in txs).strftime("%Y-%m-%d")
idx = pd.date_range(start, datetime.now().date(), freq="D")

with st.sidebar:
    st.title("⚙️ הגדרות")
    classes = sorted({a[2] for a in assets.values()})
    sel = st.multiselect("סינון לפי סוג נכס", classes, default=classes)
    bench = st.multiselect("מדדי ייחוס", ["SPY", "QQQ", "BTC-USD"], default=["SPY", "QQQ", "BTC-USD"])
    dca_on = st.checkbox("השוואת BTC ל-DCA יומי", value=True,
                         help="הכסף שהשקעת בביטקוין בלבד, מחולק שווה על כל יום מהרכישה הראשונה של BTC ועד היום")
    st.subheader("הקצאת יעד (%)")
    default_t = {"equity": 50, "crypto": 35, "cash": 15}
    targets = {c: st.slider(c, 0, 100, default_t.get(c, 0)) for c in classes}
    if sum(targets.values()) != 100:
        st.warning(f"סכום היעדים: {sum(targets.values())}% (אמור להיות 100%)")
    if is_demo:
        st.info("מצב דמו – הגדר DATABASE_URL ו-USER_ID לנתונים אמיתיים")

c1, c2, c3 = st.columns([2, 3, 2])
ccy = c1.radio("מטבע תצוגה", ["USD", "ILS"], horizontal=True)
rng = c2.select_slider("טווח זמן", ["1M", "6M", "1Y", "YTD", "ALL"], value="ALL")
if c3.button("🔄 רענן מחירים"):
    st.cache_data.clear()
    st.rerun()

# Plotly toolbar: zoom, pan, reset stay available; tapping the chart shows the values (hovermode "x unified")
CHART_CFG = {"displayModeBar": True, "displaylogo": False, "doubleClick": "reset",
             "modeBarButtonsToRemove": ["select2d", "lasso2d", "toImage"]}

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
new_money, cash_usd = cash_model(tx_f, idx)
nw_usd = sum(vals[a] for a in ids) + cash_usd       # holdings + proceeds waiting to be reinvested
twr = E.twr_index(nw_usd, new_money)
positions = E.build_positions(tx_f)
summ = E.summarize(positions, {a: D(str(prices[a].iloc[-1])) for a in ids})

real_tx = [t for t in tx_f if not t.notes.startswith("Network fee")]
realized_sales = sum((p.realized_pnl for p in E.build_positions(real_tx).values()), D(0))
net_fee_loss = summ.realized - realized_sales
no_onchain = [t for t in tx_f if not t.notes.startswith("Network fee (on-chain)")]
realized_no_onchain = sum((p.realized_pnl for p in E.build_positions(no_onchain).values()), D(0))
onchain_loss = summ.realized - realized_no_onchain
withdrawal_loss = net_fee_loss - onchain_loss

inv_cum = (new_money * fx).cumsum()      # money put in from outside so far, at each day's rate
invested_now = float(inv_cum.iloc[-1])

# --- benchmark: the money you put into BTC, spread evenly over every day instead (daily DCA) ---
dca = None
btc_ids = [a for a in ids if assets[a][0] == "BTC"]
btc_tx = [t for t in tx_f if t.asset_id in btc_ids]
if dca_on and btc_tx:
    btc_h = daily(history("BTC-USD", start), idx)
    if CG["BTC"] in live and not btc_h.isna().all():
        btc_h.iloc[-1] = live[CG["BTC"]]
    btc_new, _ = cash_model(btc_tx, idx)
    total_usd = float(btc_new.sum())
    first = pd.Timestamp(min(t.timestamp for t in btc_tx)).normalize().tz_localize(None)
    days = idx[idx >= first]
    if total_usd > 0 and len(days) and not btc_h.isna().any():
        per_day = total_usd / len(days)
        buy = pd.Series(0.0, index=idx)
        buy[days] = per_day
        dca_qty = (buy / btc_h).cumsum()
        dca_usd = dca_qty * btc_h
        mine = sum(vals[a] for a in btc_ids)
        dca = {"usd": dca_usd, "qty": float(dca_qty.iloc[-1]), "total": total_usd, "per_day": per_day,
               "mine": mine, "inv": (btc_new * fx).cumsum(), "start": first,
               "twr": E.twr_index(dca_usd, buy)}
    else:
        st.sidebar.warning("אין מספיק נתונים לחישוב DCA יומי")
nw_now = (float(summ.net_worth) + float(cash_usd.iloc[-1])) * fx_now

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


def grid(cards, key, per_row=3):
    """Metric cards in rows of `per_row` (kept side by side on phones too, see CSS below)."""
    with st.container(key=key):
        for i in range(0, len(cards), per_row):
            cols = st.columns(per_row)
            for col, (label, value, delta) in zip(cols, cards[i:i + per_row]):
                col.metric(label, value, delta)


st.markdown("""<style>
[class*="st-key-kpi"] [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: .5rem; }
[class*="st-key-kpi"] [data-testid="stColumn"], [class*="st-key-kpi"] [data-testid="column"] {
    min-width: 0 !important; width: calc(33.3% - .35rem) !important; flex: 1 1 0 !important; }
[class*="st-key-kpi"] [data-testid="stMetric"] { background: rgba(128,128,128,.08); border-radius: .6rem; padding: .5rem .6rem; }
[class*="st-key-kpi"] [data-testid="stMetricLabel"] p { font-size: .75rem; white-space: normal; line-height: 1.2; }
[class*="st-key-kpi"] [data-testid="stMetricValue"] { font-size: 1.15rem; }
[class*="st-key-kpi"] [data-testid="stMetricDelta"] { font-size: .7rem; }
</style>""", unsafe_allow_html=True)

cards = [("שווי נקי", money(nw_now), None),
         ("סה״כ הושקע", money(invested_now), None),
         ("רווח/הפסד (בניכוי עמלות)", money(pl), f"{pl / invested_now * 100 if invested_now else 0.0:+.1f}%"),
         ("רווח ממומש (ממכירות)", money(float(realized_sales) * fx_now), None),
         ("TWR – תשואה כוללת", f"{(twr.iloc[-1] - 1) * 100:+.1f}%", f"{E.cagr(twr) * 100:+.1f}% לשנה"),
         ("Max Drawdown", f"{E.max_drawdown(twr) * 100:.1f}%", None)]
if A.COLD in chosen:                           # bitcoin network fees exist only in the cold wallet
    cards += [("עמלות רשת (on-chain)", money(float(onchain_loss) * fx_now), None),
              ("עמלות משיכה מהבורסות", money(float(withdrawal_loss) * fx_now), None)]
grid(cards, "kpi_main")

if len(chosen) > 1:
    st.subheader("לפי תיק")
    acards = []
    for a in chosen:
        at = [t for t in acct_txs[a] if assets[t.asset_id][2] in sel]
        a_new, a_cash = cash_model(at, idx)
        v = (sum(float(signed_qty(at, aid, idx).iloc[-1]) * float(prices[aid].iloc[-1]) for aid in assets)
             + float(a_cash.iloc[-1])) * fx_now
        inv = float((a_new * fx).sum())
        acards.append((a, money(v), f"{money(v - inv)} ({(v - inv) / inv * 100:+.1f}%)" if inv else None))
    grid(acards, "kpi_accounts")

held = [a for a in ids if summ.by_asset[a]["qty"] > 0 and assets[a][2] != "cash"]
if held:
    st.subheader("מחיר רכישה ממוצע")
    hcards = []
    for a in held:
        b = summ.by_asset[a]
        avg = float(b["avg_cost"]) * afx[a]
        now = float(b["price"]) * fx_now
        fmt = (lambda x: f"{sym}{x:,.2f}") if avg < 100 else (lambda x: f"{sym}{x:,.0f}")
        hcards.append((assets[a][0], fmt(avg), f"{(now / avg - 1) * 100:+.1f}% (היום {fmt(now)})"))
    grid(hcards, "kpi_assets")
if ccy == "ILS":
    st.caption("בשקלים: עלות הקנייה מומרת לפי שער הדולר ביום כל קנייה והשווי לפי השער היום, "
               "ולכן הרווח וההפסד כוללים גם את השפעת שער החליפין. TWR והשוואה למדדים בדולרים.")
with st.expander("ℹ️ מה המדדים אומרים?"):
    st.markdown("""
- **סה״כ הושקע**: כל הכסף ששילמת על קניות מאז ההתחלה, כולל עמלות בורסה.
- **מחיר רכישה ממוצע**: כמה שילמת בממוצע על יחידה אחת (למשל BTC אחד), כולל עמלות בורסה ומשוקלל לפי הכמות שקנית בכל עסקה. בשקלים לפי ממוצע השערים בימי הקנייה.
- **רווח/הפסד (בניכוי עמלות)**: מה שיש לך היום שווה פחות מה ששילמת בפועל. הוא כולל את עמלות הבורסה (שנכללות בסכום ששילמת) ואת ה-BTC שנוכה בעמלות רשת ובמשיכות. הוא "על הנייר" ומשתנה עם המחיר, ומתממש רק כשמוכרים. בתצוגת שקלים: שווי היום בשקלים פחות עלות הקנייה בשקלים, כולל השפעת השער.
- **רווח ממומש**: רווח או הפסד שנסגר במכירה בפועל. אם לא מכרת, הוא 0.
- **עמלות רשת (on-chain)**: מה ששילמת לכורים בהעברות מהארנק שלך, לפי הבלוקצ'יין. בדרך כלל סכום זעיר.
- **עמלות משיכה מהבורסות**: BTC שהבורסה ניכתה ממך כשמשכת לארנק. זו עמלת שירות של הבורסה, לא עמלת רשת, והיא יכולה להיות פרופורציונלית לסכום.
- **TWR**: התשואה של ההשקעה עצמה, בלי קשר לכמה כסף הוספת ומתי. אם כל שקל שהושקע שווה היום 1% פחות, זה -1.0%. מתאים להשוואה מול SPY, QQQ ו-BTC.
- **CAGR**: אותה תשואה מתורגמת לקצב שנתי ממוצע. אם עברו כשנה וחצי ו-TWR הוא -1.0%, ה-CAGR יהיה בערך -0.6% בשנה.
- **Max Drawdown**: הירידה הגדולה ביותר משיא לשפל לפני התאוששות, כלומר כמה כואב היה הרגע הכי גרוע.
""")

t1, t2, t3, t4, t5 = st.tabs(["📊 שווי היסטורי", "🆚 השוואה למדדים", "🥧 הקצאה", "📋 נכסים", "💡 תובנות"])

with t1:
    cmp_df = pd.DataFrame({"שווי": nw_usd * fx, "הושקע": inv_cum})
    sl1 = E.slice_range(cmp_df, rng)
    f = go.Figure()
    for col in sl1.columns:
        f.add_trace(go.Scatter(x=sl1.index, y=sl1[col], name=col, hovertemplate=sym + "%{y:,.0f}"))
    if btc_ids:                                           # BTC price on its own (right) axis
        bp = E.slice_range(pd.DataFrame({"p": prices[btc_ids[0]] * fx}), rng)["p"]
        f.add_trace(go.Scatter(x=bp.index, y=bp, name=f"מחיר BTC ({sym})", yaxis="y2",
                               line=dict(color="#9aa0a6", width=1.5, dash="dot"),
                               hovertemplate=sym + "%{y:,.0f}"))
    f.update_layout(title=f"שווי מול סכום שהושקע ({ccy})", height=480, hovermode="x unified",
                    legend=dict(orientation="h", y=-0.18), margin=dict(l=10, r=10),
                    yaxis=dict(title=f"({ccy})"),
                    yaxis2=dict(title=f"מחיר BTC ({ccy})", overlaying="y", side="right", showgrid=False))
    st.plotly_chart(f, use_container_width=True, config=CHART_CFG)
    stack = pd.DataFrame({assets[a][0]: vals[a] * fx for a in ids})
    if (cash_usd > 0.5).any():
        stack["מזומן"] = cash_usd * fx
    st.plotly_chart(px.area(E.slice_range(stack, rng), height=350, title="שווי לפי נכס"), use_container_width=True)
    if dca:                                               # bitcoin only: my BTC buys vs. daily DCA
        bd = pd.DataFrame({"ה-BTC שלי": dca["mine"] * fx, "DCA יומי": dca["usd"] * fx,
                           "הושקע ב-BTC": dca["inv"]})
        bd = E.slice_range(bd[bd.index >= dca["start"]], rng)
        fb = go.Figure()
        for col in bd.columns:
            fb.add_trace(go.Scatter(x=bd.index, y=bd[col], name=col, hovertemplate=sym + "%{y:,.0f}"))
        fb.update_layout(title=f"ביטקוין: הרכישות שלי מול DCA יומי ({ccy})", height=420, hovermode="x unified",
                         legend=dict(orientation="h", y=-0.18), margin=dict(l=10, r=10))
        st.plotly_chart(fb, use_container_width=True, config=CHART_CFG)

with t2:
    if dca:
        dca_now = float(dca["usd"].iloc[-1]) * fx_now
        mine_now = float(dca["mine"].iloc[-1]) * fx_now
        diff = mine_now - dca_now
        my_qty = sum(float(summ.by_asset[a]["qty"]) for a in btc_ids)
        grid([("BTC שלי – שווי", f"{sym}{mine_now:,.0f}", f"{'+' if diff >= 0 else '-'}{sym}{abs(diff):,.0f} מול DCA"),
              ("DCA יומי – שווי", f"{sym}{dca_now:,.0f}", None),
              ("BTC: שלי / DCA", f"{my_qty:.4f} / {dca['qty']:.4f}", None)], "kpi_dca")
        st.caption(f"DCA יומי: אותו סכום שהשקעת בביטקוין (${dca['total']:,.0f}), מחולק שווה לכ-${dca['per_day']:,.0f} ליום "
                   f"מהרכישה הראשונה של BTC ועד היום, לפי מחיר הסגירה היומי. ההשוואה היא רק לביטקוין, "
                   f"בלי הנכסים האחרים. ההפרש מראה אם הקניות שלך הקדימו (+) או פיגרו (-) אחרי קנייה יומית קבועה.")
    sl = E.slice_range(twr, rng)
    fig = go.Figure(go.Scatter(x=sl.index, y=E.rebased(sl), name="Portfolio (TWR)", line=dict(width=3)))
    for b in bench:
        h = history(b, start)
        if h.empty:
            st.warning(f"לא נמצאו מחירים עבור {b}")
            continue
        s = E.slice_range(daily(h, idx), rng)
        fig.add_trace(go.Scatter(x=s.index, y=E.rebased(s), name=b))
    if dca:
        d = E.slice_range(dca["twr"], rng)
        fig.add_trace(go.Scatter(x=d.index, y=E.rebased(d), name="DCA יומי ל-BTC", line=dict(dash="dash")))
    fig.update_layout(height=450, yaxis_title="Rebased = 100", hovermode="x unified",
                      legend=dict(orientation="h", y=-0.18), margin=dict(l=10, r=10))
    st.plotly_chart(fig, use_container_width=True, config=CHART_CFG)

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
        # net P/L after fees: unrealized + realized (realized carries the network/withdrawal fee losses)
        unreal_c = float(b["value"]) * fx_now - cost_c + float(b["realized"]) * afx[a]
        paid_c = float(positions[a].total_invested) * afx[a]
        held = p[vals[a] > 0] if (vals[a] > 0).any() else p
        rows.append({"נכס": assets[a][0], "סוג": assets[a][2], "כמות": float(b["qty"]),
                     "עלות ממוצעת": float(b["avg_cost"]) * afx[a], "מחיר": float(b["price"]) * fx_now,
                     "שווי": float(b["value"]) * fx_now, "רווח/הפסד": unreal_c,
                     "רווח/הפסד %": unreal_c / paid_c * 100 if paid_c else 0.0, "רווח ממומש": float(b["realized"]) * fx_now,
                     "הכנסות": float(b["income"]) * fx_now,
                     "מרחק מ-ATH %": E.distance_from_ath(p) * 100, "Max DD %": E.max_drawdown(held) * 100})
    st.dataframe(pd.DataFrame(rows).style.format(precision=2), use_container_width=True, hide_index=True)

with t5:
    usd = lambda v: money(v * fx_now)                     # analyses run in USD; shown in the display currency
    pct = lambda v: f"{v:+.1f}%"
    st.caption("ניתוח התזמון של הקניות והמכירות, לפי התיקים שבחרת למעלה. תיאור של מה שקרה, לא המלצה.")

    buys_t = I.trade_timing(tx_f, assets, prices, "BUY")
    sells_t = I.trade_timing(tx_f, assets, prices, "SELL")

    def share(df, mask):
        return df[mask].usd.sum() / df.usd.sum() * 100 if df.usd.sum() else 0.0

    def buy_lines(df):
        out = []
        rp = I.wavg(df, "range_pct")
        verdict = ("קנית קרוב לתחתיות המקומיות 👍" if rp < 35 else "נטייה לקנות קרוב לשיאים מקומיים" if rp > 65
                   else "בערך כמו קנייה בזמן אקראי")
        out.append(f"בממוצע קנית ב-**{rp:.0f}%** מהטווח של החודש שלפני ואחרי כל קנייה "
                   f"(0% = היום הכי זול, 100% = הכי יקר) – {verdict}.")
        up, down = df.before_30 > 10, df.before_30 < -10
        out.append(f"**{share(df, up):.0f}%** מהכסף נקנה אחרי עלייה של 10%+ בחודש שלפני, "
                   f"ו-**{share(df, down):.0f}%** אחרי ירידה של 10%+.")
        u90, d90 = I.wavg(df[up], "after_90"), I.wavg(df[down], "after_90")
        if u90 is not None and d90 is not None:
            better = "אחרי ירידות" if d90 > u90 else "אחרי עליות"
            out.append(f"90 יום אחרי הקנייה: קניות אחרי ירידה {pct(d90)}, קניות אחרי עלייה {pct(u90)} – "
                       f"אצלך הקניות {better} עבדו טוב יותר.")
        a90 = I.wavg(df, "after_90")
        if a90 is not None:
            out.append(f"בממוצע, 90 יום אחרי קנייה המחיר היה {pct(a90)} מהמחיר שקנית.")
        d = df.dropna(subset=["after_90"])
        if len(d) >= 3:
            b, w = d.loc[d.after_90.idxmax()], d.loc[d.after_90.idxmin()]
            out.append(f"הקנייה עם התזמון הכי טוב: {b.asset} ב-{b.date:%d/%m/%Y} ({pct(b.after_90)} תוך 90 יום). "
                       f"הכי גרוע: {w.asset} ב-{w.date:%d/%m/%Y} ({pct(w.after_90)}).")
        return out

    st.subheader("🟢 קניות")
    btc_b = buys_t[buys_t.asset == "BTC"] if not buys_t.empty else buys_t
    stk_b = buys_t[buys_t.asset != "BTC"] if not buys_t.empty else buys_t
    if not btc_b.empty:
        st.markdown("**ביטקוין**")
        lines = []
        bid = next(a for a in assets if assets[a][0] == "BTC")
        ap = I.avg_price_vs_period(tx_f, prices, bid)
        if ap:
            lines.append(f"המחיר הממוצע שלך **{usd(ap['mine'])}** מול **{usd(ap['period_avg'])}** – הממוצע היומי בתקופה "
                         f"שקנית ({ap['first']:%m/%Y}–{ap['last']:%m/%Y}). כלומר קנית "
                         f"**{abs(ap['diff_pct']):.1f}% {'בזול' if ap['diff_pct'] < 0 else 'ביוקר'}** "
                         f"ממי שהיה קונה כל יום אותו סכום.")
        lines += buy_lines(btc_b)
        near, deep = btc_b.from_ath > -10, btc_b.from_ath < -30
        lines.append(f"**{share(btc_b, near):.0f}%** מהכסף נקנה כש-BTC היה בטווח 10% מהשיא, "
                     f"ו-**{share(btc_b, deep):.0f}%** כשהיה 30%+ מתחת לשיא "
                     f"(בממוצע {I.wavg(btc_b, 'from_ath'):.0f}% מהשיא).")
        st.markdown("\n".join(f"- {x}" for x in lines))
    if not stk_b.empty:
        st.markdown("**מניות וקרנות**")
        st.markdown("\n".join(f"- {x}" for x in buy_lines(stk_b)))
        g = stk_b.groupby("asset")
        tbl = pd.DataFrame({"קניות": g.size(), "סכום": g.usd.sum() * fx_now,
                            "מיקום בטווח %": g.apply(lambda x: I.wavg(x, "range_pct"), include_groups=False),
                            "אחרי 90 יום %": g.apply(lambda x: I.wavg(x, "after_90"), include_groups=False)}).reset_index()
        st.dataframe(tbl.rename(columns={"asset": "נכס"}).style.format(
            {"סכום": "{:,.0f}", "מיקום בטווח %": "{:.0f}", "אחרי 90 יום %": "{:+.1f}"}, na_rep="–"),
            use_container_width=True, hide_index=True)

    if not sells_t.empty:
        st.subheader("🔴 מכירות")
        lines = []
        rp = I.wavg(sells_t, "range_pct")
        lines.append(f"בממוצע מכרת ב-**{rp:.0f}%** מהטווח של החודש שמסביב (100% = מכרת ביום הכי יקר) – "
                     + ("מכרת קרוב לשיאים 👍" if rp > 65 else "מכרת קרוב לתחתיות מקומיות" if rp < 35
                        else "בערך כמו מכירה בזמן אקראי") + ".")
        for col, n in (("after_30", 30), ("after_90", 90)):
            v = I.wavg(sells_t, col)
            if v is not None:
                lines.append(f"{n} יום אחרי המכירה המחיר היה {pct(v)} – "
                             + ("המכירות הקדימו ירידות 👍" if v < 0 else "המחיר עלה אחרי שמכרת") + ".")
        drop = sells_t[sells_t.before_30 < -15]
        if len(drop):
            v = I.wavg(drop, "after_30")
            lines.append(f"{len(drop)} מכירות היו אחרי ירידה של 15%+ בחודש שלפני"
                         + (f"; אחריהן המחיר {'המשיך לרדת' if v < 0 else 'התאושש'} ({pct(v)} תוך 30 יום)" if v is not None else "")
                         + ".")
        st.markdown("\n".join(f"- {x}" for x in lines))
        st.dataframe(pd.DataFrame({"תאריך": sells_t.date.dt.strftime("%d/%m/%Y"), "נכס": sells_t.asset,
                                   "סכום": sells_t.usd * fx_now, "מיקום בטווח %": sells_t.range_pct,
                                   "30 יום לפני %": sells_t.before_30, "30 יום אחרי %": sells_t.after_30,
                                   "90 יום אחרי %": sells_t.after_90})
                     .style.format({"סכום": "{:,.0f}", "מיקום בטווח %": "{:.0f}", "30 יום לפני %": "{:+.1f}",
                                    "30 יום אחרי %": "{:+.1f}", "90 יום אחרי %": "{:+.1f}"}, na_rep="–"),
                     use_container_width=True, hide_index=True)

    traded = sorted({assets[t.asset_id][0] for t in tx_f if t.type in ("BUY", "SELL") and float(t.price_per_unit) > 0})
    if traded:
        st.subheader("📍 איפה קנית ומכרת")
        pick = st.selectbox("נכס", traded, index=traded.index("BTC") if "BTC" in traded else 0)
        aid = next(a for a in assets if assets[a][0] == pick)
        tt = [t for t in tx_f if t.asset_id == aid and t.type in ("BUY", "SELL") and float(t.price_per_unit) > 0]
        first = min(pd.Timestamp(t.timestamp).normalize().tz_localize(None) for t in tt) - pd.Timedelta(days=30)
        ps = prices[aid][prices[aid].index >= first] * fx
        fm = go.Figure(go.Scatter(x=ps.index, y=ps, name=pick, line=dict(color="#9aa0a6", width=1.5),
                                  hovertemplate=sym + "%{y:,.2f}<extra></extra>"))
        for kind, color, symbol, label in (("BUY", "#2e9e5b", "triangle-up", "קנייה"),
                                           ("SELL", "#d9534f", "triangle-down", "מכירה")):
            k = [t for t in tt if t.type == kind]
            if not k:
                continue
            dd = [pd.Timestamp(t.timestamp).normalize().tz_localize(None) for t in k]
            usd_amt = [float(t.amount * t.price_per_unit) for t in k]
            mx = max(usd_amt)
            fm.add_trace(go.Scatter(x=dd, y=[float(ps.get(d, float("nan"))) for d in dd], mode="markers", name=label,
                                    marker=dict(color=color, symbol=symbol, line=dict(width=0),
                                                size=[8 + 16 * (u / mx) ** 0.5 for u in usd_amt]),
                                    customdata=usd_amt,
                                    hovertemplate=label + " $%{customdata:,.0f}<extra></extra>"))
        fm.update_layout(height=380, hovermode="closest", legend=dict(orientation="h", y=-0.2),
                         margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fm, use_container_width=True, config=CHART_CFG)
        st.caption("גודל הסימון לפי סכום העסקה.")

    rt = I.rotations(tx_f, assets, prices)
    if not rt.empty:
        st.subheader("🔁 החלפות בין נכסים")
        st.caption("מכרת נכס וקנית אחר באותו תיק תוך שבועיים: מה עשה כל אחד מאז ההחלפה.")
        st.dataframe(pd.DataFrame({"מתאריך": rt.date, "מכרת": rt.sold, "קנית": rt.bought,
                                   "סכום": rt.amount * fx_now, "מה שמכרת %": rt.sold_ret,
                                   "מה שקנית %": rt.bought_ret, "תוצאה": rt.edge * fx_now})
                     .style.format({"סכום": "{:,.0f}", "מה שמכרת %": "{:+.1f}", "מה שקנית %": "{:+.1f}",
                                    "תוצאה": "{:+,.0f}"}), use_container_width=True, hide_index=True)
        e = rt.edge.sum()
        st.markdown(f"בסך הכול ההחלפות {'הרוויחו' if e >= 0 else 'הפסידו'} **{usd(abs(e))}** "
                    f"לעומת השארת הכסף בנכס שנמכר.")

    with st.expander("סיכום כללי"):
        usd = lambda v: money(v * fx_now)                     # analyses run in USD; shown in the display currency
        pct = lambda v: f"{v:+.1f}%"

        # 1. what worked
        pa = I.per_asset_pnl(tx_f, assets, prices)
        if not pa.empty:
            st.subheader("מה עבד ומה לא")
            best, worst = pa.iloc[0], pa.iloc[-1]
            lines = [f"**הכי טוב: {best.asset}** – {usd(best.pnl)} ({pct(best.pct)}) על השקעה של {usd(best.paid)}."]
            if len(pa) > 1:
                lines.append(f"**הכי חלש: {worst.asset}** – {usd(worst.pnl)} ({pct(worst.pct)}) על השקעה של {usd(worst.paid)}.")
            losers = pa[pa.pnl < 0]
            if len(losers) and pa.pnl.sum() < 0:
                share = losers.pnl.min() / losers.pnl.sum() * 100
                lines.append(f"{worst.asset} לבד אחראי ל-{share:.0f}% מכל ההפסדים.")
            wins = (pa.pnl > 0).sum()
            lines.append(f"{wins} מתוך {len(pa)} הנכסים שקנית ברווח כרגע (כולל מה שנמכר).")
            st.markdown("\n".join(f"- {x}" for x in lines))
            fig = go.Figure(go.Bar(x=pa.pnl * fx_now, y=pa.asset, orientation="h",
                                   marker_color=["#2e9e5b" if v >= 0 else "#d9534f" for v in pa.pnl],
                                   hovertemplate="%{y}: " + sym + "%{x:,.0f}<extra></extra>"))
            fig.update_layout(height=60 + 34 * len(pa), margin=dict(l=10, r=10, t=10, b=10),
                              yaxis=dict(autorange="reversed"), xaxis_title=f"רווח/הפסד כולל ({sym})")
            st.plotly_chart(fig, use_container_width=True, config=CHART_CFG)

        # 2. all in bitcoin
        btc_px = daily(history("BTC-USD", start), idx)
        non_btc = [t for t in tx_f if t.type in ("BUY", "SELL") and assets[t.asset_id][0] != "BTC"]
        if non_btc and not btc_px.isna().any():
            st.subheader("אילו הכול היה בביטקוין")
            alt = I.all_in_btc(tx_f, btc_px)
            actual = float(sum(vals[a].iloc[-1] for a in ids) + cash_usd.iloc[-1])
            gap = actual - alt["value"]
            st.markdown(f"אותם סכומים, באותם תאריכים, אבל כל קנייה הייתה ב-BTC וכל מכירה מוציאה מ-BTC את אותו סכום: "
                        f"היום זה היה שווה **{usd(alt['value'])}** (כ-{alt['btc_qty']:.4f} BTC), מול **{usd(actual)}** בפועל – "
                        f"{'יותר ב-' if gap >= 0 else 'פחות ב-'}**{usd(abs(gap))}**.")
            if gap < 0:
                st.caption("כלומר, החשיפה לביטקוין דרך מניות וקרנות עלתה לך יותר מאשר להחזיק ביטקוין עצמו.")

        # 3. sells
        sv = I.sells_vs_holding(tx_f, assets, prices)
        if not sv.empty:
            st.subheader("המכירות שלך מול להמשיך להחזיק")
            tot = sv.saved.sum()
            st.markdown(f"קיבלת {usd(sv.got.sum())} ממכירות. אותן יחידות היו שוות היום {usd(sv.if_held.sum())} – "
                        f"המכירות {'חסכו לך' if tot >= 0 else 'עלו לך'} **{usd(abs(tot))}**.")
            b = sv.loc[sv.saved.idxmax()]
            w = sv.loc[sv.saved.idxmin()]
            st.markdown(f"- המכירה הכי טובה: {b.asset} ב-{b.date:%d/%m/%Y} ({usd(b.saved)} מול החזקה)\n"
                        f"- המכירה הכי פחות טובה: {w.asset} ב-{w.date:%d/%m/%Y} ({usd(w.saved)} מול החזקה)")

        # 5. fees
        fs = I.fee_summary(tx_f)
        if fs["paid"]:
            st.subheader("עמלות")
            total_fees = fs["trade"] + fs["account"]
            st.markdown(f"שילמת {usd(total_fees)} עמלות – {total_fees / fs['paid'] * 100:.2f}% מכל הקניות: "
                        f"{usd(fs['trade'])} על {fs['trades']} פעולות מסחר"
                        + (f" ו-{usd(fs['account'])} דמי ניהול וטיפול." if fs["account"] else "."))
            vba = {}
            for a in chosen:
                at = acct_txs[a]
                vba[a] = sum(float(signed_qty(at, aid, idx).iloc[-1]) * float(prices[aid].iloc[-1]) for aid in assets) \
                    + float(cash_model(at, idx)[1].iloc[-1])
            dr = I.account_fee_drag(tx_f, vba)
            for r in dr.itertuples():
                st.markdown(f"- {r.account}: {usd(r.fees_12m)} דמי ניהול וטיפול ב-12 החודשים האחרונים = "
                            f"**{r.pct:.2f}% בשנה** מהשווי הנוכחי"
                            + (" – משמעותי ביחס לגודל החשבון." if r.pct >= 0.5 else "."))

        # 6. concentration
        cv = {assets[a][0]: float(vals[a].iloc[-1]) for a in ids}
        cc = I.concentration(cv)
        if cc:
            st.subheader("ריכוזיות")
            st.markdown(f"- הנכס הגדול ביותר: **{cc['top']}** – {cc['top_pct']:.0f}% מהתיק.\n"
                        f"- **{cc['btc_linked_pct']:.0f}%** מהתיק תלוי ישירות במחיר הביטקוין "
                        f"(BTC, IBIT, MSTR, Metaplanet וחברות דומות), גם אם הוא מחולק בין נכסים שונים.")

with st.expander("📒 ספר עסקאות"):
    st.dataframe(pd.DataFrame([{**t.__dict__, "asset": assets[t.asset_id][0]} for t in tx_f]),
                 use_container_width=True, hide_index=True)
