"""Visit the Streamlit app in a real browser so Streamlit Cloud doesn't put it to sleep.

If the app is already asleep, click the "wake up" button and wait for it to come back.
"""
import sys

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

APP_URL = "https://btc-portfolio-jguv8kcdauchggtvou2z9w.streamlit.app"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto(APP_URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(10_000)

    wake = page.get_by_role("button", name="get this app back up", exact=False)
    if wake.count() > 0:
        print("App was asleep - waking it up")
        wake.first.click()
        page.wait_for_timeout(5_000)
    else:
        print("App is awake")

    # Wait for the app itself to load (Streamlit renders inside an iframe on *.streamlit.app)
    try:
        page.wait_for_selector("iframe", timeout=180_000)
        page.wait_for_timeout(20_000)
        print("App loaded")
    except PWTimeout:
        print("App did not finish loading in time", file=sys.stderr)
        browser.close()
        sys.exit(1)

    browser.close()
