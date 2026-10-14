"""Shekels sitting on an exchange (Coinbox) waiting to buy bitcoin.

A small ledger in its own table: every deposit adds, every purchase paid from the balance
subtracts, and a manual correction (ADJUST) sets the balance to what the exchange shows.
The balance is the sum of all rows. Rows come from the import CSV (symbol "ILS"), written by
the daily e-mail scan, so the same row imported twice is skipped.

CSV row format (same columns as the other import rows):
    date,symbol,type,amount,price_per_unit,fee,currency,notes
    2026-10-06,ILS,DEPOSIT,80000,1,0,ILS,Coinbox · הפקדה
    2026-10-14,ILS,SPEND,137.50,1,0,ILS,Coinbox · רכישה חוזרת 10:00
"""
import os
from datetime import date
from decimal import Decimal as D

import accounts as A

SYMBOL = "ILS"
KINDS = {"DEPOSIT": 1, "SPEND": -1, "WITHDRAW": -1}     # sign of each kind; ADJUST rows are stored signed
DEFAULT_EXCHANGE = "Coinbox"

DDL = """CREATE TABLE IF NOT EXISTS exchange_cash (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     UUID          NOT NULL,
    exchange    TEXT          NOT NULL,
    date        DATE          NOT NULL,
    kind        TEXT          NOT NULL,      -- DEPOSIT / SPEND / WITHDRAW / ADJUST
    amount_ils  NUMERIC(14,2) NOT NULL,      -- signed: + money in, - money out
    note        TEXT          NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    UNIQUE (user_id, exchange, date, kind, amount_ils, note))"""

clean = lambda s: "".join(ch for ch in (s or "") if ch.isascii() and not ch.isspace())


def _conn():
    import psycopg
    c = psycopg.connect(clean(os.getenv("DATABASE_URL")))
    c.execute(DDL)
    return c


def _uid():
    return clean(os.getenv("USER_ID"))


def is_cash_row(r: dict) -> bool:
    return (r.get("symbol") or "").strip().upper() == SYMBOL


def add_rows(c, rows) -> tuple[int, int]:
    """Insert ILS cash rows from the import CSV on an open connection; returns (added, skipped)."""
    c.execute(DDL)
    added = skipped = 0
    for r in rows:
        kind = (r["type"] or "").strip().upper()
        if kind not in KINDS:
            raise ValueError(f"סוג לא מוכר בשורת מזומן: {r['type']} (צריך DEPOSIT / SPEND / WITHDRAW)")
        amt = D(r["amount"]) * KINDS[kind]
        notes = (r.get("notes") or "").strip()
        ex = A.platform_of(notes) or DEFAULT_EXCHANGE
        cur = c.execute("""INSERT INTO exchange_cash(user_id, exchange, date, kind, amount_ils, note)
                           VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (_uid(), ex, date.fromisoformat(r["date"]), kind, amt, notes))
        if cur.rowcount:
            added += 1
        else:
            skipped += 1
    return added, skipped


def balances() -> dict:
    """{exchange: (balance_ils, last_date)} for exchanges with any rows."""
    with _conn() as c:
        rows = c.execute("""SELECT exchange, sum(amount_ils), max(date) FROM exchange_cash
                            WHERE user_id=%s GROUP BY exchange ORDER BY exchange""", (_uid(),)).fetchall()
    return {ex: (float(b), d) for ex, b, d in rows}


def ledger():
    """All rows, oldest first: (date, exchange, kind, amount_ils, note)."""
    with _conn() as c:
        return c.execute("""SELECT date, exchange, kind, amount_ils, note FROM exchange_cash
                            WHERE user_id=%s ORDER BY date, id""", (_uid(),)).fetchall()


def set_balance(exchange: str, target: float):
    """Correct the balance to what the exchange shows, by adding the difference as an ADJUST row."""
    with _conn() as c:
        cur = c.execute("SELECT coalesce(sum(amount_ils),0) FROM exchange_cash WHERE user_id=%s AND exchange=%s",
                        (_uid(), exchange)).fetchone()[0]
        diff = D(str(round(target, 2))) - cur
        if diff:
            c.execute("""INSERT INTO exchange_cash(user_id, exchange, date, kind, amount_ils, note)
                         VALUES (%s,%s,%s,'ADJUST',%s,%s) ON CONFLICT DO NOTHING""",
                      (_uid(), exchange, date.today(), diff, f"תיקון ידני ליתרה {target:,.2f}"))
