"""Import the CSV files the daily e-mail scan leaves in the private inbox repo.

Run by the inbox repo's GitHub Action:  python app/scripts/import_inbox.py inbox/*.csv
Needs DATABASE_URL and USER_ID in the environment (repo secrets). Each file is imported with
the same rules as the ייבוא ומחיקה page, so rows already stored are skipped and re-running is safe.
Prints one summary line per file; exits non-zero if any file fails (the file then stays in inbox/).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the app's modules

import psycopg

import importer

clean = lambda s: "".join(ch for ch in (s or "") if ch.isascii() and not ch.isspace())
URL, UID = clean(os.getenv("DATABASE_URL")), clean(os.getenv("USER_ID"))
if not (URL and UID):
    sys.exit("DATABASE_URL / USER_ID missing")

failed = []
for path in sys.argv[1:]:
    p = Path(path)
    if not p.is_file():
        continue
    try:
        with psycopg.connect(URL) as c:
            res = importer.import_rows(c, UID, importer.parse(p.read_text(encoding="utf-8")))
        print(f"{p.name}: added {res['added']}, skipped {res['skipped']}, "
              f"cash added {res['cash_added']}, cash skipped {res['cash_skipped']}")
        with open(os.getenv("IMPORTED_LIST", "imported.txt"), "a") as f:
            f.write(str(p) + "\n")
    except Exception as e:
        print(f"{p.name}: FAILED: {e}", file=sys.stderr)
        failed.append(p.name)
sys.exit(1 if failed else 0)
