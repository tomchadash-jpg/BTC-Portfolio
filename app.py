"""Entry point (Streamlit Cloud runs this file).

Builds the side menu with Hebrew page names; each page sets its own page config,
right-to-left styling and password check.
"""
import streamlit as st

pg = st.navigation([
    st.Page("dashboard.py", title="התיק שלי", icon="📈", default=True),
    st.Page("pages/add_transaction.py", title="הוספת עסקה", icon="➕"),
    st.Page("pages/import_csv.py", title="ייבוא ומחיקה", icon="📥"),
    st.Page("pages/fees.py", title="עמלות", icon="💸"),
])
pg.run()
