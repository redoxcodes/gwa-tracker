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

    page.screenshot(path="debug_login_screen.png")

    try:
        page.wait_for_selector('input[autocomplete="username"]', timeout=15000)
        page.fill('input[autocomplete="username"]', X_USERNAME)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)

        page.screenshot(path="debug_after_username.png")

        page.wait_for_selector('input[type="password"]', timeout=15000)
        page.fill('input[type="password"]', X_PASSWORD)
        page.keyboard.press("Enter")
        page.wait_for_timeout(5000)

        page.screenshot(path="debug_after_login.png")
    except Exception as e:
        page.screenshot(path="debug_login_failed.png")
        print(f"Login step failed: {e}")
        raise


def get_latest_post(page, username):
    page.goto(f"https://x.com/{username}")
    page.wait_for_timeout(4000)
    links = page.eval_on_selector_all(
        'a[href*="/status/"]',
        "els => els.map(e => e.href)"
    )
    if not links:
        return None
    return links[0].split("?")[0]


def main():
    usernames = load_usernames()
    seen = load_seen()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        login(page)

        for username in usernames:
            try:
                latest = get_latest_post(page, username)
                if latest and seen.get(username) != latest:
                    seen[username] = latest
                    send_telegram(f"New post from @{username}:\n{latest}")
                    print(f"New post found for {username}: {latest}")
                else:
                    print(f"No new post for {username}")
                time.sleep(3)
            except Exception as e:
                print(f"Error checking {username}: {e}")

        browser.close()

    save_seen(seen)


if __name__ == "__main__":
    main()
