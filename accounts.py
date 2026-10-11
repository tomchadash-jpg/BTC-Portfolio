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
