"""Add transactions straight to the database (run by the "Import rows" GitHub Actions workflow).

The rows arrive as CSV text in the IMPORT_ROWS env var - the same columns the
"ייבוא ומחיקה" page accepts: date,symbol,type,amount,price_per_unit,fee,currency,notes.
Rows already in the database (same date, symbol, type, amount, notes) are skipped, exactly
like the page does, so re-sending a file is harmless. Nothing about the rows is printed:
the repo is public and so are its Actions logs - only the added/skipped counts are.
"""
import csv
import io
import os
import sys
from datetime import datetime, time
from decimal import Decimal as D

import psycopg

clean = lambda s: "".join(ch for ch in (s or "") if ch.isascii() and not ch.isspace())
URL, UID = clean(os.getenv("DATABASE_URL")), clean(os.getenv("USER_ID"))
COLS = ["date", "symbol", "type", "amount", "price_per_unit", "fee", "currency", "notes"]
TYPES = {"BUY", "SELL", "TRANSFER", "STAKING_REWARD", "DIVIDEND", "FEE"}
DEFAULTS = {"BTC": ("Bitcoin", "crypto", "BTC-USD")}  # other symbols: add them via the page


def parse(text: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿").strip())))
    if not rows:
        sys.exit("No rows in IMPORT_ROWS")
    for i, r in enumerate(rows, 1):
        missing = [c for c in COLS if c not in r]
        if missing:
            sys.exit(f"Row {i}: missing columns {missing}")
        r["symbol"] = r["symbol"].strip().upper()
        r["type"] = r["type"].strip().upper()
        if r["type"] not in TYPES:
            sys.exit(f"Row {i}: unknown type")
        datetime.fromisoformat(r["date"])                       # raises on a bad date
        D(r["amount"]), D(r["price_per_unit"]), D(r["fee"] or 0)  # raises on a bad number
    return rows


def main() -> None:
    if not (URL and UID):
        sys.exit("Missing DATABASE_URL / USER_ID repository secrets")
    rows = parse(os.environ.get("IMPORT_ROWS", ""))
    with psycopg.connect(URL) as c:
        assets = {s: i for i, s in c.execute("SELECT id, symbol FROM assets")}
        for sym in {r["symbol"] for r in rows} - set(assets):
            if sym not in DEFAULTS:
                sys.exit(f"Unknown asset {sym} - import it once through the app page")
            name, cls, api = DEFAULTS[sym]
            assets[sym] = c.execute("INSERT INTO assets(symbol,name,asset_class,api_id) "
                                    "VALUES (%s,%s,%s::asset_class,%s) RETURNING id",
                                    (sym, name, cls, api)).fetchone()[0]
        existing = {(str(d), s, t, D(a), n) for d, s, t, a, n in c.execute(
            """SELECT t."timestamp"::date, a.symbol, t.type::text, t.amount, coalesce(t.notes,'')
               FROM transactions t JOIN assets a ON a.id = t.asset_id WHERE t.user_id = %s""", (UID,))}
        added = skipped = 0
        for r in rows:
            key = (r["date"], r["symbol"], r["type"], D(r["amount"]), r["notes"])
            if key in existing:
                skipped += 1
                continue
            c.execute("""INSERT INTO transactions(user_id,asset_id,type,amount,price_per_unit,fee,currency,"timestamp",notes)
                         VALUES (%s,%s,%s::tx_type,%s,%s,%s,%s,%s,%s)""",
                      (UID, assets[r["symbol"]], r["type"], D(r["amount"]), D(r["price_per_unit"]),
                       D(r["fee"] or 0), r["currency"] or "USD",
                       datetime.combine(datetime.fromisoformat(r["date"]).date(), time(12)), r["notes"]))
            existing.add(key)
            added += 1
    print(f"Added {added} rows, skipped {skipped} existing rows")


if __name__ == "__main__":
    main()
