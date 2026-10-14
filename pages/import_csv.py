import csv
import io
import os
from datetime import datetime, time
from decimal import Decimal as D

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

clean = lambda s: "".join(ch for ch in (s or "") if ch.isascii() and not ch.isspace())
URL, UID = clean(os.getenv("DATABASE_URL")), clean(os.getenv("USER_ID"))
if not (URL and UID):
    st.error("חסרים DATABASE_URL / USER_ID ב-Secrets")
    st.stop()

st.title(":material/upload_file: ייבוא ומחיקה")
tab_imp, tab_del = st.tabs(["ייבוא CSV", ":material/delete: מחיקה"])

DEFAULTS = {  # symbol -> (name, asset_class, Yahoo ticker) for assets created on import
    "BTC": ("Bitcoin", "crypto", "BTC-USD"),
    "CASH": ("Cash / account fees", "cash", "CASH"),
    "IBIT": ("iShares Bitcoin Trust", "equity", "IBIT"),
    "MSTR": ("Strategy (MicroStrategy)", "equity", "MSTR"),
    "MSTX": ("2x MSTR ETF", "equity", "MSTX"),
    "MTPLF": ("Metaplanet", "equity", "MTPLF"),
    "MPJPY": ("Metaplanet ADR", "equity", "MPJPY"),
    "ASST": ("Strive", "equity", "ASST"),
    "TSWCF": ("Smarter Web Company", "equity", "TSWCF"),
    "TQQQ": ("ProShares UltraPro QQQ", "equity", "TQQQ"),
}

with tab_imp:
    st.caption("עמודות: date, symbol, type, amount, price_per_unit, fee, currency, notes. שורות שכבר קיימות מדולגות. "
               "שורות עם symbol=ILS הן מזומן בבורסה (DEPOSIT / SPEND / WITHDRAW, הסכום בשקלים).")
    up = st.file_uploader("בחר קובץ CSV", type="csv")
    replace = st.checkbox("מחק את כל העסקאות הקיימות לפני הייבוא")
    if up:
        rows = list(csv.DictReader(io.StringIO(up.getvalue().decode("utf-8-sig"))))
        st.write(f"נמצאו {len(rows)} שורות")
        st.dataframe(rows, use_container_width=True, hide_index=True)
        if st.button("ייבא עכשיו", type="primary"):
            cash_rows = [r for r in rows if XC.is_cash_row(r)]
            rows = [r for r in rows if not XC.is_cash_row(r)]
            with psycopg.connect(URL) as c:
                cash_added, cash_skipped = XC.add_rows(c, cash_rows)
                if replace:
                    c.execute("DELETE FROM transactions WHERE user_id = %s", (UID,))
                assets = {s: i for i, s in c.execute("SELECT id, symbol FROM assets")}
                for sym in {r["symbol"].upper() for r in rows} - set(assets):
                    name, cls, api = DEFAULTS.get(sym, (sym, "equity", sym))
                    assets[sym] = c.execute("INSERT INTO assets(symbol,name,asset_class,api_id) "
                                            "VALUES (%s,%s,%s::asset_class,%s) RETURNING id",
                                            (sym, name, cls, api)).fetchone()[0]
                existing = {}   # (date, symbol, type, amount) -> [notes]; FEE rows also keyed by notes
                for d, s, t, a, n in c.execute(
                    """SELECT t."timestamp"::date, a.symbol, t.type::text, t.amount, coalesce(t.notes,'')
                       FROM transactions t JOIN assets a ON a.id = t.asset_id WHERE t.user_id = %s""", (UID,)):
                    existing.setdefault((str(d), s, t, D(a)) + ((n,) if t == "FEE" else ()), []).append(n)
                added = skipped = 0
                for r in rows:
                    sym, notes = r["symbol"].upper(), r["notes"] or ""
                    key = (r["date"], sym, r["type"], D(r["amount"])) + ((notes,) if r["type"] == "FEE" else ())
                    # same transaction already stored, even if its notes are worded differently
                    if dedupe.is_duplicate_of_existing(key, notes, existing):
                        skipped += 1
                        continue
                    existing.setdefault(key, []).append(notes)
                    c.execute("""INSERT INTO transactions(user_id,asset_id,type,amount,price_per_unit,fee,currency,"timestamp",notes)
                                 VALUES (%s,%s,%s::tx_type,%s,%s,%s,%s,%s,%s)""",
                              (UID, assets[sym], r["type"], D(r["amount"]), D(r["price_per_unit"]), D(r["fee"] or 0),
                               r["currency"] or "USD",
                               datetime.combine(datetime.fromisoformat(r["date"]).date(), time(12)), r["notes"]))
                    added += 1
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
