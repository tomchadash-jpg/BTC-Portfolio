"""Export the data: an Excel workbook (transactions, holdings, bank balance, assets) and a CSV in the
same format the import page reads, so an export can be re-imported as a backup.

Filters: sections (accounts) and a date range. Amounts are kept in each transaction's own currency
(no FX conversion), so the numbers match the original records exactly.
"""
import csv
import io
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
import streamlit as st

st.set_page_config(page_title="ייצוא נתונים", page_icon=":material/download:")

import ui
ui.rtl()
import gate
gate.require_login()  # nothing below runs without the password
import menu
menu.top()
import accounts as A

TZ = ZoneInfo("Asia/Jerusalem")
clean = lambda s: "".join(ch for ch in (s or "") if ch.isascii() and not ch.isspace())
URL, UID = clean(os.getenv("DATABASE_URL")), clean(os.getenv("USER_ID"))
if not (URL and UID):
    st.error("חסרים DATABASE_URL / USER_ID ב-Secrets")
    st.stop()

TYPE_HE = {"BUY": "קנייה", "SELL": "מכירה", "TRANSFER": "העברה", "STAKING_REWARD": "תגמול",
           "DIVIDEND": "דיבידנד", "FEE": "עמלה"}
IMPORT_COLS = ["date", "symbol", "type", "amount", "price_per_unit", "fee", "currency", "notes"]


@st.cache_data(ttl=60, show_spinner="טוען נתונים...")
def load():
    with psycopg.connect(URL) as c:
        tx = c.execute("""SELECT t."timestamp", a.symbol, a.name, t.type::text, t.amount, t.price_per_unit,
                                 t.fee, t.currency, coalesce(t.notes,'')
                          FROM transactions t JOIN assets a ON a.id = t.asset_id
                          WHERE t.user_id = %s ORDER BY t."timestamp", t.id""", (UID,)).fetchall()
        assets = c.execute("SELECT symbol, name, asset_class::text, api_id, base_currency FROM assets "
                           "ORDER BY symbol").fetchall()
        try:
            bank = c.execute("SELECT updated_at, balance_ils FROM bank_balances WHERE user_id = %s "
                             "ORDER BY updated_at", (UID,)).fetchall()
        except psycopg.errors.UndefinedTable:
            bank = []
    df = pd.DataFrame(tx, columns=["ts", "symbol", "name", "type", "amount", "price", "fee", "currency", "notes"])
    for col in ("amount", "price", "fee"):
        df[col] = df[col].astype(float)
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(TZ).dt.tz_localize(None)
    df["date"] = df["ts"].dt.date
    df["section"] = df["notes"].map(A.account_of)
    return df, assets, bank


df, assets, bank = load()

st.title(":material/download: ייצוא נתונים")
if df.empty:
    st.info("אין עסקאות לייצוא עדיין.")
    st.stop()

# ---------------- filters ----------------
present = [s for s in A.ORDER if s in set(df["section"])]
sections = st.multiselect("חלקים בתיק", present, default=present)
first, last = df["date"].min(), max(df["date"].max(), date.today())
c1, c2 = st.columns(2)
d_from = c1.date_input("מתאריך", first, min_value=first, max_value=last, format="DD/MM/YYYY")
d_to = c2.date_input("עד תאריך", last, min_value=first, max_value=last, format="DD/MM/YYYY")

sel = df[df["section"].isin(sections) & (df["date"] >= d_from) & (df["date"] <= d_to)]
st.write(f"**{len(sel)}** עסקאות נבחרו מתוך {len(df)}")
if sel.empty:
    st.warning("אין עסקאות בבחירה הזו.")
    st.stop()


