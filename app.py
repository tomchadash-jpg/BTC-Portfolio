"""Entry point (Streamlit Cloud runs this file).

Registers the pages; the menu itself is the ☰ button at the top right (menu.py),
so Streamlit's own left sidebar menu is hidden. Each page sets its own page config,
right-to-left styling and password check.
"""
import importlib
import sys

import streamlit as st

# Streamlit Cloud re-runs the pages after an update but can keep old copies of our own
# modules in memory (styles, menu, ...). Reload them on every run so changes always show.
for _name in ("ui", "menu", "gate", "accounts", "engine", "insights", "powerlaw", "exchange_cash"):
    if _name in sys.modules:
        importlib.reload(sys.modules[_name])

import runpy
from pathlib import Path

import menu

DASHBOARD = str(Path(__file__).resolve().parent / "dashboard.py")
# (title, icon, url) - every section of the dashboard is its own page in the ☰ menu
SECTIONS = [("שווי היסטורי", ":material/show_chart:", "history"),
            ("השוואה למדדים", ":material/compare_arrows:", "benchmarks"),
            ("יעדים", ":material/flag:", "goals"),
            ("נכסים", ":material/account_balance_wallet:", "assets"),
            ("תובנות", ":material/lightbulb:", "insights"),
            ("Power Law", ":material/stacked_line_chart:", "powerlaw")]


def section_page(i: int):
    def run():
        st.session_state["section_idx"] = i            # dashboard.py draws only this section
        runpy.run_path(DASHBOARD, run_name="__main__")
    run.__name__ = f"section_{SECTIONS[i][2]}"         # pages are told apart by their function name
    return run


pages = [st.Page(section_page(i), title=t, icon=ic, url_path=u, default=(i == 0))
         for i, (t, ic, u) in enumerate(SECTIONS)]
pages += [st.Page("pages/import_csv.py", title="ייבוא ומחיקה", icon=":material/upload_file:"),
          st.Page("pages/fees.py", title="עמלות", icon=":material/receipt_long:"),
          st.Page("pages/export.py", title="ייצוא נתונים", icon=":material/download:")]
menu.NAV = pages                                       # the ☰ menu links to these
menu.TOOLS_FROM = len(SECTIONS)                        # pages after the sections go under "כלים"
pg = st.navigation(pages, position="hidden")
pg.run()
