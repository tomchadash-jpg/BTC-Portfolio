"""Import CSV rows into the database. Shared by the ייבוא ומחיקה page and scripts/import_inbox.py.

CSV columns: date,symbol,type,amount,price_per_unit,fee,currency,notes
Rows with symbol=ILS are shekel cash on an exchange (see exchange_cash.py); all other rows are
transactions. Rows already stored are skipped (dedupe.py), so importing the same file twice is safe.
No Streamlit here, so it also runs from GitHub Actions.
"""
import csv
import io
from datetime import datetime, time
from decimal import Decimal as D

import dedupe
import exchange_cash as XC

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


def parse(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


def import_rows(c, uid: str, rows: list[dict], replace: bool = False) -> dict:
    """Insert rows on an open psycopg connection. Returns counts:
    {"added", "skipped", "cash_added", "cash_skipped"}."""
    cash_rows = [r for r in rows if XC.is_cash_row(r)]
    rows = [r for r in rows if not XC.is_cash_row(r)]
    cash_added, cash_skipped = XC.add_rows(c, cash_rows)
    if replace:
        c.execute("DELETE FROM transactions WHERE user_id = %s", (uid,))
    assets = {s: i for i, s in c.execute("SELECT id, symbol FROM assets")}
    for sym in {r["symbol"].upper() for r in rows} - set(assets):
        name, cls, api = DEFAULTS.get(sym, (sym, "equity", sym))
        assets[sym] = c.execute("INSERT INTO assets(symbol,name,asset_class,api_id) "
                                "VALUES (%s,%s,%s::asset_class,%s) RETURNING id",
                                (sym, name, cls, api)).fetchone()[0]
    existing = {}   # (date, symbol, type, amount) -> [notes]; FEE rows also keyed by notes
    for d, s, t, a, n in c.execute(
        """SELECT t."timestamp"::date, a.symbol, t.type::text, t.amount, coalesce(t.notes,'')
           FROM transactions t JOIN assets a ON a.id = t.asset_id WHERE t.user_id = %s""", (uid,)):
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
                  (uid, assets[sym], r["type"], D(r["amount"]), D(r["price_per_unit"]), D(r["fee"] or 0),
                   r["currency"] or "USD",
                   datetime.combine(datetime.fromisoformat(r["date"]).date(), time(12)), r["notes"]))
        added += 1
    return {"added": added, "skipped": skipped, "cash_added": cash_added, "cash_skipped": cash_skipped}
