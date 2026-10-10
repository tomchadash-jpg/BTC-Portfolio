import os
import re

import pandas as pd
import plotly.express as px
import psycopg
import streamlit as st
import yfinance as yf

st.set_page_config(page_title="עמלות", page_icon="💸", layout="wide")

clean = lambda s: "".join(ch for ch in (s or "") if ch.isascii() and not ch.isspace())
URL, UID = clean(os.getenv("DATABASE_URL")), clean(os.getenv("USER_ID"))
if not (URL and UID):
    st.error("חסרים DATABASE_URL / USER_ID ב-Secrets")
    st.stop()


@st.cache_data(ttl=300)
def load():
    with psycopg.connect(URL) as c:
        return c.execute("""SELECT t."timestamp"::date, a.symbol, t.type::text, t.amount, t.price_per_unit,
                                   t.fee, coalesce(t.notes,'')
                            FROM transactions t JOIN assets a ON a.id = t.asset_id
                            WHERE t.user_id = %s ORDER BY t."timestamp" """, (UID,)).fetchall()


@st.cache_data(ttl=3600)
def btc_prices(start):
    h = yf.Ticker("BTC-USD").history(start=start, auto_adjust=True)
    if h.empty:
        return pd.Series(dtype=float)
    s = h["Close"]
    s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
    return s[~s.index.duplicated()]


def platform(notes):
    for name in ("Horizon", "Bits of Gold", "Coinbox"):
        if name in notes:
            return name
    return "אחר"


rows = load()
df = pd.DataFrame(rows, columns=["date", "asset", "type", "amount", "price", "fee", "notes"])
if df.empty:
    st.info("אין עסקאות עדיין. ייבא קובץ בעמוד import csv")
    st.stop()
df["date"] = pd.to_datetime(df["date"])
for col in ("amount", "price", "fee"):
    df[col] = df[col].astype(float)

is_wd = df.notes.str.startswith("Network fee (withdrawal)")   # fees the exchanges deducted on withdrawal
is_oc = df.notes.str.startswith("Network fee (on-chain)")     # real network fees, from the blockchain

# BTC price per day, to value fees paid in BTC
px_btc = None
btc_fees = df[is_wd | is_oc]
if not btc_fees.empty:
    p = btc_prices(btc_fees.date.min().strftime("%Y-%m-%d"))
    if not p.empty:
        px_btc = p.reindex(pd.date_range(btc_fees.date.min(), pd.Timestamp.today().normalize()), method="ffill")


def usd_value(frame):
    if px_btc is None:
        return frame.amount * 0.0
    return frame.amount * frame.date.map(px_btc).astype(float)


st.title("💸 עמלות")
t1, t2 = st.tabs(["🏦 עמלות בורסה", "⛓️ עמלות רשת"])

