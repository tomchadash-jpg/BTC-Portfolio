import json
import os
import re
import urllib.request
from datetime import datetime, time, timezone
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
import streamlit as st

st.set_page_config(page_title="ארנק", page_icon="👛", layout="wide")

clean = lambda s: "".join(ch for ch in (s or "") if ch.isascii() and not ch.isspace())
URL, UID = clean(os.getenv("DATABASE_URL")), clean(os.getenv("USER_ID"))
if not (URL and UID):
    st.error("חסרים DATABASE_URL / USER_ID ב-Secrets")
    st.stop()

API = "https://mempool.space/api"
IL = ZoneInfo("Asia/Jerusalem")
ADDR_RE = re.compile(r"(?:bc1[a-z0-9]{20,90}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})")


@st.cache_data(ttl=120, show_spinner="בודק בבלוקצ'יין...")
def api(path: str):
    req = urllib.request.Request(API + path, headers={"User-Agent": "portfolio-app"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def address_txs(addr: str) -> list:
    out = api(f"/address/{addr}/txs")
    last = [t for t in out if t["status"]["confirmed"]]
    for _ in range(4):                                  # up to ~100 confirmed txs per address
        if len(last) < 25:
            break
        nxt = api(f"/address/{addr}/txs/chain/{last[-1]['txid']}")
        if not nxt:
            break
        out, last = out + nxt, nxt
    return out


def ledger():
    with psycopg.connect(URL) as c:
        notes = [r[0] for r in c.execute("SELECT notes FROM transactions WHERE user_id = %s AND notes LIKE %s",
                                         (UID, "Network fee (on-chain)%"))]
        qty = c.execute("""SELECT coalesce(sum(CASE WHEN t.type IN ('BUY','STAKING_REWARD') THEN t.amount
                                                    WHEN t.type = 'SELL' THEN -t.amount ELSE 0 END), 0)
                           FROM transactions t JOIN assets a ON a.id = t.asset_id
                           WHERE t.user_id = %s AND a.symbol = 'BTC'""", (UID,)).fetchone()[0]
    txids = {m.group(1) for n in notes if (m := re.search(r"tx (\w+)", n))}
    return txids, float(qty)


st.title("👛 מעקב ארנק")
st.caption("קריאה בלבד מ-mempool.space. לא נדרשים מפתחות או סיסמאות.")

known_txids, ledger_qty = ledger()

# --- which addresses are yours: inputs of the on-chain txs already in the ledger + extra addresses you add ---
own, suggestions = set(), set()
for t in known_txids:
    try:
        tx = api(f"/tx/{t}")
        own |= {v["prevout"]["scriptpubkey_address"] for v in tx["vin"] if v.get("prevout")}
    except Exception:
        continue
extra_text = os.getenv("WALLET_ADDRESSES", "") + " " + st.text_area(
    "כתובות נוספות שלך (אופציונלי, לשימוש חד-פעמי; לקבע הוסף ל-Secrets כ-WALLET_ADDRESSES)", "")
own |= set(ADDR_RE.findall(extra_text))
for t in known_txids:
    try:
        tx, outs = api(f"/tx/{t}"), api(f"/tx/{t}/outspends")
        for o, sp in zip(tx["vout"], outs):
            a = o.get("scriptpubkey_address")
            if a and a not in own and not sp.get("spent"):
                suggestions.add(a)
    except Exception:
        continue
if not own:
    st.info("לא נמצאו כתובות. הוסף כתובת ארנק בתיבה למעלה, או ייבא קודם את עמלות ה-on-chain.")
    st.stop()
if suggestions:
    st.info("כתובות שייתכן שהן שלך (פלטים שלא נוצלו מהטרנזקציות שלך):\n\n"
            + "\n".join(f"`{a}`" for a in sorted(suggestions)))
    if st.checkbox("הכתובות האלה שלי, כלול אותן במעקב"):
        own |= suggestions

# --- on-chain activity ---
try:
    txs = {}
    for a in own:
        for t in address_txs(a):
            txs[t["txid"]] = t
    balance = 0
    for a in own:
        s = api(f"/address/{a}")
        balance += (s["chain_stats"]["funded_txo_sum"] - s["chain_stats"]["spent_txo_sum"]
                    + s["mempool_stats"]["funded_txo_sum"] - s["mempool_stats"]["spent_txo_sum"])
except Exception as e:
    st.error(f"לא הצלחתי לקרוא מ-mempool.space: {e}")
    st.stop()

rows, new_fees = [], []
for t in txs.values():
    ins = [(v["prevout"]["scriptpubkey_address"], v["prevout"]["value"]) for v in t["vin"] if v.get("prevout")]
    outs = [(o.get("scriptpubkey_address"), o["value"]) for o in t["vout"]]
    own_in = sum(v for a, v in ins if a in own)
    own_out = sum(v for a, v in outs if a in own)
    all_own_in = bool(ins) and all(a in own for a, _ in ins)
    if own_in == 0:
        kind = "נכנס"
    elif all(a in own for a, _ in outs):
        kind = "העברה פנימית"
    else:
        kind = "יוצא"
    bt = t["status"].get("block_time")
    when = datetime.fromtimestamp(bt, timezone.utc).astimezone(IL) if bt else None
    fee = t["fee"] if all_own_in else None
    rows.append({"date": when, "type": kind, "net_btc": (own_out - own_in) / 1e8, "fee_sat": fee,
                 "status": "מאושר" if bt else "ממתין", "link": f"{API.replace('/api', '')}/tx/{t['txid']}"})
    if fee and bt and t["txid"] not in known_txids:
        new_fees.append({"txid": t["txid"], "date": when.date(), "sat": fee})

c = st.columns(3)
c[0].metric("יתרה בארנק (בלוקצ'יין)", f"{balance / 1e8:.8f} BTC")
c[1].metric("אחזקה לפי הספר", f"{ledger_qty:.8f} BTC")
c[2].metric("הפרש", f"{(ledger_qty - balance / 1e8):+.8f} BTC")
st.caption("ההפרש הוא בעיקר BTC שנשאר בבורסות (למשל Coinbox) ועמלות שעוד לא נרשמו. מחיר קנייה אי אפשר לדעת מהבלוקצ'יין, ולכן הוא נשאר מהבורסות.")

df = pd.DataFrame(rows).sort_values("date", ascending=False, na_position="first")
st.subheader("תנועות בארנק")
st.dataframe(df, use_container_width=True, hide_index=True, column_config={
    "date": st.column_config.DatetimeColumn("תאריך", format="DD/MM/YYYY HH:mm"), "type": "סוג",
    "net_btc": st.column_config.NumberColumn("שינוי BTC", format="%.8f"),
    "fee_sat": st.column_config.NumberColumn("עמלה (sat)"), "status": "סטטוס",
    "link": st.column_config.LinkColumn("טרנזקציה", display_text="פתח")})

st.subheader("עמלות רשת חדשות")
if not new_fees:
    st.success("אין עמלות רשת שלא נרשמו בספר ✅")
else:
    nf = pd.DataFrame(new_fees)
    st.dataframe(nf, use_container_width=True, hide_index=True)
    st.write(f"{len(nf)} עמלות, סה״כ {int(nf.sat.sum()):,} sat")
    pw = os.getenv("APP_PASSWORD")
    if pw and st.text_input("סיסמה להוספה", type="password") != pw:
        st.stop()
    if st.button("➕ הוסף לספר"):
        with psycopg.connect(URL) as conn:
            aid = conn.execute("SELECT id FROM assets WHERE symbol = 'BTC'").fetchone()
            if not aid:
                st.error("לא נמצא נכס BTC")
                st.stop()
            for r in new_fees:
                conn.execute("""INSERT INTO transactions(user_id,asset_id,type,amount,price_per_unit,fee,currency,"timestamp",notes)
                                VALUES (%s,%s,'SELL'::tx_type,%s,0,0,'USD',%s,%s)""",
                             (UID, aid[0], D(r["sat"]) / D(10 ** 8), datetime.combine(r["date"], time(12)),
                              f"Network fee (on-chain) {r['sat']} sat, tx {r['txid']}"))
        st.cache_data.clear()
        st.success(f"נוספו {len(new_fees)} עמלות ✅")
