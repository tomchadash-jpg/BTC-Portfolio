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

import importlib

import accounts as A
import engine as E
import insights as I

# Streamlit Cloud re-runs app.py after each update but can keep old copies of our own
# modules in memory; reload them so new functions are always there.
for _m in (A, E, I):
    importlib.reload(_m)

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

st.markdown("<h1 style='text-align:center'>📈 Portfolio Tracker</h1>", unsafe_allow_html=True)

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
    BENCH = {"BTC": "BTC-USD", "SPY": "SPY", "QQQ": "QQQ", "TQQQ": "TQQQ"}   # label -> Yahoo ticker
    bench = st.multiselect("מדדי ייחוס", list(BENCH), default=list(BENCH))
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
if not hasattr(st, "_orig_plotly_chart"):      # keep the original once; this script re-runs on every tap
    st._orig_plotly_chart = st.plotly_chart
_plotly_chart = st._orig_plotly_chart


def _centered_chart(fig, *args, **kwargs):
    """All chart titles centered."""
    fig.update_layout(title_x=0.5, title_xanchor="center")
    return _plotly_chart(fig, *args, **kwargs)


st.plotly_chart = _centered_chart

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


(st.html if hasattr(st, "html") else lambda c: st.markdown(c, unsafe_allow_html=True))("""<style>
[class*="st-key-kpi"] [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: .5rem; }
[class*="st-key-kpi"] [data-testid="stColumn"], [class*="st-key-kpi"] [data-testid="column"] {
    min-width: 0 !important; width: calc(33.3% - .35rem) !important; flex: 1 1 0 !important; }
[class*="st-key-kpi"] [data-testid="stMetric"] { background: rgba(128,128,128,.08); border-radius: .6rem; padding: .5rem .6rem; }
[class*="st-key-kpi"] [data-testid="stMetricLabel"] p { font-size: .75rem; white-space: normal; line-height: 1.2; }
[class*="st-key-kpi"] [data-testid="stMetricValue"] { font-size: 1.15rem; }
[class*="st-key-kpi"] [data-testid="stMetricDelta"] { font-size: .7rem; }
</style>""")

cards = [("שווי נקי", money(nw_now), None),
         ("סה״כ הושקע", money(invested_now), None),
         ("רווח/הפסד (בניכוי עמלות)", money(pl), f"{pl / invested_now * 100 if invested_now else 0.0:+.1f}%"),
         ("רווח ממומש (ממכירות)", money(float(realized_sales) * fx_now), None),
         ("TWR – תשואה כוללת", f"{(twr.iloc[-1] - 1) * 100:+.1f}%", f"{E.cagr(twr) * 100:+.1f}% לשנה"),
         ("Max Drawdown", f"{E.max_drawdown(twr) * 100:.1f}%", None)]
if A.COLD in chosen:                           # bitcoin network fees exist only in the cold wallet
    cards += [("עמלות רשת (on-chain)", money(float(onchain_loss) * fx_now), None),
              ("עמלות משיכה מהבורסות", money(float(withdrawal_loss) * fx_now), None)]