# ---------------- exchange fees: trading + withdrawal ----------------
with t1:
    ex = df[(df.type.isin(["BUY", "SELL"])) & (df.fee > 0) & (~df.notes.str.startswith("Network fee"))].copy()
    wd = df[is_wd].copy()
    if ex.empty and wd.empty:
        st.info("אין עמלות בורסה")
    else:
        ex["platform"] = ex.notes.map(platform)
        ex["trade_usd"] = ex.amount * ex.price
        ex["fee_pct"] = ex.fee / ex.trade_usd * 100
        wd["platform"] = wd.notes.map(platform)
        wd["sat"] = (wd.amount * 1e8).round().astype(int)
        wd["usd"] = usd_value(wd)

        trade_fees, wd_fees = float(ex.fee.sum()), float(wd.usd.sum())
        c = st.columns(3)
        c[0].metric("סך עמלות בורסה", f"${trade_fees + wd_fees:,.2f}")
        c[1].metric("עמלות מסחר", f"${trade_fees:,.2f}")
        c[2].metric("עמלות משיכה", f"${wd_fees:,.2f}")
        if not ex.empty:
            st.caption(f"{len(ex)} עסקאות קנייה · עמלת מסחר ממוצעת {trade_fees / ex.trade_usd.sum() * 100:.2f}% מהנפח · "
                       f"עמלות משיכה: {wd.sat.sum():,} sat")

        by = ex.groupby("platform").agg(עסקאות=("fee", "size"), נפח=("trade_usd", "sum"), מסחר=("fee", "sum"))
        by = by.join(wd.groupby("platform").agg(משיכה=("usd", "sum")), how="outer").fillna(0).reset_index()
        by["עסקאות"] = by["עסקאות"].astype(int)
        by["סה״כ"] = by["מסחר"] + by["משיכה"]
        by["עמלה % מהנפח"] = (by["סה״כ"] / by["נפח"] * 100).where(by["נפח"] > 0, 0.0)
        st.subheader("לפי פלטפורמה")
        st.dataframe(by.rename(columns={"platform": "פלטפורמה"}).style.format(
            {"נפח": "${:,.0f}", "מסחר": "${:,.2f}", "משיכה": "${:,.2f}", "סה״כ": "${:,.2f}",
             "עמלה % מהנפח": "{:.2f}%"}), use_container_width=True, hide_index=True)
        st.plotly_chart(px.bar(by, x="platform", y=["מסחר", "משיכה"], barmode="stack",
                               title="עמלות ($) לפי פלטפורמה", labels={"value": "$", "variable": ""}),
                        use_container_width=True)

        if not ex.empty:
            st.subheader("פירוט עמלות מסחר")
            det = ex[["date", "platform", "asset", "type", "amount", "trade_usd", "fee", "fee_pct", "notes"]]
            det = det.sort_values("date", ascending=False)
            det["date"] = det["date"].dt.date
            st.dataframe(det.rename(columns={"date": "תאריך", "platform": "פלטפורמה", "asset": "נכס", "type": "סוג",
                                             "amount": "כמות", "trade_usd": "שווי עסקה $", "fee": "עמלה $",
                                             "fee_pct": "עמלה %", "notes": "הערות"}).style.format(
                {"כמות": "{:.8f}", "שווי עסקה $": "{:,.2f}", "עמלה $": "{:,.2f}", "עמלה %": "{:.2f}"}),
                use_container_width=True, hide_index=True)
        if not wd.empty:
            st.subheader("פירוט עמלות משיכה")
            w = wd.sort_values("date", ascending=False)[["date", "platform", "sat", "amount", "usd"]].copy()
            w["date"] = w["date"].dt.date
            st.dataframe(w, use_container_width=True, hide_index=True, column_config={
                "date": "תאריך", "platform": "פלטפורמה", "sat": "sat",
                "amount": st.column_config.NumberColumn("BTC", format="%.8f"),
                "usd": st.column_config.NumberColumn("שווי $", format="%.2f")})
            st.caption("עמלות המשיכה נגזרו מדוחות Bits of Gold ו-Coinbox, ושוויין לפי מחיר ה-BTC ביום המשיכה.")

# ---------------- network fees: on-chain only ----------------
with t2:
    nf = df[is_oc].copy()
    if nf.empty:
        st.info("אין עמלות רשת")
    else:
        nf["sat"] = (nf.amount * 1e8).round().astype(int)
        nf["txid"] = nf.notes.map(lambda s: (re.search(r"tx (\w+)", s) or [None, ""])[1])
        nf["usd"] = usd_value(nf)
        nf["link"] = nf.txid.map(lambda t: f"https://mempool.space/tx/{t}" if t else None)
        c = st.columns(3)
        c[0].metric("סה״כ עמלות רשת", f"{nf.sat.sum():,} sat")
        c[1].metric("שווי משוער", f"${nf.usd.sum():,.2f}")
        c[2].metric("טרנזקציות", len(nf))
        st.caption(f"{nf.amount.sum():.8f} BTC")
        st.plotly_chart(px.bar(nf, x="date", y="sat", text_auto=True, title="עמלות רשת (sat) לפי תאריך"),
                        use_container_width=True)
        det = nf.sort_values("date", ascending=False)[["date", "sat", "amount", "usd", "link"]].copy()
        det["date"] = det["date"].dt.date
        st.dataframe(det, use_container_width=True, hide_index=True, column_config={
            "date": "תאריך", "sat": "sat",
            "amount": st.column_config.NumberColumn("BTC", format="%.8f"),
            "usd": st.column_config.NumberColumn("שווי $", format="%.2f"),
            "link": st.column_config.LinkColumn("טרנזקציה", display_text=r"tx/(\w{8})")})
        st.caption("עמלות על-ארנק בלבד, מ-mempool.space. עמלות משיכה שגבו הבורסות מופיעות בטאב עמלות בורסה.")