# ---------------- Excel workbook ----------------
def holdings(d: pd.DataFrame) -> pd.DataFrame:
    """Per section + asset + currency: units held and money in/out, in the original currency."""
    rows = []
    for (sec, sym, cur), g in d.groupby(["section", "symbol", "currency"], sort=False):
        sign = g["type"].map({"BUY": 1, "STAKING_REWARD": 1, "SELL": -1}).fillna(0)
        gross = g["amount"] * g["price"]
        bought = g["type"] == "BUY"
        sold = g["type"] == "SELL"
        qty_bought = g.loc[bought, "amount"].sum()
        rows.append({
            "חלק": sec, "סימול": sym, "מטבע": cur,
            "יחידות נטו": (g["amount"] * sign).sum(),
            "סה\"כ קניות (כולל עמלות)": (gross[bought] + g.loc[bought, "fee"]).sum(),
            "סה\"כ מכירות (נטו)": (gross[sold] - g.loc[sold, "fee"]).sum(),
            "עמלות": g["fee"].sum(),
            "מחיר קנייה ממוצע": ((gross[bought] + g.loc[bought, "fee"]).sum() / qty_bought) if qty_bought else None,
            "מספר עסקאות": len(g),
        })
    order = {s: i for i, s in enumerate(A.ORDER)}
    return pd.DataFrame(rows).sort_values(["חלק", "סימול"], key=lambda s: s.map(order) if s.name == "חלק" else s)


def workbook(d: pd.DataFrame) -> bytes:
    tx = pd.DataFrame({
        "תאריך": d["ts"], "חלק": d["section"], "סימול": d["symbol"], "נכס": d["name"],
        "סוג": d["type"].map(TYPE_HE).fillna(d["type"]), "כמות": d["amount"], "מחיר ליחידה": d["price"],
        "עמלה": d["fee"], "מטבע": d["currency"],
        "סכום (כמות × מחיר)": d["amount"] * d["price"], "הערות": d["notes"],
    })
    bank_df = pd.DataFrame([(pd.Timestamp(t).tz_convert(TZ).tz_localize(None), float(b)) for t, b in bank],
                           columns=["עודכן", "יתרה (₪)"])
    assets_df = pd.DataFrame(assets, columns=["סימול", "שם", "סוג נכס", "טיקר", "מטבע בסיס"])
    sheets = {"עסקאות": tx, "אחזקות": holdings(d), "יתרת עו\"ש": bank_df, "נכסים": assets_df}

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, frame in sheets.items():
            frame.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.sheet_view.rightToLeft = True
            ws.freeze_panes = "A2"
            for col in ws.columns:
                header = str(col[0].value or "")
                width = max([len(header)] + [len(str(c.value)) for c in col[1:200] if c.value is not None])
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 45)
                for c in col[1:]:
                    if isinstance(c.value, datetime):
                        c.number_format = "dd/mm/yyyy hh:mm"
                    elif isinstance(c.value, float):
                        c.number_format = "#,##0.########" if header in ("כמות", "יחידות נטו") else "#,##0.00"
    return buf.getvalue()


# ---------------- CSV in the import format ----------------
def import_csv(d: pd.DataFrame) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(IMPORT_COLS)
    num = lambda x: format(x, ".10f").rstrip("0").rstrip(".") or "0"
    for r in d.itertuples():
        w.writerow([r.date.isoformat(), r.symbol, r.type, num(r.amount), num(r.price), num(r.fee), r.currency, r.notes])
    return ("﻿" + out.getvalue()).encode("utf-8")   # BOM so Excel/Numbers show Hebrew correctly


stamp = date.today().strftime("%Y-%m-%d")
st.download_button("הורד קובץ Excel", workbook(sel), file_name=f"portfolio-{stamp}.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   icon=":material/table_view:", type="primary", use_container_width=True)
st.caption("ארבעה גיליונות: עסקאות, אחזקות לפי חלק בתיק, היסטוריית יתרת העו\"ש, ורשימת הנכסים. "
           "הסכומים במטבע המקורי של כל עסקה.")

st.download_button("הורד CSV לגיבוי", import_csv(sel), file_name=f"portfolio-backup-{stamp}.csv",
                   mime="text/csv", icon=":material/backup:", use_container_width=True)
st.caption("באותו פורמט של מסך הייבוא, כך שאפשר לטעון אותו בחזרה לאפליקציה.")

st.info("באייפון הקובץ נפתח בתצוגה מקדימה: לחץ על כפתור השיתוף ← \"שמור בקבצים\" או שלח במייל.",
        icon=":material/smartphone:")
