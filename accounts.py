"""Which account (section) a transaction belongs to.

There is no account column in the database; the account is read from the start of the
transaction's notes, e.g. "IBI גמל 150246 · ...". Anything without an account prefix
(the Bitcoin purchases and network fees) belongs to the cold wallet.
"""

COLD = "ארנק קר"

# notes prefix -> section name (display order)
PREFIXES = {
    "IBI השתלמות": "קרן השתלמות",
    "IBI גמל": "קופת גמל",
    "IBI מסחר": "מסחר IBI (סגור)",
}
# notes prefix to write for a manual transaction in each section
NOTE_PREFIX = {"קרן השתלמות": "IBI השתלמות 109318", "קופת גמל": "IBI גמל 150246",
               "מסחר IBI (סגור)": "IBI מסחר 169834", COLD: ""}
ORDER = ["קרן השתלמות", "קופת גמל", COLD, "מסחר IBI (סגור)"]


def account_of(notes: str) -> str:
    notes = notes or ""
    for prefix, name in PREFIXES.items():
        if notes.startswith(prefix):
            return name
    return COLD


def tag(account: str, notes: str) -> str:
    """Notes for a new transaction in `account` (adds the account prefix)."""
    prefix = NOTE_PREFIX.get(account, "")
    if not prefix:
        return notes
    return f"{prefix} · {notes}" if notes else prefix


# ---- where the bitcoin sits: cold wallet vs. still on an exchange ----
EXCHANGES = ("Coinbox", "Bits of Gold", "Horizon")
WITHDRAWAL = "Withdrawal to cold wallet"          # notes prefix of TRANSFER rows (exchange -> wallet)


def platform_of(notes: str) -> str | None:
    for name in EXCHANGES:
        if name in (notes or ""):
            return name
    return None


def btc_on_exchanges(txs, btc_ids) -> dict:
    """BTC still held on each exchange: bought there minus withdrawn to the wallet.

    Only exchanges with at least one recorded withdrawal are tracked; for the others
    (older purchases recorded before withdrawals were logged) the coins are assumed to
    have been moved to the cold wallet."""
    bought, withdrawn, tracked = {}, {}, set()
    for t in txs:
        if t.asset_id not in btc_ids:
            continue
        p = platform_of(t.notes)
        if not p:
            continue
        if t.type == "BUY":
            bought[p] = bought.get(p, 0.0) + float(t.amount)
        elif t.type == "TRANSFER" and (t.notes or "").startswith(WITHDRAWAL):
            withdrawn[p] = withdrawn.get(p, 0.0) + float(t.amount)
            tracked.add(p)
    return {p: max(bought.get(p, 0.0) - withdrawn.get(p, 0.0), 0.0) for p in tracked}
