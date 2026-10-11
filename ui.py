"""Right-to-left layout for the Hebrew UI. Call `rtl()` right after st.set_page_config."""
import streamlit as st

_CSS = """
<style>
/* Rubik: a rounded, friendly font with proper Hebrew + Latin + tabular digits */
@import url('https://fonts.googleapis.com/css2?family=Rubik:wght@400;500;700&display=swap');
.stApp, .stApp p, .stApp li, .stApp label, .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5,
.stApp input, .stApp textarea, .stApp button, .stApp [data-testid="stMarkdownContainer"],
.stApp [data-testid="stMetricValue"], .stApp [data-testid="stMetricLabel"],
.stApp [data-baseweb="tab"], .stApp .ins-card {
    font-family: 'Rubik', -apple-system, 'Segoe UI', sans-serif !important; }
.stApp p, .stApp li { line-height: 1.6; }
.stApp h1, .stApp h2, .stApp h3 { font-weight: 700; letter-spacing: -.01em; }
.stApp [data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; font-weight: 500; }

/* ☰ menu panel (rendered outside the page, so it needs its own rules):
   right-to-left, a fixed width instead of the whole screen, and anchored to the right edge */
[data-testid="stPopoverBody"] { direction: rtl !important; text-align: right !important;
    width: min(300px, 82vw) !important; min-width: 0 !important; max-width: 82vw !important; }
[data-testid="stPopoverBody"] * { text-align: right; }
[data-testid="stPopoverBody"] [data-baseweb="slider"] { direction: ltr; }
/* bigger, easier-to-tap page links in the ☰ menu */
[data-testid="stPopoverBody"] [data-testid^="stPageLink"] a, [data-testid="stPopoverBody"] a[data-testid^="stPageLink"] {
    padding: .45rem .5rem !important; }
[data-testid="stPopoverBody"] [data-testid^="stPageLink"] p, [data-testid="stPopoverBody"] [data-testid^="stPageLink"] span,
[data-testid="stPopoverBody"] a[data-testid^="stPageLink"] p, [data-testid="stPopoverBody"] a[data-testid^="stPageLink"] span {
    font-size: 1.2rem !important; line-height: 1.5 !important; }
[data-testid="stPopoverBody"] { position: fixed !important; inset: 3.6rem .5rem auto auto !important;
    transform: none !important; max-height: 80vh; overflow-y: auto; }

/* Insight cards */
.ins-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(155px, 1fr)); gap: .6rem; margin: .4rem 0 1rem; }
.ins-card { background: rgba(128,128,128,.09); border-radius: .8rem; padding: .7rem .8rem;
            border-right: 4px solid rgba(128,128,128,.35); direction: rtl; text-align: right; }
.ins-card.good { border-right-color: #2e9e5b; }
.ins-card.bad  { border-right-color: #d9534f; }
.ins-top { display: flex; align-items: center; justify-content: space-between; gap: .4rem; }
.ins-icon { font-size: 1.2rem; }
.ins-big { font-size: 1.45rem; font-weight: 700; direction: ltr; unicode-bidi: isolate; font-variant-numeric: tabular-nums; }
.ins-card.good .ins-big { color: #3fbf73; }
.ins-card.bad  .ins-big { color: #ef6b66; }
.ins-title { font-weight: 500; font-size: .9rem; margin-top: .25rem; }
.ins-text { font-size: .78rem; opacity: .75; line-height: 1.45; margin-top: .2rem; }

/* Hebrew: right-to-left text and layout */
.stApp [data-testid="stMain"], .stApp section.main,
.stApp [data-testid="stSidebar"] { direction: rtl; text-align: right; }

/* Mixed Hebrew/English lines: each line picks its own direction from its first letter */
.stApp p, .stApp h1, .stApp h2, .stApp h3, .stApp li, .stApp label,
.stApp [data-testid="stCaptionContainer"] { unicode-bidi: plaintext; text-align: right; }

/* Keep numbers, charts, sliders and tables left-to-right so they read correctly */
.stApp [data-testid="stMetricValue"], .stApp [data-testid="stMetricDelta"] { direction: ltr; text-align: right; justify-content: flex-end; }
.stApp [data-testid="stMetricValue"] > div { text-align: right; }
.stApp [data-testid="stPlotlyChart"], .stApp .js-plotly-plot,
.stApp [data-baseweb="slider"], .stApp [data-testid="stDataFrame"],
.stApp [data-testid="stTable"], .stApp code, .stApp pre { direction: ltr; }
.stApp input[type="number"], .stApp input[type="password"] { direction: ltr; text-align: right; }
</style>
"""


def rtl() -> None:
    # st.html injects a <style> block reliably (and takes no space on the page);
    # older Streamlit versions fall back to markdown.
    # both ways: depending on the Streamlit version one of them applies the <style> block
    if hasattr(st, "html"):
        st.html(_CSS)
    st.markdown(_CSS, unsafe_allow_html=True)
