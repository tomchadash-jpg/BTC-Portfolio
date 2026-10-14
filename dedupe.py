"""Find transactions that were recorded twice.

The daily e-mail scan sometimes writes the same transaction twice with different notes
(e.g. "Network fee (on-chain) 110 sat" and, later, the same fee with ", tx <hash>" added;
or a Coinbox recurring buy once from its receipt and once as "רכישה חוזרת"). The import's
exact-match check misses these because the notes differ.

Rule: rows with the same date, symbol, type and amount are the same transaction unless BOTH
carry their own, different identifier (an on-chain tx hash or an exchange order id) — e.g.
two Coinbox withdrawal fees on the same day with ids 5a3abfde and 28b95886 are kept.
Of each duplicate group the row with the most detail (an identifier, then longest notes) is kept.
"""
import re
from decimal import Decimal as D

_ID = re.compile(r"(?<![.\d])\b([0-9a-f]{8,64})\b", re.I)   # not the digits of a decimal like 0.00000752


def ident(notes: str) -> str | None:
    """The transaction's own id in its notes (tx hash / order id), or None."""
    m = _ID.search(notes or "")
    return m.group(1).lower() if m else None


def _rank(notes: str):
    return (ident(notes) is not None, len(notes or ""))


def same_tx(notes_a: str, notes_b: str) -> bool:
    """Two rows with the same date/symbol/type/amount: are they one transaction?"""
    a, b = ident(notes_a), ident(notes_b)
    return not (a and b and a != b)


def find_duplicates(rows):
    """rows: iterable of (id, date, symbol, type, amount, notes). Returns [(drop_row, kept_row)]."""
    groups = {}
    for r in rows:
        if r[3] == "FEE":          # account-fee rows have amount 0; never merge them
            continue
        groups.setdefault((str(r[1]), r[2], r[3], D(str(r[4]))), []).append(r)
    out = []
    for g in groups.values():
        kept = []
        for r in sorted(g, key=lambda r: _rank(r[5]), reverse=True):
            match = next((k for k in kept if same_tx(k[5], r[5])), None)
            if match:
                out.append((r, match))
            else:
                kept.append(r)
    return out


def is_duplicate_of_existing(key, notes, existing_notes_by_key) -> bool:
    """For the importer: does an incoming row repeat a transaction already stored?"""
    return any(same_tx(n, notes) for n in existing_notes_by_key.get(key, ()))
