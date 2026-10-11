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
import menu
import powerlaw as PL

# Streamlit Cloud re-runs app.py after each update but can keep old copies of our own
# modules in memory; reload them so new functions are always there.
for _m in (A, E, I, PL):
    importlib.reload(_m)

st.set_page_config(page_title="Portfolio Tracker", layout="wide", page_icon=":material/show_chart:")

import ui
ui.rtl()
import gate
gate.require_login()  # nothing below runs without the password

clean = lambda s: "".join(ch for ch in (s or "") if ch.isascii() and not ch.isspace())
# a Material Symbols icon (same outline style as the menu) inside raw HTML
MI = "<span style=\"font-family:'Material Symbols Rounded';font-weight:normal;font-style:normal;line-height:1;vertical-align:-0.15em\">{}</span>"


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
all_assets, all_txs = dict(assets), list(txs)       # before any section filter (goals use them)
if not txs:
    st.info("אין עסקאות עדיין. ייבא קובץ בעמוד ״ייבוא ומחיקה״ בתפריט ☰")
    st.stop()

menu_pop = menu.top(overlap=True)          # ☰ at the top right; the title sits on the left of the same row
st.markdown("<h1 dir='ltr' style='text-align:left'>" + MI.format("show_chart") + " Portfolio Tracker</h1>",
            unsafe_allow_html=True)

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

CLS_HE = {"equity": "מניות וקרנות", "crypto": "קריפטו", "cash": "מזומן", "bond": "אג״ח", "digital_asset": "נכס דיגיטלי"}
he = lambda c: CLS_HE.get(c, c)
SECTIONS = ["שווי היסטורי", "השוואה למדדים", "יעדים", "נכסים", "תובנות", "Power Law"]
section = SECTIONS[st.session_state.get("section_idx", 0)]       # which page of the ☰ menu this is (app.py)
with menu_pop:
    menu.group("הגדרות")
    classes = sorted({a[2] for a in assets.values()})
    sel = st.multiselect("סינון לפי סוג נכס", classes, default=classes, format_func=he)
    BENCH = {"BTC": "BTC-USD", "SPY": "SPY", "QQQ": "QQQ", "TQQQ": "TQQQ"}   # label -> Yahoo ticker
    bench = st.multiselect("מדדי ייחוס", list(BENCH), default=list(BENCH))
    dca_on = st.checkbox("השוואת BTC ל-DCA יומי", value=True)
    # a caption instead of a "?" tooltip: tooltips open on hover only, so they don't respond to a tap on iPhone
    st.caption("הכסף שהשקעת בביטקוין בלבד, מחולק שווה על כל יום מהרכישה הראשונה של BTC ועד היום")
    btc_goal = st.number_input("יעד ביטקוין (BTC)", min_value=0.01, value=2.1, step=0.1, format="%.2f", key="btc_goal")
    if is_demo:
        st.info("מצב דמו – הגדר DATABASE_URL ו-USER_ID לנתונים אמיתיים")

c1, c2, c3 = st.columns([2, 3, 2])
ccy = c1.radio("מטבע תצוגה", ["USD", "ILS"], horizontal=True)
rng = c2.select_slider("טווח זמן", ["1M", "6M", "1Y", "YTD", "ALL"], value="ALL")
if c3.button("רענן מחירים", icon=":material/refresh:"):
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
realized_no_net = sum((p.realized_pnl for p in E.build_positions(real_tx).values()), D(0))
net_fee_loss = summ.realized - realized_no_net                  # bitcoin network + withdrawal fees only
# "realized from sales": also without the account fees (IBI management / handling)
realized_sales = sum((p.realized_pnl for p in E.build_positions([t for t in real_tx if t.type != "FEE"]).values()), D(0))
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
        st.caption("אין מספיק נתונים לחישוב DCA יומי")
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
         ("ירידה מקסימלית מהשיא", f"{E.max_drawdown(twr) * 100:.1f}%", None)]
if A.COLD in chosen:                           # bitcoin network fees exist only in the cold wallet
    cards += [("עמלות רשת (on-chain)", money(float(onchain_loss) * fx_now), None),
              ("עמלות משיכה מהבורסות", money(float(withdrawal_loss) * fx_now), None)]
