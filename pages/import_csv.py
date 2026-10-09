import csv
import io
import os
from datetime import datetime, time
from decimal import Decimal as D

import psycopg
import streamlit as st

st.set_page_config(page_title="ייבוא ומחיקה", page_icon="📥")

clean = lambda s: "".join(ch for ch in (s or "") if ch.isascii() and not ch.isspace())
URL, UID = clean(os.getenv("DATABASE_URL")), clean(os.getenv("USER_ID"))
if not (URL and UID):
    st.error("חסרים DATABASE_URL / USER_ID ב-Secrets")
    st.stop()

pw = os.getenv("APP_PASSWORD")
if pw and st.text_input("סיסמה", type="password") != pw:
    st.stop()

st.title("📥 ייבוא ומחיקה")
tab_imp, tab_del = st.tabs(["ייבוא CSV", "🗑️ מחיקה"])

DEFAULTS = {"BTC": ("Bitcoin", "crypto", "BTC-USD")}

with tab_imp:
    st.caption("עמודות: date, symbol, type, amount, price_per_unit, fee, currency, notes. שורות שכבר קיימות מדולגות.")
    up = st.file_uploader("בחר קובץ CSV", type="csv")
    replace = st.checkbox("מחק את כל העסקאות הקיימות לפני הייבוא")
    if up:
        rows = list(csv.DictReader(io.StringIO(up.getvalue().decode("utf-8-sig"))))
        st.write(f"נמצאו {len(rows)} שורות")
        st.dataframe(rows, use_container_width=True, hide_index=True)
        if st.button("ייבא עכשיו", type="primary"):
            with psycopg.connect(URL) as c:
                if replace:
                    c.execute("DELETE FROM transactions WHERE user_id = %s", (UID,))
                assets = {s: i for i, s in c.execute("SELECT id, symbol FROM assets")}
                for sym in {r["symbol"].upper() for r in rows} - set(assets):
                    name, cls, api = DEFAULTS.get(sym, (sym, "equity", sym))
                    assets[sym] = c.execute("INSERT INTO assets(symbol,name,asset_class,api_id) "
                                            "VALUES (%s,%s,%s::asset_class,%s) RETURNING id",
                                            (sym, name, cls, api)).fetchone()[0]
                existing = {(str(d), s, t, D(a), n) for d, s, t, a, n in c.execute(
                    """SELECT t."timestamp"::date, a.symbol, t.type::text, t.amount, coalesce(t.notes,'')
                       FROM transactions t JOIN assets a ON a.id = t.asset_id WHERE t.user_id = %s""", (UID,))}
                added = skipped = 0
                for r in rows:
                    sym = r["symbol"].upper()
                    if (r["date"], sym, r["type"], D(r["amount"]), r["notes"]) in existing:
                        skipped += 1
                        continue
                    c.execute("""INSERT INTO transactions(user_id,asset_id,type,amount,price_per_unit,fee,currency,"timestamp",notes)
                                 VALUES (%s,%s,%s::tx_type,%s,%s,%s,%s,%s,%s)""",
                              (UID, assets[sym], r["type"], D(r["amount"]), D(r["price_per_unit"]), D(r["fee"] or 0),
                               r["currency"] or "USD",
                               datetime.combine(datetime.fromisoformat(r["date"]).date(), time(12)), r["notes"]))
                    added += 1
            st.cache_data.clear()
            st.success(f"נוספו {added} שורות, דולגו {skipped} שורות קיימות ✅")

with tab_del:
    with psycopg.connect(URL) as c:
        n = c.execute("SELECT count(*) FROM transactions WHERE user_id = %s", (UID,)).fetchone()[0]
    st.write(f"יש כרגע **{n}** עסקאות באפליקציה.")
    st.warning("המחיקה סופית ומוחקת את כל העסקאות. הנכסים (BTC וכו') נשארים.")
    ok = st.text_input("כדי לאשר, כתוב: מחק")
    if st.button("🗑️ מחק את כל העסקאות"):
        if ok.strip() == "מחק":
            with psycopg.connect(URL) as c:
                c.execute("DELETE FROM transactions WHERE user_id = %s", (UID,))
            st.cache_data.clear()
            st.success("כל העסקאות נמחקו ✅")
        else:
            st.error("לא אושר. כתוב בדיוק: מחק")
