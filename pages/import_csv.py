import os

import psycopg
import streamlit as st

st.set_page_config(page_title="ייבוא ומחיקה", page_icon=":material/upload_file:")

import ui
ui.rtl()
import gate
gate.require_login()  # nothing below runs without the password
import menu
menu.top()
import exchange_cash as XC
import dedupe
import importer

clean = lambda s: "".join(ch for ch in (s or "") if ch.isascii() and not ch.isspace())
URL, UID = clean(os.getenv("DATABASE_URL")), clean(os.getenv("USER_ID"))
if not (URL and UID):
    st.error("חסרים DATABASE_URL / USER_ID ב-Secrets")
    st.stop()

st.title(":material/upload_file: ייבוא ומחיקה")
tab_imp, tab_del = st.tabs(["ייבוא CSV", ":material/delete: מחיקה"])

with tab_imp:
    st.caption("עמודות: date, symbol, type, amount, price_per_unit, fee, currency, notes. שורות שכבר קיימות מדולגות. "
               "שורות עם symbol=ILS הן מזומן בבורסה (DEPOSIT / SPEND / WITHDRAW, הסכום בשקלים).")
    up = st.file_uploader("בחר קובץ CSV", type="csv")
    replace = st.checkbox("מחק את כל העסקאות הקיימות לפני הייבוא")
    if up:
        rows = importer.parse(up.getvalue().decode("utf-8-sig"))
        st.write(f"נמצאו {len(rows)} שורות")
        st.dataframe(rows, use_container_width=True, hide_index=True)
        if st.button("ייבא עכשיו", type="primary"):
            with psycopg.connect(URL) as c:
                res = importer.import_rows(c, UID, rows, replace=replace)
            added, skipped = res["added"], res["skipped"]
            cash_added, cash_skipped = res["cash_added"], res["cash_skipped"]
            cash_rows = [r for r in rows if XC.is_cash_row(r)]
            st.cache_data.clear()
            st.success(f"נוספו {added} שורות, דולגו {skipped} שורות קיימות", icon=":material/check_circle:")
            if cash_rows:
                st.success(f"מזומן בבורסה: נוספו {cash_added} שורות, דולגו {cash_skipped} קיימות",
                           icon=":material/payments:")

with tab_del:
    with psycopg.connect(URL) as c:
        n = c.execute("SELECT count(*) FROM transactions WHERE user_id = %s", (UID,)).fetchone()[0]
        dups = dedupe.find_duplicates(c.execute(
            """SELECT t.id, t."timestamp"::date, a.symbol, t.type::text, t.amount, coalesce(t.notes,'')
               FROM transactions t JOIN assets a ON a.id = t.asset_id WHERE t.user_id = %s
               ORDER BY t."timestamp", t.id""", (UID,)).fetchall())
    st.subheader(":material/content_copy: כפילויות")
    if not dups:
        st.success("אין עסקאות כפולות", icon=":material/check_circle:")
    else:
        st.write(f"נמצאו **{len(dups)}** עסקאות שנרשמו פעמיים. השורה המפורטת יותר נשארת, הכפולה תימחק:")
        st.dataframe([{"תאריך": str(d[1]), "נכס": d[2], "סוג": d[3], "כמות": float(d[4]),
                       "נמחקת": d[5], "נשארת": k[5]} for d, k in dups],
                     use_container_width=True, hide_index=True)
        if st.button(f"מחק {len(dups)} כפילויות", type="primary", icon=":material/cleaning_services:"):
            with psycopg.connect(URL) as c:
                c.execute("DELETE FROM transactions WHERE user_id = %s AND id = ANY(%s)",
                          (UID, [d[0] for d, _ in dups]))
            st.cache_data.clear()
            st.success(f"נמחקו {len(dups)} כפילויות", icon=":material/check_circle:")
            st.rerun()
    st.divider()
    st.subheader(":material/delete_forever: מחיקת הכול")
    st.write(f"יש כרגע **{n}** עסקאות באפליקציה.")
    st.warning("המחיקה סופית ומוחקת את כל העסקאות. הנכסים (BTC וכו') נשארים.")
    ok = st.text_input("כדי לאשר, כתוב: מחק")
    if st.button("מחק את כל העסקאות", icon=":material/delete:"):
        if ok.strip() == "מחק":
            with psycopg.connect(URL) as c:
                c.execute("DELETE FROM transactions WHERE user_id = %s", (UID,))
            st.cache_data.clear()
            st.success("כל העסקאות נמחקו", icon=":material/check_circle:")
        else:
            st.error("לא אושר. כתוב בדיוק: מחק")
