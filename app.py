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
for _name in ("ui", "menu", "gate", "accounts", "engine", "insights", "powerlaw"):
    if _name in sys.modules:
        importlib.reload(sys.modules[_name])

pg = st.navigation([
    st.Page("dashboard.py", title="התיק שלי", icon="📈", default=True),
    st.Page("pages/add_transaction.py", title="הוספת עסקה", icon="➕"),
    st.Page("pages/import_csv.py", title="ייבוא ומחיקה", icon="📥"),
    st.Page("pages/fees.py", title="עמלות", icon="💸"),
], position="hidden")
pg.run()
