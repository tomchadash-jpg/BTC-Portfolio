"""Daily Power Law check (run by GitHub Actions). Opens a GitHub issue - which reaches the
phone as a GitHub notification - when BTC enters the bottom two deciles, comments when it
moves between them, and closes the issue when BTC leaves the zone."""
import json
import os
import sys
import urllib.request
from pathlib import Path

import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import powerlaw as PL  # noqa: E402

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
OWNER = REPO.split("/")[0]
LABEL = "powerlaw-alert"
ZONE = 2                                        # bottom two deciles


def gh(method, path, body=None):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r) if r.status != 204 else None


h = yf.Ticker("BTC-USD").history(period="max", auto_adjust=True)["Close"]
h.index = h.index.tz_localize(None).normalize()
s = PL.status(h, 10)
band, price, fair = s["band"], s["price"], s["trend"]
edge = s["edges_today"][ZONE - 1]
print(f"BTC ${price:,.0f} | trend ${fair:,.0f} | decile {band}/10 | percentile {s['percentile']:.0f}")

try:
    gh("POST", "/labels", {"name": LABEL, "color": "2e9e5b"})
except Exception:
    pass                                         # label exists
issues = gh("GET", f"/issues?state=open&labels={LABEL}")
open_issue = issues[0] if issues else None
title = f"🟢 Power Law: BTC בעשירון {band} – אזור הגדלת DCA"
details = (f"מחיר: **${price:,.0f}** · קו המגמה: ${fair:,.0f} ({price / fair:.2f}×)\n\n"
           f"עשירון **{band} מתוך 10** · אחוזון {s['percentile']:.0f} מכל ההיסטוריה\n\n"
           f"גבול אזור ההגדלה היום (שני העשירונים התחתונים): ${edge:,.0f}")

if band <= ZONE:
    if not open_issue:
        gh("POST", "/issues", {"title": title, "labels": [LABEL],
                               "body": f"@{OWNER} הביטקוין נכנס לאזור הגדלת ה-DCA.\n\n{details}"})
        print("opened alert")
    elif open_issue["title"] != title:
        gh("POST", f"/issues/{open_issue['number']}/comments", {"body": f"@{OWNER} שינוי עשירון.\n\n{details}"})
        gh("PATCH", f"/issues/{open_issue['number']}", {"title": title})
        print("updated alert")
elif open_issue:
    gh("POST", f"/issues/{open_issue['number']}/comments",
       {"body": f"@{OWNER} הביטקוין יצא מאזור ההגדלה (עשירון {band}).\n\n{details}"})
    gh("PATCH", f"/issues/{open_issue['number']}", {"state": "closed"})
    print("closed alert")
