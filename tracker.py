import os
import json
import time
from playwright.sync_api import sync_playwright
import requests

# ---- CONFIG (comes from GitHub Secrets, not hardcoded) ----
X_USERNAME = os.environ["X_USERNAME"]
X_PASSWORD = os.environ["X_PASSWORD"]
TG_TOKEN = os.environ["TG_TOKEN"]

SEEN_FILE = "seen_posts.json"
USERNAMES_FILE = "usernames.txt"
KEYWORDS_FILE = "keywords.txt"
SUBSCRIBERS_FILE = "subscribers.json"
LAST_UPDATE_FILE = "last_update_id.txt"

MAX_POSTS_PER_CHECK = 5   # how many recent posts to look at per account
MAX_SEEN_PER_USER = 30    # cap stored history so the file doesn't grow forever


def load_keywords():
    if not os.path.exists(KEYWORDS_FILE):
        return []
    with open(KEYWORDS_FILE) as f:
        return [line.strip().lower() for line in f if line.strip()]


def load_subscribers():
    if os.path.exists(SUBSCRIBERS_FILE):
        with open(SUBSCRIBERS_FILE) as f:
            return json.load(f)
    return []


def save_subscribers(subscribers):
    with open(SUBSCRIBERS_FILE, "w") as f:
        json.dump(subscribers, f, indent=2)


def load_last_update_id():
    if os.path.exists(LAST_UPDATE_FILE):
        with open(LAST_UPDATE_FILE) as f:
            content = f.read().strip()
            return int(content) if content else 0
    return 0


def save_last_update_id(update_id):
    with open(LAST_UPDATE_FILE, "w") as f:
        f.write(str(update_id))


def register_new_subscribers():
    subscribers = load_subscribers()
    last_update_id = load_last_update_id()

    url = "https://api.telegram.org/bot" + TG_TOKEN + "/getUpdates"
    params = {"offset": last_update_id + 1, "timeout": 5}
    try:
        response = requests.get(url, params=params, timeout=15)
        data = response.json()
    except Exception as e:
        print("Could not check for new subscribers: " + str(e))
        return subscribers

    if not data.get("ok"):
        return subscribers

    highest_update_id = last_update_id
    for update in data.get("result", []):
        highest_update_id = max(highest_update_id, update["update_id"])
        message = update.get("message")
        if not message:
            continue
        chat_id = str(message["chat"]["id"])
        if chat_id not in subscribers:
            subscribers.append(chat_id)
            print("New subscriber added: " + chat_id)
            requests.post(
                "https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage",
                data={"chat_id": chat_id, "text": "You're subscribed! You'll get an alert here whenever a tracked post matches."}
            )

    if highest_update_id > last_update_id:
        save_last_update_id(highest_update_id)
    save_subscribers(subscribers)
    return subscribers


def load_usernames():
    with open(USERNAMES_FILE) as f:
        return [line.strip().lstrip("@") for line in f if line.strip()]


def load_seen():
    """
    seen_posts.json format: { "username": ["link1", "link2", ...], ... }
    Keeps a small rolling history per account so posts that get buried
    by a newer post within one check window are still caught, without
    ever re-sending something already recorded.
    """
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            data = json.load(f)
        # migrate old format (single string per user) to list format
        migrated = {}
        for user, val in data.items():
            if isinstance(val, str):
                migrated[user] = [val]
            else:
                migrated[user] = val
        return migrated
    return {}


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)


def send_telegram(text, subscribers):
    url = "https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage"
    for chat_id in subscribers:
        requests.post(url, data={"chat_id": chat_id, "text": text})


def login(page):
    page.goto("https://x.com/login", wait_until="domcontentloaded")
    page.wait_for_timeout(5000)

    page.screenshot(path="debug_login_screen.png")

    try:
        dialog_selector = 'div[role="dialog"][aria-modal="true"]'
        dialog = page.locator(dialog_selector)
        dialog.wait_for(state="visible", timeout=15000)
        page.wait_for_timeout(1000)

        username_input = dialog.locator("input").first
        username_input.wait_for(state="visible", timeout=15000)
        username_input.click()
        page.wait_for_timeout(500)
        username_input.type(X_USERNAME, delay=100)
        page.wait_for_timeout(1000)

        page.screenshot(path="debug_after_typing.png")

        next_button = dialog.get_by_role("button", name="Next")
        if next_button.count() > 0:
            next_button.click()
        else:
            page.keyboard.press("Enter")
        page.wait_for_timeout(3000)

        page.screenshot(path="debug_after_username.png")

        password_selector = 'input[type="password"]'
        page.wait_for_selector(password_selector, timeout=15000)
        page.fill(password_selector, X_PASSWORD)
        page.keyboard.press("Enter")
        page.wait_for_timeout(5000)

        page.screenshot(path="debug_after_login.png")
    except Exception as e:
        page.screenshot(path="debug_login_failed.png")
        print("Login step failed: " + str(e))
        raise


def get_recent_posts(page, username, max_posts=MAX_POSTS_PER_CHECK):
    """
    Returns a list of (link, text) tuples for the most recent non-pinned
    posts, newest first. Returns [] if none could be read.
    """
    page.goto("https://x.com/" + username)
    page.wait_for_timeout(4000)

    articles = page.locator("article")
    count = min(articles.count(), max_posts)
    posts = []

    for i in range(count):
        article = articles.nth(i)
        try:
            article_text = article.inner_text()
        except Exception:
            continue

        # Skip pinned posts - X always shows these first regardless of recency
        if "Pinned" in article_text.split("\n")[0:3]:
            continue

        links = article.locator('a[href*="/status/"]')
        if links.count() == 0:
            continue

        link = links.first.get_attribute("href")
        if link and link.startswith("/"):
            link = "https://x.com" + link

        posts.append((link, article_text))

    return posts


def main():
    usernames = load_usernames()
    seen = load_seen()
    keywords = load_keywords()
    subscribers = register_new_subscribers()
    print("Current subscriber count: " + str(len(subscribers)))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        login(page)

        for username in usernames:
            try:
                posts = get_recent_posts(page, username)
                if not posts:
                    print("No post found for " + username)
                    continue

                seen_links = seen.get(username, [])
                # process oldest-to-newest so alerts arrive in chronological order
                new_posts = [p for p in reversed(posts) if p[0] not in seen_links]

                if not new_posts:
                    print("No new post for " + username)
                    continue

                for link, text in new_posts:
                    seen_links.append(link)

                    if keywords:
                        text_lower = (text or "").lower()
                        matched = [kw for kw in keywords if kw in text_lower]
                        if not matched:
                            print("New post for " + username + ", but no keyword match - skipped")
                            continue
                        msg = "New post from @" + username + " (matched: " + matched[0] + "):\n" + link
                        send_telegram(msg, subscribers)
                        print("New post found for " + username + ": " + link + " (matched: " + str(matched) + ")")
                    else:
                        msg = "New post from @" + username + ":\n" + link
                        send_telegram(msg, subscribers)
                        print("New post found for " + username + ": " + link)

                # keep only the most recent N links so the file doesn't grow forever
                seen[username] = seen_links[-MAX_SEEN_PER_USER:]

                time.sleep(3)
            except Exception as e:
                print("Error checking " + username + ": " + str(e))

        browser.close()

    save_seen(seen)


if __name__ == "__main__":
    main()
