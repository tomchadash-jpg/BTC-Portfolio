import os
from datetime import datetime, time
from decimal import Decimal as D

import psycopg
import streamlit as st

st.set_page_config(page_title="הוסף עסקה", page_icon="➕")

clean = lambda s: "".join(ch for ch in (s or "") if ch.isascii() and not ch.isspace())
URL, UID = clean(os.getenv("DATABASE_URL")), clean(os.getenv("USER_ID"))
if not (URL and UID):
    st.error("חסרים DATABASE_URL / USER_ID ב-Secrets")
    st.stop()

pw = os.getenv("APP_PASSWORD")
if pw and st.text_input("סיסמה", type="password") != pw:
    st.stop()


def q(sql, args=None, fetch=True):
    with psycopg.connect(URL) as c:
        cur = c.execute(sql, args)
        return cur.fetchall() if fetch else None


st.title("➕ עסקה חדשה")
st.caption("הזן מחירים ועמלות ב-USD (המרת מטבעות עדיין לא נתמכת במנוע)")

with st.expander("נכס חדש"):
    with st.form("asset", clear_on_submit=True):
        sym = st.text_input("סימול (BTC)")
        name = st.text_input("שם")
        cls = st.selectbox("סוג", ["equity", "crypto", "digital_asset", "cash", "bond"])
        api = st.text_input("Yahoo ticker (BTC-USD, SPY, או CASH למזומן)")
        if st.form_submit_button("שמור נכס") and sym and api:
            q("INSERT INTO assets(symbol,name,asset_class,api_id) VALUES (%s,%s,%s::asset_class,%s)",
              (sym.upper(), name or sym, cls, api), fetch=False)
            st.cache_data.clear()
            st.rerun()

assets = q("SELECT id, symbol, asset_class::text FROM assets ORDER BY symbol")
if not assets:
    st.info("הוסף נכס קודם")
    st.stop()

labels = {f"{s} ({c})": i for i, s, c in assets}
with st.form("tx", clear_on_submit=True):
    a = st.selectbox("נכס", list(labels))
    typ = st.selectbox("סוג", ["BUY", "SELL", "STAKING_REWARD", "DIVIDEND", "FEE", "TRANSFER"])
    amount = st.number_input("כמות (יחידות)", min_value=0.0, format="%.8f")
    price = st.number_input("מחיר ליחידה ($)", min_value=0.0, format="%.8f")
    fee = st.number_input("עמלה ($)", min_value=0.0, format="%.8f")
    d = st.date_input("תאריך")
    notes = st.text_input("הערות")
    if st.form_submit_button("שמור עסקה"):
        if typ != "FEE" and amount <= 0:
            st.error("הכמות חייבת להיות גדולה מ-0")
        else:
            q("""INSERT INTO transactions(user_id,asset_id,type,amount,price_per_unit,fee,currency,"timestamp",notes)
                 VALUES (%s,%s,%s::tx_type,%s,%s,%s,'USD',%s,%s)""",
              (UID, labels[a], typ, D(str(amount)), D(str(price)), D(str(fee)),
               datetime.combine(d, time(12)), notes), fetch=False)
            st.cache_data.clear()
            st.success("נשמר ✅")

st.subheader("עסקאות אחרונות")
rows = q("""SELECT t."timestamp"::date, a.symbol, t.type::text, t.amount, t.price_per_unit, t.fee
            FROM transactions t JOIN assets a ON a.id=t.asset_id
            WHERE t.user_id=%s ORDER BY t."timestamp" DESC, t.id DESC LIMIT 10""", (UID,))
st.dataframe([dict(zip(["תאריך", "נכס", "סוג", "כמות", "מחיר", "עמלה"], map(str, r))) for r in rows],
             use_container_width=True, hide_index=True)
