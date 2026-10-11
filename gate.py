"""Lock screen that keeps portfolio data private even when the app itself is public.

Every page calls `require_login()` right after `st.set_page_config`. Nothing below it
(DB reads, edits, deletes) runs until the viewer has proven they know APP_PASSWORD
(a Streamlit secret). Proof comes from either:
  * `?key=...` in the URL - sent automatically by the home-screen launcher page, or
  * typing the password on the lock screen.
Once verified, the flag lives in this browser session, so switching pages doesn't re-ask,
and a derived token (not the password) is kept in the page URL: when Streamlit starts a new
session (app update, phone waking up) on any page, the URL still proves access.

When the password is typed, the page tells the launcher (the page that embeds the app)
to remember it on the device; a wrong saved key tells the launcher to forget it.
"""
import hashlib
import hmac
import json
import os

import streamlit as st
import streamlit.components.v1 as components

LAUNCHER_ORIGIN = "https://tomchadash-jpg.github.io"


def _token(password: str) -> str:
    """URL-safe stand-in for the password (so the password itself never sits in the URL)."""
    return hashlib.sha256(f"portfolio:{password}".encode()).hexdigest()[:32]


def _ok(candidate: str, password: str) -> bool:
    if not candidate:
        return False
    c = candidate.encode()
    return hmac.compare_digest(c, password.encode()) or hmac.compare_digest(c, _token(password).encode())


def _keep_in_url(password: str) -> None:
    try:
        if st.query_params.get("key") != _token(password):
            st.query_params["key"] = _token(password)
    except Exception:
        pass


def _tell_launcher(msg: dict) -> None:
    # Invisible component; postMessage to the top window (the launcher) is allowed cross-origin.
    components.html(
        f"<script>try{{window.top.postMessage({json.dumps(msg)}, {json.dumps(LAUNCHER_ORIGIN)});}}catch(e){{}}</script>",
        height=0,
    )


def require_login() -> None:
    password = (os.getenv("APP_PASSWORD") or "").strip()
    if not password:
        # No password configured yet: the app must stay private on Streamlit Cloud.
        st.caption(":material/warning: APP_PASSWORD לא מוגדר ב-Secrets – אל תהפוך את האפליקציה לציבורית")
        return

    if st.session_state.get("authed"):
        if st.session_state.pop("_remember", None):
            _tell_launcher({"type": "portfolio-key", "key": password})
        _keep_in_url(password)
        return

    key = st.query_params.get("key", "")
    if key:
        if _ok(key, password):
            st.session_state["authed"] = True
            _keep_in_url(password)
            return
        _tell_launcher({"type": "portfolio-bad-key"})

    st.markdown("<div style='height:12vh'></div>", unsafe_allow_html=True)
    st.markdown("<h2 style='text-align:center'><span style=\"font-family:'Material Symbols Rounded';vertical-align:-0.15em\">"
                "lock</span> My Portfolio</h2>", unsafe_allow_html=True)
    with st.form("login", border=False):
        typed = st.text_input("סיסמה", type="password")
        if st.form_submit_button("כניסה", use_container_width=True):
            if _ok(typed.strip(), password):
                st.session_state["authed"] = True
                st.session_state["_remember"] = True
                st.rerun()
            st.error("סיסמה שגויה")
    st.stop()
