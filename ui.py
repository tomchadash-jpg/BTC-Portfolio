"""Right-to-left layout for the Hebrew UI. Call `rtl()` right after st.set_page_config."""
import streamlit as st

_CSS = """
<style>
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
    st.markdown(_CSS, unsafe_allow_html=True)