btc_held = [a for a in ids if assets[a][0] == "BTC"]
if btc_held:                                   # bitcoin in the cold wallet: amount first, it's what matters most
    q = sum(float(summ.by_asset[a]["qty"]) for a in btc_held)
    px_btc = float(summ.by_asset[btc_held[0]]["price"]) * fx_now
    on_ex = A.btc_on_exchanges(tx_f, set(btc_held))
    ex_q = sum(on_ex.values())
    # short enough to fit a card on a phone: 4 decimals (6 for small amounts), value underneath
    btc_fmt = lambda x: f"₿{x:.4f}" if x >= 0.01 else f"₿{x:.6f}"
    sats = lambda x: f"{x * 1e8 / 1e6:,.1f}M sats" if x * 1e8 >= 1e6 else f"{x * 1e8:,.0f} sats"
    if ex_q > 0:
        cards[0:0] = [("בארנק הקר", btc_fmt(q - ex_q), money((q - ex_q) * px_btc)),
                      ("בבורסה", btc_fmt(ex_q), f"{sats(ex_q)} · {money(ex_q * px_btc)}")]
    else:
        cards.insert(0, ("בארנק הקר", btc_fmt(q), money(q * px_btc)))
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
with st.expander("מה המדדים אומרים?", icon=":material/info:"):
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
            f'<span style="font-family:\'Material Symbols Rounded\';font-size:1.6rem;line-height:1;opacity:.85">{icon}</span>'
            f'<span dir="ltr" style="font-size:1.45rem;font-weight:700;color:{num.get(tone, "inherit")};'
            f'font-variant-numeric:tabular-nums">{big}</span></div>'
            f'<div style="font-weight:600;font-size:.92rem;margin-top:4px">{title}</div>'
            f'<div style="font-size:.8rem;opacity:.75;line-height:1.45;margin-top:3px">{text}</div></div>')
    st.markdown('<div dir="rtl" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));'
                'gap:10px;margin:6px 0 16px">' + "".join(html) + "</div>", unsafe_allow_html=True)


if section == SECTIONS[0]:
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
    # --- month by month: how much the market moved the portfolio vs. new money put in ---
    val_c = nw_usd * fx                                   # value in display currency, daily
    flow_c = new_money * fx                               # new money in, at each day's rate
    m_end = val_c.resample("ME").last()
    m_in = flow_c.resample("ME").sum()
    m_gain = m_end - m_end.shift(1).fillna(0.0) - m_in    # change in value that didn't come from new money
    monthly = pd.DataFrame({"gain": m_gain, "new": m_in})
    monthly = E.slice_range(monthly, rng) if rng != "ALL" else monthly
    if len(monthly):
        labels = [d.strftime("%m/%y") for d in monthly.index]
        fmn = go.Figure()
        fmn.add_trace(go.Bar(x=labels, y=monthly.gain, name="שינוי מהשוק",
                             marker_color=["#2e9e5b" if v >= 0 else "#d9534f" for v in monthly.gain],
                             hovertemplate=sym + "%{y:,.0f}<extra>שינוי מהשוק</extra>"))
        fmn.add_trace(go.Scatter(x=labels, y=monthly.new, name="כסף חדש שהוכנס", mode="lines+markers",
                                 line=dict(color="#9aa0a6", width=1.5, dash="dot"), marker=dict(size=5),
                                 hovertemplate=sym + "%{y:,.0f}<extra>כסף חדש</extra>"))
        fmn.update_layout(title=f"חודש אחרי חודש ({ccy})", height=360, hovermode="x unified", bargap=0.25,
                          legend=dict(orientation="h", y=-0.2), margin=dict(l=10, r=10),
                          yaxis=dict(tickprefix=sym, title=None), xaxis=dict(title=None, type="category"))
        st.plotly_chart(fmn, use_container_width=True, config=CHART_CFG)
        good, bad = int((monthly.gain > 0).sum()), int((monthly.gain < 0).sum())
        best, worst = monthly.gain.idxmax(), monthly.gain.idxmin()
        st.caption(f"עמודה = כמה התיק עלה או ירד באותו חודש בגלל תנודות המחיר בלבד, בלי הכסף שהוספת. "
                   f"{good} חודשים ירוקים, {bad} אדומים. "
                   f"הכי טוב: {best:%m/%Y} ({money(float(monthly.gain[best]))}) · "
                   f"הכי גרוע: {worst:%m/%Y} ({money(float(monthly.gain[worst]))}).".replace("$", "\\$"))
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