btc_held = [a for a in ids if assets[a][0] == "BTC"]
if btc_held:                                   # bitcoin in the cold wallet: amount first, it's what matters most
    q = sum(float(summ.by_asset[a]["qty"]) for a in btc_held)
    px_btc = float(summ.by_asset[btc_held[0]]["price"]) * fx_now
    on_ex = A.btc_on_exchanges(tx_f, set(btc_held))
    ex_q = sum(on_ex.values())
    btc_fmt = lambda x: f"{x:.8f}".rstrip("0").rstrip(".") + " BTC"
    if ex_q > 0:
        cards[0:0] = [("₿ בארנק הקר", btc_fmt(q - ex_q), f"{(q - ex_q) * 1e8:,.0f} sats · {money((q - ex_q) * px_btc)}"),
                      ("₿ בבורסה", btc_fmt(ex_q), " · ".join(f"{p} {v * 1e8:,.0f} sats" for p, v in on_ex.items() if v > 0))]
    else:
        cards.insert(0, ("₿ בארנק הקר", btc_fmt(q), f"{q * 1e8:,.0f} sats · {money(q * px_btc)}"))
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
    # --- value by asset: stacked layers, biggest at the bottom, small ones grouped as "אחר" ---
    stack = pd.DataFrame({assets[a][0]: vals[a] * fx for a in ids if (vals[a] > 0.5).any()})
    if (cash_usd > 0.5).any():
        stack["מזומן"] = cash_usd * fx
    sl_st = E.slice_range(stack, rng)
    sl_st = sl_st.loc[:, (sl_st > 0.5).any()]                 # assets not held in this range
    if not sl_st.empty:
        peak_total = float(sl_st.sum(axis=1).max())
        small = [c for c in sl_st if c != "מזומן" and sl_st[c].max() < 0.03 * peak_total]
        if len(small) > 1:
            sl_st["אחר"] = sl_st[small].sum(axis=1)
            sl_st = sl_st.drop(columns=small)
        order = sorted(sl_st.columns, key=lambda c: -float(sl_st[c].iloc[-1]) - float(sl_st[c].max()) / 1e6)
        as_pct = st.radio("הצג", ["שווי", "% מהתיק"], horizontal=True, key="stack_mode", label_visibility="collapsed")
        shown = sl_st.div(sl_st.sum(axis=1).replace(0, float("nan")), axis=0) * 100 if as_pct == "% מהתיק" else sl_st
        palette = {"BTC": "#F7931A", "IBIT": "#4C8BF5", "MSTR": "#E0457B", "MSTX": "#B04AC9", "MTPLF": "#2EB8A6",
                   "MPJPY": "#7FD1C5", "ASST": "#F2C94C", "TSWCF": "#9B7B5B", "TQQQ": "#8E9AAF",
                   "מזומן": "#6FCF97", "אחר": "#9AA0A6"}
        extra = iter(["#56CCF2", "#BB6BD9", "#EB5757", "#27AE60", "#F2994A"])
        fs = go.Figure()
        for c in order:
            fs.add_trace(go.Scatter(x=shown.index, y=shown[c], name=c, stackgroup="one", mode="lines",
                                    line=dict(width=0.6, color=palette.get(c) or next(extra, "#cccccc")),
                                    hovertemplate=("%{y:.0f}%" if as_pct == "% מהתיק" else sym + "%{y:,.0f}")))
        fs.update_layout(title="ממה מורכב התיק לאורך זמן", height=380, hovermode="x unified",
                         legend=dict(orientation="h", y=-0.15, traceorder="normal"), margin=dict(l=10, r=10),
                         yaxis=dict(title=None, ticksuffix="%" if as_pct == "% מהתיק" else "",
                                    tickprefix="" if as_pct == "% מהתיק" else sym),
                         xaxis=dict(title=None))
        st.plotly_chart(fs, use_container_width=True, config=CHART_CFG)
        st.caption("כל צבע הוא נכס, ועובי השכבה הוא כמה הוא שווה באותו יום. הקצה העליון הוא שווי התיק כולו. "
                   "ב'% מהתיק' רואים איך החלוקה בין הנכסים השתנתה. נכסים קטנים מ-3% מאוחדים ל'אחר'.")
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
        st.caption(f"DCA יומי: אותו סכום שהשקעת בביטקוין (\\${dca['total']:,.0f}), מחולק שווה לכ-\\${dca['per_day']:,.0f} ליום "
                   f"מהרכישה הראשונה של BTC ועד היום, לפי מחיר הסגירה היומי. ההשוואה היא רק לביטקוין, "
                   f"בלי הנכסים האחרים. ההפרש מראה אם הקניות שלך הקדימו (+) או פיגרו (-) אחרי קנייה יומית קבועה.")
    sl = E.slice_range(twr, rng)
    fig = go.Figure(go.Scatter(x=sl.index, y=E.rebased(sl), name="Portfolio (TWR)", line=dict(width=3)))
    for b in bench:
        h = history(BENCH[b], start)
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
    # "$" must be escaped in Streamlit markdown, otherwise "$...$" turns into a math formula
    usd = lambda v: money(v * fx_now).replace("$", "\\$")
    pct = lambda v: f"{v:+.1f}%"
    st.caption("ניתוח התזמון של הקניות והמכירות, לפי התיקים שבחרת למעלה. תיאור של מה שקרה, לא המלצה.")

    buys_t = I.trade_timing(tx_f, assets, prices, "BUY")
    sells_t = I.trade_timing(tx_f, assets, prices, "SELL")

    def share(df, mask):
        return df[mask].usd.sum() / df.usd.sum() * 100 if df.usd.sum() else 0.0

    def cards(items):
        """Insight cards: (icon, big number, title, sentence, tone) with tone good / bad / None.
        Styles are inline so the cards look right even if the page-wide CSS doesn't load."""
        edge = {"good": "#2e9e5b", "bad": "#d9534f"}
        num = {"good": "#3fbf73", "bad": "#ef6b66"}
        html = []
        for icon, big, title, text, tone in items:
            html.append(
                f'<div dir="rtl" style="background:rgba(128,128,128,.10);border-radius:12px;padding:10px 12px;'
                f'border-right:4px solid {edge.get(tone, "rgba(128,128,128,.35)")};text-align:right">'
                f'<div style="display:flex;align-items:center;justify-content:space-between;gap:6px">'
                f'<span style="font-size:1.2rem">{icon}</span>'
                f'<span dir="ltr" style="font-size:1.45rem;font-weight:700;color:{num.get(tone, "inherit")};'
                f'font-variant-numeric:tabular-nums">{big}</span></div>'
                f'<div style="font-weight:600;font-size:.92rem;margin-top:4px">{title}</div>'
                f'<div style="font-size:.8rem;opacity:.75;line-height:1.45;margin-top:3px">{text}</div></div>')
        st.markdown('<div dir="rtl" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));'
                    'gap:10px;margin:6px 0 16px">' + "".join(html) + "</div>", unsafe_allow_html=True)

    cash_fmt = lambda v: money(v * fx_now).replace("$", "&#36;")      # same, inside HTML cards

    def buy_cards(df):
        out = []
        rp = I.wavg(df, "range_pct")
        out.append(("📍", f"{rp:.0f}%", "איפה בטווח החודשי קנית",
                    "0% = היום הכי זול בחודש שמסביב, 100% = הכי יקר. "
                    + ("קנית קרוב לתחתיות." if rp < 35 else "נטייה לקנות קרוב לשיאים." if rp > 65
                       else "בערך כמו קנייה בזמן אקראי."),
                    "good" if rp < 35 else "bad" if rp > 65 else None))
        up, down = df.before_30 > 10, df.before_30 < -10
        u90, d90 = I.wavg(df[up], "after_90"), I.wavg(df[down], "after_90")
        txt = f"מהכסף נקנה אחרי ירידה של 10%+ בחודש שלפני, ו-{share(df, up):.0f}% אחרי עלייה של 10%+."
        tone = None
        if u90 is not None and d90 is not None:
            txt += f" אחרי 90 יום: קניות בירידה {pct(d90)}, קניות בעלייה {pct(u90)}."
            tone = "good" if d90 > u90 else "bad"
        out.append(("📉", f"{share(df, down):.0f}%", "קנית בירידות", txt, tone))
        a90 = I.wavg(df, "after_90")
        if a90 is not None:
            out.append(("⏱️", pct(a90), "90 יום אחרי קנייה", "השינוי הממוצע במחיר ב-3 החודשים שאחרי כל קנייה.",
                        "good" if a90 > 0 else "bad"))
        d = df.dropna(subset=["after_90"])
        if len(d) >= 3:
            bst, wst = d.loc[d.after_90.idxmax()], d.loc[d.after_90.idxmin()]
            out.append(("🏆", pct(bst.after_90), "הקנייה הכי מוצלחת",
                        f"{bst.asset} · {bst.date:%d/%m/%Y} · {cash_fmt(bst.usd)}", "good"))
            out.append(("🫠", pct(wst.after_90), "הקנייה הכי פחות מוצלחת",
                        f"{wst.asset} · {wst.date:%d/%m/%Y} · {cash_fmt(wst.usd)}", "bad"))
        return out

    st.subheader("🟢 קניות")
    btc_b = buys_t[buys_t.asset == "BTC"] if not buys_t.empty else buys_t
    stk_b = buys_t[buys_t.asset != "BTC"] if not buys_t.empty else buys_t
    if not btc_b.empty:
        st.markdown("##### ביטקוין")
        items = []
        bid = next(a for a in assets if assets[a][0] == "BTC")
        ap = I.avg_price_vs_period(tx_f, prices, bid)
        if ap:
            items.append(("🎯", pct(ap["diff_pct"]), "מחיר ממוצע מול קנייה יומית",
                          f"שילמת בממוצע {cash_fmt(ap['mine'])} מול {cash_fmt(ap['period_avg'])} – הממוצע היומי "
                          f"בתקופה שקנית ({ap['first']:%m/%Y}–{ap['last']:%m/%Y}).",
                          "good" if ap["diff_pct"] < 0 else "bad"))
        items += buy_cards(btc_b)
        near, deep = btc_b.from_ath > -10, btc_b.from_ath < -30
        items.append(("🏔️", f"{I.wavg(btc_b, 'from_ath'):.0f}%", "מרחק ממוצע מהשיא בזמן קנייה",
                      f"{share(btc_b, near):.0f}% מהכסף נקנה בטווח 10% מהשיא, "
                      f"{share(btc_b, deep):.0f}% כשהיה 30%+ מתחתיו.", None))
        cards(items)
    if not stk_b.empty:
        st.markdown("##### מניות וקרנות")
        cards(buy_cards(stk_b))
        g = stk_b.groupby("asset")
        tbl = pd.DataFrame({"קניות": g.size(), "סכום": g.usd.sum() * fx_now,
                            "מיקום בטווח %": g.apply(lambda x: I.wavg(x, "range_pct"), include_groups=False),
                            "אחרי 90 יום %": g.apply(lambda x: I.wavg(x, "after_90"), include_groups=False)}).reset_index()
        st.dataframe(tbl.rename(columns={"asset": "נכס"}).style.format(
            {"סכום": "{:,.0f}", "מיקום בטווח %": "{:.0f}", "אחרי 90 יום %": "{:+.1f}"}, na_rep="–"),
            use_container_width=True, hide_index=True)

    if not sells_t.empty:
        st.subheader("🔴 מכירות")
        items = []
        rp = I.wavg(sells_t, "range_pct")
        items.append(("📍", f"{rp:.0f}%", "איפה בטווח החודשי מכרת",
                      "100% = מכרת ביום הכי יקר בחודש שמסביב. "
                      + ("מכרת קרוב לשיאים." if rp > 65 else "מכרת קרוב לתחתיות." if rp < 35
                         else "בערך כמו מכירה בזמן אקראי."),
                      "good" if rp > 65 else "bad" if rp < 35 else None))
        for col, n in (("after_30", 30), ("after_90", 90)):
            v = I.wavg(sells_t, col)
            if v is not None:
                items.append(("🔭", pct(v), f"{n} יום אחרי מכירה",
                              "המחיר המשיך לרדת – המכירות הקדימו ירידות." if v < 0 else "המחיר עלה אחרי שמכרת.",
                              "good" if v < 0 else "bad"))
        drop = sells_t[sells_t.before_30 < -15]
        if len(drop):
            v = I.wavg(drop, "after_30")
            items.append(("😬", str(len(drop)), "מכירות אחרי ירידה חדה",
                          "מכירות אחרי ירידה של 15%+ בחודש שלפני."
                          + (f" אחריהן המחיר {'המשיך לרדת' if v < 0 else 'התאושש'}: {pct(v)} תוך 30 יום." if v is not None else ""),
                          ("good" if v < 0 else "bad") if v is not None else None))
        cards(items)
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

    # ---------- lessons: patterns in your own data, phrased as rules to consider ----------
    lessons = []          # (icon, key number, rule, evidence, tone)
    for name, df in (("ביטקוין", btc_b), ("מניות", stk_b)):
        if df.empty:
            continue
        up, down = df.before_30 > 10, df.before_30 < -10
        u90, d90 = I.wavg(df[up], "after_90"), I.wavg(df[down], "after_90")
        if u90 is not None and d90 is not None and abs(d90 - u90) >= 5:
            if d90 > u90:
                lessons.append(("📉", pct(d90 - u90), f"{name}: ירידות היו הזדמנות",
                                f"קניות אחרי ירידה של 10%+ הניבו {pct(d90)} ב-90 יום, מול {pct(u90)} אחרי עליות. "
                                f"כלל לשקול: לשמור חלק מהתקציב לימים של ירידה חדה.", "good"))
            else:
                lessons.append(("🚀", pct(u90 - d90), f"{name}: המומנטום עבד לך",
                                f"קניות אחרי עלייה הניבו {pct(u90)} ב-90 יום, מול {pct(d90)} אחרי ירידות – "
                                f"אצלך 'לתפוס סכין נופלת' עבד פחות טוב.", None))
        rp = I.wavg(df, "range_pct")
        if rp is not None and rp > 60:
            lessons.append(("🧊", f"{rp:.0f}%", f"{name}: לא למהר לקנות",
                            "רוב הכסף נקנה קרוב לשיאים המקומיים של החודש. פיצול כל קנייה ל-3–4 חלקים "
                            "על פני כמה שבועות היה מוריד את המחיר הממוצע.", "bad"))
    if not btc_b.empty and ap:
        if ap["diff_pct"] > 1:
            lessons.append(("🤖", pct(ap["diff_pct"]), "ביטקוין: קנייה קבועה הייתה זולה יותר",
                            "קנייה יומית או שבועית באותו סכום הייתה נותנת מחיר ממוצע נמוך יותר מהתזמון הידני. "
                            "כלל לשקול: להפוך חלק מהקניות להוראה קבועה.", "bad"))
        elif ap["diff_pct"] < -1:
            lessons.append(("🎯", pct(ap["diff_pct"]), "ביטקוין: התזמון שלך עובד",
                            f"המחיר הממוצע שלך נמוך ב-{abs(ap['diff_pct']):.1f}% מקנייה יומית קבועה. "
                            "מה שעשית עד עכשיו – לקנות יותר כשהמחיר יורד – הוכיח את עצמו.", "good"))
    if not sells_t.empty:
        a90 = I.wavg(sells_t, "after_90")
        if a90 is not None and a90 > 5:
            lessons.append(("🔭", pct(a90), "מניות: מכרת מוקדם מדי",
                            "90 יום אחרי המכירות המחיר היה גבוה יותר. כלל לשקול: לפני מכירה, להגדיר מראש "
                            "מחיר או סיבה למכירה, ולא למכור מתוך תגובה לתנודה.", "bad"))
        drop = sells_t[sells_t.before_30 < -15]
        v = I.wavg(drop, "after_30") if len(drop) else None
        if v is not None and v > 0:
            lessons.append(("😬", pct(v), "מניות: לא למכור בפאניקה",
                            f"{len(drop)} מכירות היו אחרי ירידה חדה, ובחודש שאחריהן המחיר התאושש. "
                            "כלל לשקול: לחכות כמה ימים אחרי ירידה חדה לפני החלטה על מכירה.", "bad"))
    if not rt.empty:
        wins, n = int((rt.edge > 0).sum()), len(rt)
        e = rt.edge.sum()
        if e < 0:
            lessons.append(("🔁", usd(e).replace("\\$", "&#36;"), "מניות: פחות החלפות",
                            f"ההחלפות בין נכסים עבדו ב-{wins} מתוך {n} פעמים, ובסך הכול עלו לך כסף. "
                            "כל החלפה היא הימור שהנכס החדש יעשה יותר טוב – כלל לשקול: להחליף רק עם סיבה ברורה.",
                            "bad"))
    cv_now = {assets[a][0]: float(vals[a].iloc[-1]) for a in ids}
    cc = I.concentration(cv_now)
    if cc and cc["btc_linked_pct"] > 80 and len([v for v in cv_now.values() if v > 0]) > 1:
        lessons.append(("🧲", f"{cc['btc_linked_pct']:.0f}%", "הפיזור כאן הוא בעיקר שמות",
                        "כמעט כל התיק נע עם מחיר הביטקוין, גם המניות והקרנות. אם המטרה היא פיזור סיכון, "
                        "הוא יגיע רק מנכסים שלא קשורים לביטקוין.", None))
    if lessons:
        st.subheader("🧭 מסקנות להמשך")
        st.caption("דפוסים שעולים מהעסקאות שלך, ניסוחים של כללים לשקול – לא המלצת השקעה.")
        cards(lessons)

    with st.expander("סיכום כללי"):
        usd = lambda v: money(v * fx_now).replace("$", "\\$")                   # analyses run in USD; shown in the display currency
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
