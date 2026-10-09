import os
import re
from decimal import Decimal as D

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
    h = yf.Ticker("BTC-USD").history(start=start, auto_adjust=True)["Close"]
    h.index = h.index.tz_localize(None).normalize()
    return h


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

st.title("💸 עמלות")
t1, t2 = st.tabs(["🏦 עמלות בורסה", "⛓️ עמלות רשת"])

# ---------------- exchange fees ----------------
with t1:
    ex = df[(df.type.isin(["BUY", "SELL"])) & (df.fee > 0) & (~df.notes.str.startswith("Network fee"))].copy()
    if ex.empty:
        st.info("אין עמלות בורסה")
    else:
        ex["platform"] = ex.notes.map(platform)
        ex["trade_usd"] = ex.amount * ex.price
        ex["fee_pct"] = ex.fee / ex.trade_usd * 100
        c = st.columns(3)
        c[0].metric("סך עמלות", f"${ex.fee.sum():,.2f}")
        c[1].metric("עסקאות", len(ex))
        c[2].metric("עמלה ממוצעת", f"{ex.fee.sum() / ex.trade_usd.sum() * 100:.2f}%")

        by = ex.groupby("platform").agg(עסקאות=("fee", "size"), נפח=("trade_usd", "sum"),
                                        עמלות=("fee", "sum")).reset_index()
        by["עמלה %"] = by["עמלות"] / by["נפח"] * 100
        st.subheader("לפי פלטפורמה")
        st.dataframe(by.rename(columns={"platform": "פלטפורמה"}).style.format(
            {"נפח": "${:,.0f}", "עמלות": "${:,.2f}", "עמלה %": "{:.2f}%"}),
            use_container_width=True, hide_index=True)
        st.plotly_chart(px.bar(by, x="platform", y="עמלות", text_auto=".0f", title="עמלות ($) לפי פלטפורמה"),
                        use_container_width=True)

        st.subheader("פירוט עסקאות")
        det = ex[["date", "platform", "asset", "type", "amount", "trade_usd", "fee", "fee_pct", "notes"]]
        det = det.sort_values("date", ascending=False)
        det["date"] = det["date"].dt.date
        st.dataframe(det.rename(columns={"date": "תאריך", "platform": "פלטפורמה", "asset": "נכס", "type": "סוג",
                                         "amount": "כמות", "trade_usd": "שווי עסקה $", "fee": "עמלה $",
                                         "fee_pct": "עמלה %", "notes": "הערות"}).style.format(
            {"כמות": "{:.8f}", "שווי עסקה $": "{:,.2f}", "עמלה $": "{:,.2f}", "עמלה %": "{:.2f}"}),
            use_container_width=True, hide_index=True)

# ---------------- network fees ----------------
with t2:
    nf = df[df.notes.str.startswith("Network fee")].copy()
    if nf.empty:
        st.info("אין עמלות רשת")
    else:
        nf["sat"] = (nf.amount * 1e8).round().astype(int)
        nf["kind"] = nf.notes.map(lambda s: "מהארנק (on-chain)" if "on-chain" in s else "משיכה מבורסה")
        nf["source"] = nf.notes.map(lambda s: "ארנק" if "on-chain" in s else platform(s))
        nf["txid"] = nf.notes.map(lambda s: (re.search(r"tx (\w+)", s) or [None, ""])[1])
        px_btc = btc_prices(nf.date.min().strftime("%Y-%m-%d")).reindex(
            pd.date_range(nf.date.min(), pd.Timestamp.today().normalize()), method="ffill")
        nf["usd"] = nf.amount * nf.date.map(px_btc).astype(float)
        nf["link"] = nf.txid.map(lambda t: f"https://mempool.space/tx/{t}" if t else None)
        c = st.columns(3)
        c[0].metric("סה״כ עמלות רשת", f"{nf.sat.sum():,} sat", f"{nf.amount.sum():.8f} BTC", delta_color="off")
        c[1].metric("שווי משוער", f"${nf.usd.sum():,.2f}")
        c[2].metric("מספר עמלות", len(nf))
        by = nf.groupby("kind").agg(עמלות=("sat", "size"), sat=("sat", "sum"), BTC=("amount", "sum"),
                                    usd=("usd", "sum")).reset_index()
        st.dataframe(by.rename(columns={"kind": "סוג", "usd": "שווי $"}).style.format(
            {"sat": "{:,}", "BTC": "{:.8f}", "שווי $": "{:,.2f}"}), use_container_width=True, hide_index=True)
        st.plotly_chart(px.bar(nf, x="date", y="sat", color="kind", title="עמלות רשת (sat) לפי תאריך"),
                        use_container_width=True)
        det = nf.sort_values("date", ascending=False)[["date", "kind", "source", "sat", "amount", "usd", "link"]].copy()
        det["date"] = det["date"].dt.date
        st.dataframe(det, use_container_width=True, hide_index=True, column_config={
            "date": "תאריך", "kind": "סוג", "source": "מקור", "sat": "sat",
            "amount": st.column_config.NumberColumn("BTC", format="%.8f"),
            "usd": st.column_config.NumberColumn("שווי $", format="%.2f"),
            "link": st.column_config.LinkColumn("טרנזקציה", display_text=r"tx/(\w{8})")})
        st.caption("עמלות משיכה מבורסה נגזרו מדוחות Bits of Gold ו-Coinbox. עמלות on-chain נלקחו מ-mempool.space.")
