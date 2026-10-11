"""Top-right ☰ menu (instead of Streamlit's left sidebar).

The pages are registered in app.py with st.navigation(position="hidden"); this menu
links to them with Hebrew names. Pages can add their own controls under the links by
writing into the returned popover.
"""
import streamlit as st

PAGES = [("dashboard.py", "התיק שלי", "📈"),
         ("pages/add_transaction.py", "הוספת עסקה", "➕"),
         ("pages/import_csv.py", "ייבוא ומחיקה", "📥"),
         ("pages/fees.py", "עמלות", "💸")]


def top(overlap: bool = False):
    """Draw the ☰ button at the top right and return the open menu as a container.

    overlap=True pulls the next element (the page title) up into the same row; use it
    when that title is aligned to the left, so the two don't collide."""
    key = "topmenu_overlap" if overlap else "topmenu"
    css = (f".st-key-{key} {{ display: flex; align-items: flex-start; direction: rtl; position: relative; z-index: 5;"
           f"{' margin-bottom: -3.4rem;' if overlap else ''} }}"
           f".st-key-{key} [data-testid='stPopover'] {{ width: fit-content !important; margin-left: auto !important;"
           f" margin-right: 0 !important; }}"
           # a plain, big ☰: no border/background, no little arrow next to it
           f".st-key-{key} button {{ border: none !important; background: transparent !important;"
           f" box-shadow: none !important; padding: 0 .2rem !important; min-height: 0 !important;"
           f" width: auto !important; }}"
           f".st-key-{key} button p {{ font-size: 2.2rem !important; line-height: 1 !important; margin: 0 !important; }}"
           f".st-key-{key} button svg, .st-key-{key} button [data-testid='stIconMaterial'] {{ display: none !important; }}")
    (st.html if hasattr(st, "html") else lambda c: st.markdown(c, unsafe_allow_html=True))(f"<style>{css}</style>")
    with st.container(key=key):
        pop = st.popover("☰")
    with pop:
        for path, title, icon in PAGES:
            st.page_link(path, label=title, icon=icon)
    return pop