if section == SECTIONS[1]:
    if dca:
        dca_now = float(dca["usd"].iloc[-1]) * fx_now
        mine_now = float(dca["mine"].iloc[-1]) * fx_now
        diff = mine_now - dca_now
        my_qty = sum(float(summ.by_asset[a]["qty"]) for a in btc_ids)
        grid([("BTC שלי – שווי", f"{sym}{mine_now:,.0f}", f"{'+' if diff >= 0 else '-'}{sym}{abs(diff):,.0f} מול DCA"),
              ("DCA יומי – שווי", f"{sym}{dca_now:,.0f}", None),
              ("BTC שלי מול DCA", f"₿{my_qty:.3f}", f"DCA: ₿{dca['qty']:.3f}")], "kpi_dca")
        tot_c = float(dca["inv"].iloc[-1])                 # in the display currency, at each purchase day's rate
        per_c = tot_c / max(int((idx >= dca["start"]).sum()), 1)
        mny = lambda v: money(v).replace("$", "\\$")
        st.caption(f"DCA יומי: אותו סכום שהשקעת בביטקוין ({mny(tot_c)}), מחולק שווה לכ-{mny(per_c)} ליום "
                   f"מהרכישה הראשונה של BTC ועד היום, לפי מחיר הסגירה היומי. ההשוואה היא רק לביטקוין, "
                   f"בלי הנכסים האחרים. ההפרש מראה אם הקניות שלך הקדימו (+) או פיגרו (-) אחרי קנייה יומית קבועה.")
    st.caption("הגרף מתחת מציג תשואה באחוזים ולא סכומים: כל הקווים מתחילים מ-100, "
               "כך ש-150 = עלייה של 50% מתחילת הטווח. לכן הוא זהה בדולרים ובשקלים.")
    sl = E.slice_range(twr, rng)
    fig = go.Figure(go.Scatter(x=sl.index, y=E.rebased(sl), name="התיק שלי (TWR)", line=dict(width=3)))
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
    fig.update_layout(height=450, yaxis_title="100 = תחילת הטווח", hovermode="x unified",
                      legend=dict(orientation="h", y=-0.18), margin=dict(l=10, r=10))
    st.plotly_chart(fig, use_container_width=True, config=CHART_CFG)

