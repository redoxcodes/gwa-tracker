import os
import json
import time
from playwright.sync_api import sync_playwright
import requests

# ---- CONFIG (comes from GitHub Secrets, not hardcoded) ----
X_USERNAME = os.environ["X_USERNAME"]
X_PASSWORD = os.environ["X_PASSWORD"]
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

SEEN_FILE = "seen_posts.json"
USERNAMES_FILE = "usernames.txt"
KEYWORDS_FILE = "keywords.txt"


def load_keywords():
    if not os.path.exists(KEYWORDS_FILE):
        return []  # no file = no filtering, send everything
    with open(KEYWORDS_FILE) as f:
        return [line.strip().lower() for line in f if line.strip()]


def load_usernames():
    with open(USERNAMES_FILE) as f:
        return [line.strip().lstrip("@") for line in f if line.strip()]


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return json.load(f)
    return {}


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TG_CHAT_ID, "text": text})


def login(page):
    page.goto("https://x.com/login", wait_until="domcontentloaded")
    page.wait_for_timeout(5000)

    # Take a screenshot right away so we can see what screen actually loaded
    page.screenshot(path="debug_login_screen.png")

    try:
        # Stop matching by placeholder text (unreliable) - target the
        # aria-modal dialog (confirmed to load) and grab its first input field directly
        dialog = page.locator('div[role="dialog"][aria