if section == SECTIONS[2]:
    today = pd.Timestamp(datetime.now().date())

    def price_on(aid, ix):
        """Daily USD closes for an asset on index ix (live price for today when available)."""
        sym_, _, _, api = all_assets[aid]
        p = daily(history(api, ix[0].strftime("%Y-%m-%d")), ix)
        if CG.get(sym_) in live:
            p.iloc[-1] = live[CG[sym_]]
        return p.fillna(0.0)

    # ---------- goal 1: 2.1 BTC ----------
    st.subheader(f"₿ יעד: {btc_goal:g} ביטקוין")
    btc_aid = [a for a in all_assets if all_assets[a][0] == "BTC"]
    btc_txs = [t for t in all_txs if t.asset_id in btc_aid]
    if btc_txs:
        have = sum(float(t.amount) * (-1 if t.type == "SELL" else 1)
                   for t in btc_txs if t.type in ("BUY", "SELL", "STAKING_REWARD"))
        left = max(btc_goal - have, 0.0)
        btc_px_now = float(live.get(CG["BTC"]) or price_on(btc_aid[0], pd.date_range(today - pd.Timedelta(days=7), today)).iloc[-1])
        since = today - pd.Timedelta(days=90)
        recent = sum(float(t.amount) for t in btc_txs if t.type == "BUY"
                     and pd.Timestamp(t.timestamp).tz_localize(None).normalize() >= since)
        pace = recent / 90                                  # BTC per day over the last 3 months
        pct_done = min(have / btc_goal * 100, 100)
        st.progress(pct_done / 100, text=f"{have:.4f} מתוך {btc_goal:g} BTC · {pct_done:.1f}%")
        items = [("savings", f"₿{left:.4f}", "נשאר לצבור", f"{left * 1e8 / 1e6:,.1f}M sats", None),
                 ("payments", money(left * btc_px_now * fx_now).replace("$", "&#36;"), "עלות ההשלמה במחיר של היום",
                  f"לפי {money(btc_px_now * fx_now).replace('$', '&#36;')} לביטקוין", None)]
        if left <= 0:
            items = [("flag", "✓", "היעד הושג", f"יש לך {have:.4f} BTC", "good")]
        elif pace > 0:
            eta = today + pd.Timedelta(days=left / pace)
            items.append(("hourglass_top", f"{eta:%m/%Y}", "בקצב של 3 החודשים האחרונים",
                          f"{pace * 30:.4f} BTC בחודש ({pace * 1e8 * 30 / 1e6:,.2f}M sats). "
                          f"עוד כ-{left / pace / 365:.1f} שנים.", None))
        else:
            items.append(("hourglass_top", "–", "קצב צבירה", "לא היו קניות ב-3 החודשים האחרונים.", None))
        cards(items)
        st.caption("כל הביטקוין שבבעלותך – בארנק הקר ובבורסות. ה-IBIT בגמל לא נספר, הוא קרן ולא ביטקוין. "
                   "היעד ניתן לשינוי בתפריט ☰.")

    # ---------- goal 2: קרן השתלמות beats bitcoin ----------
    st.subheader(":material/trending_up: יעד: קרן ההשתלמות מנצחת את הביטקוין")
    k_txs = [t for t in all_txs if A.account_of(t.notes) == "קרן השתלמות"]
    if not k_txs or not btc_aid:
        st.info("אין עדיין עסקאות בקרן ההשתלמות")
    else:
        k_start = pd.Timestamp(min(t.timestamp for t in k_txs)).tz_localize(None).normalize()
        kix = pd.date_range(k_start, today, freq="D")
        k_val = sum(signed_qty(k_txs, aid, kix) * price_on(aid, kix)
                    for aid in {t.asset_id for t in k_txs} if all_assets[aid][0] != "CASH")
        k_new, k_cash = cash_model(k_txs, kix)
        k_twr = E.twr_index(k_val + k_cash, k_new)
        btc_k = price_on(btc_aid[0], kix)
        periods = [("מההתחלה", k_start), ("שנה אחרונה", today - pd.DateOffset(years=1)),
                   ("מתחילת השנה", pd.Timestamp(today.year, 1, 1)), ("3 חודשים", today - pd.DateOffset(months=3))]
        items, wins = [], 0
        for label, p0 in periods:
            p0 = max(p0, k_start)
            k_r = float(k_twr.iloc[-1] / k_twr[k_twr.index >= p0].iloc[0] - 1) * 100
            b_r = float(btc_k.iloc[-1] / btc_k[btc_k.index >= p0].iloc[0] - 1) * 100
            gap = k_r - b_r
            wins += gap > 0
            items.append(("check_circle" if gap > 0 else "cancel", f"{gap:+.1f}%", label,
                          f"הקרן {k_r:+.1f}% · ביטקוין {b_r:+.1f}%", "good" if gap > 0 else "bad"))
        cards(items)
        st.caption(f"ביצועי יתר = התשואה של הקרן (TWR, לא מושפעת מהפקדות) פחות התשואה של ביטקוין באותה תקופה. "
                   f"הקרן מנצחת ב-{wins} מתוך {len(periods)} התקופות.")
        sl_k = E.slice_range(pd.DataFrame({"קרן השתלמות": k_twr, "ביטקוין": btc_k}), rng)
        fk = go.Figure()
        for col, color in (("קרן השתלמות", "#4C8BF5"), ("ביטקוין", "#F7931A")):
            fk.add_trace(go.Scatter(x=sl_k.index, y=E.rebased(sl_k[col]), name=col, line=dict(color=color, width=2)))
        fk.update_layout(title="קרן ההשתלמות מול ביטקוין", height=380, hovermode="x unified",
                         yaxis_title="100 = תחילת הטווח", legend=dict(orientation="h", y=-0.2), margin=dict(l=10, r=10))
        st.plotly_chart(fk, use_container_width=True, config=CHART_CFG)

if section == SECTIONS[3]:
    rows = []
    for a in ids:
        if assets[a][0] == "CASH":                        # only carries account fees, nothing held
            continue
        b, p = summ.by_asset[a], prices[a]
        cost_c = float(positions[a].cost_basis) * afx[a]
        # net P/L after fees: unrealized + realized (realized carries the network/withdrawal fee losses)
        unreal_c = float(b["value"]) * fx_now - cost_c + float(b["realized"]) * afx[a]
        paid_c = float(positions[a].total_invested) * afx[a]
        held = p[vals[a] > 0] if (vals[a] > 0).any() else p
        rows.append({"נכס": assets[a][0], "סוג": he(assets[a][2]), "כמות": float(b["qty"]),
                     "עלות ממוצעת": float(b["avg_cost"]) * afx[a], "מחיר": float(b["price"]) * fx_now,
                     "שווי": float(b["value"]) * fx_now, "רווח/הפסד": unreal_c,
                     "רווח/הפסד %": unreal_c / paid_c * 100 if paid_c else 0.0, "רווח ממומש": float(b["realized"]) * fx_now,
                     "מרחק מהשיא %": E.distance_from_ath(p) * 100, "ירידה מקס׳ %": E.max_drawdown(held) * 100})
    tbl = pd.DataFrame(rows).sort_values("שווי", ascending=False) if rows else pd.DataFrame(rows)
    st.dataframe(tbl.style.format({"כמות": lambda v: f"{v:,.8f}".rstrip("0").rstrip(".") if v else "0",
                                   "עלות ממוצעת": f"{sym}{{:,.2f}}", "מחיר": f"{sym}{{:,.2f}}", "שווי": f"{sym}{{:,.0f}}",
                                   "רווח/הפסד": f"{sym}{{:+,.0f}}", "רווח/הפסד %": "{:+.1f}%",
                                   "רווח ממומש": f"{sym}{{:+,.0f}}", "מרחק מהשיא %": "{:.1f}%",
                                   "ירידה מקס׳ %": "{:.1f}%"}),
                 use_container_width=True, hide_index=True)
    st.caption("ממוין לפי שווי. נכסים שנמכרו לגמרי מופיעים עם כמות 0 ועם הרווח הממומש שלהם.")

if section == SECTIONS[4]:
    # "$" must be escaped in Streamlit markdown, otherwise "$...$" turns into a math formula
    usd = lambda v: money(v * fx_now).replace("$", "\\$")
    pct = lambda v: f"{v:+.1f}%"
    st.caption("ניתוח התזמון של הקניות והמכירות, לפי התיקים שבחרת למעלה. תיאור של מה שקרה, לא המלצה.")

    buys_t = I.trade_timing(tx_f, assets, prices, "BUY")
    sells_t = I.trade_timing(tx_f, assets, prices, "SELL")

    def share(df, mask):
        return df[mask].usd.sum() / df.usd.sum() * 100 if df.usd.sum() else 0.0

    cash_fmt = lambda v: money(v * fx_now).replace("$", "&#36;")      # same, inside HTML cards

    def buy_cards(df):
        out = []
        rp = I.wavg(df, "range_pct")
        out.append(("location_on", f"{rp:.0f}%", "איפה בטווח החודשי קנית",
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
        out.append(("trending_down", f"{share(df, down):.0f}%", "קנית בירידות", txt, tone))
        a90 = I.wavg(df, "after_90")
        if a90 is not None:
            out.append(("timer", pct(a90), "90 יום אחרי קנייה", "השינוי הממוצע במחיר ב-3 החודשים שאחרי כל קנייה.",
                        "good" if a90 > 0 else "bad"))
        d = df.dropna(subset=["after_90"])
        if len(d) >= 3:
            bst, wst = d.loc[d.after_90.idxmax()], d.loc[d.after_90.idxmin()]
            out.append(("emoji_events", pct(bst.after_90), "הקנייה הכי מוצלחת",
                        f"{bst.asset} · {bst.date:%d/%m/%Y} · {cash_fmt(bst.usd)}", "good"))
            out.append(("sentiment_dissatisfied", pct(wst.after_90), "הקנייה הכי פחות מוצלחת",
                        f"{wst.asset} · {wst.date:%d/%m/%Y} · {cash_fmt(wst.usd)}", "bad"))
        return out

    st.subheader(":material/add_shopping_cart: קניות")
    btc_b = buys_t[buys_t.asset == "BTC"] if not buys_t.empty else buys_t
    stk_b = buys_t[buys_t.asset != "BTC"] if not buys_t.empty else buys_t
    if not btc_b.empty:
        st.markdown("##### ביטקוין")
        items = []
        bid = next(a for a in assets if assets[a][0] == "BTC")
        ap = I.avg_price_vs_period(tx_f, prices, bid)
        if ap:
            items.append(("target", pct(ap["diff_pct"]), "מחיר ממוצע מול קנייה יומית",
                          f"שילמת בממוצע {cash_fmt(ap['mine'])} מול {cash_fmt(ap['period_avg'])} – הממוצע היומי "
                          f"בתקופה שקנית ({ap['first']:%m/%Y}–{ap['last']:%m/%Y}).",
                          "good" if ap["diff_pct"] < 0 else "bad"))
        items += buy_cards(btc_b)
        near, deep = btc_b.from_ath > -10, btc_b.from_ath < -30
        items.append(("landscape", f"{I.wavg(btc_b, 'from_ath'):.0f}%", "מרחק ממוצע מהשיא בזמן קנייה",
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
        st.subheader(":material/sell: מכירות")
        items = []
        rp = I.wavg(sells_t, "range_pct")
        items.append(("location_on", f"{rp:.0f}%", "איפה בטווח החודשי מכרת",
                      "100% = מכרת ביום הכי יקר בחודש שמסביב. "
                      + ("מכרת קרוב לשיאים." if rp > 65 else "מכרת קרוב לתחתיות." if rp < 35
                         else "בערך כמו מכירה בזמן אקראי."),
                      "good" if rp > 65 else "bad" if rp < 35 else None))
        for col, n in (("after_30", 30), ("after_90", 90)):
            v = I.wavg(sells_t, col)
            if v is not None:
                items.append(("visibility", pct(v), f"{n} יום אחרי מכירה",
                              "המחיר המשיך לרדת – המכירות הקדימו ירידות." if v < 0 else "המחיר עלה אחרי שמכרת.",
                              "good" if v < 0 else "bad"))
        drop = sells_t[sells_t.before_30 < -15]
        if len(drop):
            v = I.wavg(drop, "after_30")
            items.append(("warning", str(len(drop)), "מכירות אחרי ירידה חדה",
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
        st.subheader(":material/location_on: איפה קנית ומכרת")
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
            usd_amt = [float(t.amount * t.price_per_unit) * fx_now for t in k]
            mx = max(usd_amt)
            fm.add_trace(go.Scatter(x=dd, y=[float(ps.get(d, float("nan"))) for d in dd], mode="markers", name=label,
                                    marker=dict(color=color, symbol=symbol, line=dict(width=0),
                                                size=[8 + 16 * (u / mx) ** 0.5 for u in usd_amt]),
                                    customdata=usd_amt,
                                    hovertemplate=label + " " + sym + "%{customdata:,.0f}<extra></extra>"))
        fm.update_layout(height=380, hovermode="closest", legend=dict(orientation="h", y=-0.2),
                         margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(fm, use_container_width=True, config=CHART_CFG)
        st.caption("גודל הסימון לפי סכום העסקה.")

    rt = I.rotations(tx_f, assets, prices)
    if not rt.empty:
        st.subheader(":material/sync_alt: החלפות בין נכסים")
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
                lessons.append(("trending_down", pct(d90 - u90), f"{name}: ירידות היו הזדמנות",
                                f"קניות אחרי ירידה של 10%+ הניבו {pct(d90)} ב-90 יום, מול {pct(u90)} אחרי עליות. "
                                f"כלל לשקול: לשמור חלק מהתקציב לימים של ירידה חדה.", "good"))
            else:
                lessons.append(("rocket_launch", pct(u90 - d90), f"{name}: המומנטום עבד לך",
                                f"קניות אחרי עלייה הניבו {pct(u90)} ב-90 יום, מול {pct(d90)} אחרי ירידות – "
                                f"אצלך 'לתפוס סכין נופלת' עבד פחות טוב.", None))
        rp = I.wavg(df, "range_pct")
        if rp is not None and rp > 60:
            lessons.append(("schedule", f"{rp:.0f}%", f"{name}: לא למהר לקנות",
                            "רוב הכסף נקנה קרוב לשיאים המקומיים של החודש. פיצול כל קנייה ל-3–4 חלקים "
                            "על פני כמה שבועות היה מוריד את המחיר הממוצע.", "bad"))
    if not btc_b.empty and ap:
        if ap["diff_pct"] > 1:
            lessons.append(("smart_toy", pct(ap["diff_pct"]), "ביטקוין: קנייה קבועה הייתה זולה יותר",
                            "קנייה יומית או שבועית באותו סכום הייתה נותנת מחיר ממוצע נמוך יותר מהתזמון הידני. "
                            "כלל לשקול: להפוך חלק מהקניות להוראה קבועה.", "bad"))
        elif ap["diff_pct"] < -1:
            lessons.append(("target", pct(ap["diff_pct"]), "ביטקוין: התזמון שלך עובד",
                            f"המחיר הממוצע שלך נמוך ב-{abs(ap['diff_pct']):.1f}% מקנייה יומית קבועה. "
                            "מה שעשית עד עכשיו – לקנות יותר כשהמחיר יורד – הוכיח את עצמו.", "good"))
    if not sells_t.empty:
        a90 = I.wavg(sells_t, "after_90")
        if a90 is not None and a90 > 5:
            lessons.append(("visibility", pct(a90), "מניות: מכרת מוקדם מדי",
                            "90 יום אחרי המכירות המחיר היה גבוה יותר. כלל לשקול: לפני מכירה, להגדיר מראש "
                            "מחיר או סיבה למכירה, ולא למכור מתוך תגובה לתנודה.", "bad"))
        drop = sells_t[sells_t.before_30 < -15]
        v = I.wavg(drop, "after_30") if len(drop) else None
        if v is not None and v > 0:
            lessons.append(("warning", pct(v), "מניות: לא למכור בפאניקה",
                            f"{len(drop)} מכירות היו אחרי ירידה חדה, ובחודש שאחריהן המחיר התאושש. "
                            "כלל לשקול: לחכות כמה ימים אחרי ירידה חדה לפני החלטה על מכירה.", "bad"))
    if not rt.empty:
        wins, n = int((rt.edge > 0).sum()), len(rt)
        e = rt.edge.sum()
        if e < 0:
            lessons.append(("sync_alt", usd(e).replace("\\$", "&#36;"), "מניות: פחות החלפות",
                            f"ההחלפות בין נכסים עבדו ב-{wins} מתוך {n} פעמים, ובסך הכול עלו לך כסף. "
                            "כל החלפה היא הימור שהנכס החדש יעשה יותר טוב – כלל לשקול: להחליף רק עם סיבה ברורה.",
                            "bad"))
    cv_now = {assets[a][0]: float(vals[a].iloc[-1]) for a in ids}
    cc = I.concentration(cv_now)
    if cc and cc["btc_linked_pct"] > 80 and len([v for v in cv_now.values() if v > 0]) > 1:
        lessons.append(("hub", f"{cc['btc_linked_pct']:.0f}%", "הפיזור כאן הוא בעיקר שמות",
                        "כמעט כל התיק נע עם מחיר הביטקוין, גם המניות והקרנות. אם המטרה היא פיזור סיכון, "
                        "הוא יגיע רק מנכסים שלא קשורים לביטקוין.", None))
    if lessons:
        st.subheader(":material/explore: מסקנות להמשך")
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

if section == SECTIONS[5]:
    btc_all = PL.with_early_history(history("BTC-USD", "2010-07-17"))   # full history from 07/2010
    if btc_all.empty:
        st.warning("לא הצלחתי לטעון את היסטוריית מחיר הביטקוין")
    else:
        mode = st.radio("חלוקה", ["עשירונים", "חמישונים"], horizontal=True, key="pl_k")
        k = 10 if mode == "עשירונים" else 5
        unit = "עשירון" if k == 10 else "חמישון"
        live_btc = live.get(CG["BTC"]) if CG["BTC"] in live else None
        stt = PL.status(btc_all, k, now_price=live_btc)
        b = stt["band"]
        buy_zone = b <= 2 if k == 10 else b == 1
        if b == 1:
            st.success(icon=":material/south:", body=f"**{unit} התחתון** – הביטקוין זול יותר ביחס למגמה מ-{100 - stt['percentile']:.0f}% מהימים "
                       f"בהיסטוריה. לפי הכלל שלך: אזור להגדלת ה-DCA.")
        elif buy_zone:
            st.success(icon=":material/south_east:", body=f"**{unit} {b} מלמטה** – מתחת ל-{stt['percentile']:.0f}% מהימים בהיסטוריה זה היה זול עוד יותר. "
                       f"לפי הכלל שלך: אזור להגדלת ה-DCA.")
        else:
            st.info(f"{unit} **{b} מתוך {k}** – לא באזור התחתון. אין אות להגדלת DCA.")

        d2 = lambda v: f"${v:,.0f}".replace("$", "&#36;")
        e = stt["edges_today"]
        zone_top = e[1] if k == 10 else e[0]          # top of the bottom two deciles / bottom quintile
        above = (stt["price"] / zone_top - 1) * 100
        cards([("location_on", f"{b}/{k}", f"{unit} נוכחי", f"אחוזון {stt['percentile']:.0f} מכל ההיסטוריה ביחס למגמה.",
                "good" if buy_zone else None),
               ("₿", d2(stt["price"]), "מחיר עכשיו", f"{stt['ratio']:.2f}× מקו המגמה", None),
               ("straighten", d2(stt["trend"]), "קו המגמה היום", "Power Law של Santostasi", None),
               ("target", d2(zone_top), "גבול אזור ההגדלה היום",
                ("מתחת למחיר הזה = שני העשירונים התחתונים." if k == 10 else "מתחת למחיר הזה = החמישון התחתון.")
                + (" כרגע בתוך האזור." if buy_zone else f" כרגע {above:.0f}% מעליו."),
                "good" if buy_zone else None)])

        # chart: price (log scale) inside the bands, from the start of the data to 2 years ahead
        future = pd.date_range(btc_all.index[-1] + pd.Timedelta(days=1), periods=730, freq="D")
        full_idx = btc_all.index.append(future)
        bp = PL.band_prices(full_idx, PL.edges(btc_all, k))
        weekly = bp.iloc[::7]
        cols = [f"e{i}" for i in range(1, k)]
        greens_reds = ["#1a7f4b", "#2e9e5b", "#6cbf5a", "#b5d455", "#f2d649", "#f2b84b", "#ee8a45", "#e3613f", "#c8373a"]
        fill = greens_reds if k == 10 else [greens_reds[i] for i in (1, 3, 5, 7)]
        fp = go.Figure()
        for i, c in enumerate(cols):
            fp.add_trace(go.Scatter(x=weekly.index, y=weekly[c], mode="lines", line=dict(width=0.8, color=fill[i]),
                                    name=f"גבול {unit} {i + 1}", hovertemplate="$%{y:,.0f}<extra>" + f"{i + 1}|{i + 2}" + "</extra>",
                                    showlegend=False))
        fp.add_trace(go.Scatter(x=weekly.index, y=weekly["trend"], mode="lines", name="קו המגמה",
                                line=dict(color="#9aa0a6", width=1.5, dash="dash"), hovertemplate="$%{y:,.0f}<extra>מגמה</extra>"))
        fp.add_trace(go.Scatter(x=btc_all.index, y=btc_all, mode="lines", name="BTC", line=dict(color="#F7931A", width=1.8),
                                hovertemplate="$%{y:,.0f}<extra>BTC</extra>"))
        my_btc = [t for t in txs if assets[t.asset_id][0] == "BTC" and t.type == "BUY"]
        if my_btc:
            fp.add_trace(go.Scatter(x=[pd.Timestamp(t.timestamp).tz_localize(None) for t in my_btc],
                                    y=[float(t.price_per_unit) for t in my_btc], mode="markers", name="הקניות שלך",
                                    marker=dict(color="#ffffff", size=5, line=dict(color="#F7931A", width=1)),
                                    hovertemplate="$%{y:,.0f}<extra>קנייה</extra>"))
        start_view = btc_all.index[0]                                    # the whole map, from the first prices
        fp.update_layout(title=f"BTC מול Power Law – {mode}", height=460, hovermode="x unified",
                         legend=dict(orientation="h", y=-0.15), margin=dict(l=10, r=10),
                         yaxis=dict(type="log", tickprefix="$", title=None),
                         xaxis=dict(range=[start_view, future[-1]], title=None))
        st.plotly_chart(fp, use_container_width=True, config=CHART_CFG)
        st.caption(f"כל קו צבעוני הוא גבול בין {unit} ל{unit}: ירוק בתחתית (זול ביחס למגמה) עד אדום למעלה. "
                   f"החלוקה לפי כל ימי המסחר מאז {btc_all.index[0]:%d/%m/%Y} (המחירים הראשונים בבורסה, Mt.Gox). "
                   f"אפשר להגדיל קטע בצביטה.")

        # where the buy zone will be in the coming months (the trend keeps rising)
        months = pd.date_range(pd.Timestamp.today().normalize(), periods=13, freq="MS")[1:]
        mp = PL.band_prices(months, PL.edges(btc_all, k))
        st.markdown("##### גבול אזור ההגדלה בחודשים הקרובים")
        st.dataframe(pd.DataFrame({"חודש": [d.strftime("%m/%Y") for d in months],
                                   "גבול אזור ההגדלה": mp["e2" if k == 10 else "e1"].round(0),
                                   "קו המגמה": mp["trend"].round(0)})
                     .style.format({"גבול אזור ההגדלה": "${:,.0f}", "קו המגמה": "${:,.0f}"}),
                     use_container_width=True, hide_index=True)

TYPE_HE = {"BUY": "קנייה", "SELL": "מכירה", "FEE": "עמלה", "TRANSFER": "העברה", "DIVIDEND": "דיבידנד",
           "STAKING_REWARD": "תגמול"}
with st.expander("ספר עסקאות", icon=":material/menu_book:"):
    log = pd.DataFrame([{"תאריך": pd.Timestamp(t.timestamp).date(), "תיק": A.account_of(t.notes),
                         "נכס": assets[t.asset_id][0], "סוג": TYPE_HE.get(t.type, t.type),
                         "כמות": float(t.amount), "מחיר ($)": float(t.price_per_unit), "עמלה ($)": float(t.fee),
                         "הערות": t.notes} for t in tx_f]).sort_values("תאריך", ascending=False)
    st.dataframe(log.style.format({"כמות": lambda v: f"{v:,.8f}".rstrip("0").rstrip(".") if v else "0",
                                   "מחיר ($)": "{:,.2f}", "עמלה ($)": "{:,.2f}"}),
                 use_container_width=True, hide_index=True)
    st.caption(f"{len(log)} שורות, החדשות למעלה.")
