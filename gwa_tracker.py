"""
gwa-tracker main script.

Runs once per invocation (designed to be triggered every 10 minutes by
GitHub Actions cron). Each run:

  1. Processes any new Telegram messages since the last run
     - admin commands: /generate, /addsub <chat_id>, /removesub <chat_id>, /broadcast <message>
     - token redemption from any user (plain message = the token text)
     - fallback "subscribe to gain access" message for anyone else
  2. Expires any subscriber whose 31-day access has run out, and
     notifies them
  3. Checks tracked accounts for new keyword-matching tweets via
     twitterapi.io's advanced_search, batched into a handful of OR
     queries to keep costs low
  4. Sends Telegram alerts for any new matching tweets to all active
     subscribers

State files (all plain JSON, created on first run if missing):
  - accounts.json      : list of tracked X/Twitter usernames (yours to edit)
  - subscribers.json    : {chat_id: {"expires_at": iso_str}}
  - tokens.json         : {token: {"used": bool, "used_by": chat_id|None}}
  - seen_posts.json     : list of tweet IDs already alerted on
  - last_update_id.txt  : last processed Telegram update_id
  - last_check.txt      : ISO timestamp of the last tweet-check window

Required secrets / env vars:
  - TELEGRAM_BOT_TOKEN
  - TWITTERAPI_KEY
"""

import json
import os
import secrets as pysecrets
import string
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ADMIN_CHAT_ID = 1465049104
ADMIN_HANDLE = "https://t.me/amredox"
KEYWORDS = ["@metawin", "username", "metawin.com"]
SUBSCRIPTION_DAYS = 31
MAX_QUERY_CHARS = 480  # stay safely under twitterapi.io's ~512 char limit

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TWITTERAPI_KEY = os.environ["TWITTERAPI_KEY"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TWITTERAPI_SEARCH_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"

SUBSCRIBE_MESSAGE = (
    "Subscribe to gain access to all notifications from Metawin giveaway host. "
    f"DM me here to get a token: {ADMIN_HANDLE}"
)
EXPIRED_MESSAGE = (
    "Your subscription has ended. "
    + SUBSCRIBE_MESSAGE
)

ACCOUNTS_FILE = "accounts.json"
SUBSCRIBERS_FILE = "subscribers.json"
TOKENS_FILE = "tokens.json"
SEEN_POSTS_FILE = "seen_posts.json"
LAST_UPDATE_ID_FILE = "last_update_id.txt"
LAST_CHECK_FILE = "last_check.txt"


# ---------------------------------------------------------------------------
# Small JSON/state helpers
# ---------------------------------------------------------------------------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_text(path, default=""):
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        return f.read().strip()


def save_text(path, text):
    with open(path, "w") as f:
        f.write(str(text))


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


def parse_iso(s):
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def tg_send(chat_id, text):
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[warn] failed to send Telegram message to {chat_id}: {e}")


def tg_get_updates(last_update_id):
    resp = requests.get(
        f"{TELEGRAM_API}/getUpdates",
        params={"offset": last_update_id + 1, "timeout": 0},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------

def generate_token():
    alphabet = string.ascii_lowercase + string.digits
    return "mw-" + "".join(pysecrets.choice(alphabet) for _ in range(10))


def grant_subscription(subscribers, chat_id):
    expires_at = utcnow() + timedelta(days=SUBSCRIPTION_DAYS)
    subscribers[str(chat_id)] = {"expires_at": iso(expires_at)}


# ---------------------------------------------------------------------------
# Step 1: process incoming Telegram messages
# ---------------------------------------------------------------------------

def process_telegram_updates(subscribers, tokens):
    last_update_id = int(load_text(LAST_UPDATE_ID_FILE, "0") or "0")
    updates = tg_get_updates(last_update_id)

    for update in updates:
        last_update_id = max(last_update_id, update["update_id"])
        message = update.get("message")
        if not message or "text" not in message:
            continue

        chat_id = message["chat"]["id"]
        text = message["text"].strip()

        # --- Admin commands ---
        if chat_id == ADMIN_CHAT_ID:
            if text == "/generate":
                token = generate_token()
                tokens[token] = {"used": False, "used_by": None}
                tg_send(chat_id, f"New token: {token}")
                continue

            if text.startswith("/addsub"):
                parts = text.split()
                if len(parts) == 2:
                    grant_subscription(subscribers, parts[1])
                    tg_send(chat_id, f"Added {parts[1]} for {SUBSCRIPTION_DAYS} days.")
                else:
                    tg_send(chat_id, "Usage: /addsub <chat_id>")
                continue

            if text.startswith("/removesub"):
                parts = text.split()
                if len(parts) == 2 and parts[1] in subscribers:
                    del subscribers[parts[1]]
                    tg_send(chat_id, f"Removed {parts[1]}.")
                else:
                    tg_send(chat_id, "Usage: /removesub <chat_id> (must be an existing subscriber)")
                continue

            if text.startswith("/broadcast"):
                announcement = text[len("/broadcast"):].strip()
                if announcement:
                    for sub_chat_id in subscribers:
                        tg_send(sub_chat_id, announcement)
                    tg_send(chat_id, f"Broadcast sent to {len(subscribers)} subscribers.")
                else:
                    tg_send(chat_id, "Usage: /broadcast <your message>")
                continue

        # --- Token redemption (any user) ---
        candidate = text
        if candidate in tokens and not tokens[candidate]["used"]:
            tokens[candidate]["used"] = True
            tokens[candidate]["used_by"] = chat_id
            grant_subscription(subscribers, chat_id)
            tg_send(
                chat_id,
                f"You're subscribed! Access lasts {SUBSCRIPTION_DAYS} days.",
            )
            continue

        # --- Fallback ---
        tg_send(chat_id, SUBSCRIBE_MESSAGE)

    save_text(LAST_UPDATE_ID_FILE, last_update_id)


# ---------------------------------------------------------------------------
# Step 2: expire old subscriptions
# ---------------------------------------------------------------------------

def expire_subscribers(subscribers):
    now = utcnow()
    expired = [
        chat_id
        for chat_id, info in subscribers.items()
        if parse_iso(info["expires_at"]) <= now
    ]
    for chat_id in expired:
        del subscribers[chat_id]
        tg_send(chat_id, EXPIRED_MESSAGE)


# ---------------------------------------------------------------------------
# Step 3: check tracked accounts for new keyword-matching tweets
# ---------------------------------------------------------------------------

def batch_accounts(accounts, keyword_clause, max_chars):
    """Group accounts into OR-queries that stay under the char budget."""
    batches = []
    current = []
    for account in accounts:
        trial = current + [account]
        from_clause = " OR ".join(f"from:{a}" for a in trial)
        query = f"({from_clause}) ({keyword_clause}) -is:retweet"
        if len(query) > max_chars and current:
            batches.append(current)
            current = [account]
        else:
            current = trial
    if current:
        batches.append(current)
    return batches


def search_batch(accounts, keyword_clause, since_str, until_str):
    from_clause = " OR ".join(f"from:{a}" for a in accounts)
    query = f"({from_clause}) ({keyword_clause}) since:{since_str} until:{until_str} -is:retweet"

    headers = {"X-API-Key": TWITTERAPI_KEY}
    all_tweets = []
    cursor = None

    while True:
        params = {"query": query, "queryType": "Latest"}
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(TWITTERAPI_SEARCH_URL, headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"[warn] search failed ({resp.status_code}): {resp.text[:200]}")
            break

        data = resp.json()
        all_tweets.extend(data.get("tweets", []))

        if data.get("has_next_page") and data.get("next_cursor"):
            cursor = data["next_cursor"]
        else:
            break

    return all_tweets


def check_new_tweets(seen_posts):
    accounts = load_json(ACCOUNTS_FILE, [])
    if not accounts:
        print("[warn] accounts.json is empty or missing — nothing to check")
        return []

    keyword_clause = " OR ".join(KEYWORDS)

    since_str_raw = load_text(LAST_CHECK_FILE, "")
    if since_str_raw:
        since_dt = parse_iso(since_str_raw)
    else:
        since_dt = utcnow() - timedelta(minutes=15)
    until_dt = utcnow()

    since_str = since_dt.strftime("%Y-%m-%d_%H:%M:%S_UTC")
    until_str = until_dt.strftime("%Y-%m-%d_%H:%M:%S_UTC")

    new_tweets = []
    for batch in batch_accounts(accounts, keyword_clause, MAX_QUERY_CHARS):
        tweets = search_batch(batch, keyword_clause, since_str, until_str)
        for tweet in tweets:
            tweet_id = tweet.get("id")
            if tweet_id and tweet_id not in seen_posts:
                new_tweets.append(tweet)
                seen_posts.append(tweet_id)
        time.sleep(0.5)  # be gentle on rate limits between batches

    save_text(LAST_CHECK_FILE, iso(until_dt))

    # keep seen_posts from growing forever
    if len(seen_posts) > 2000:
        del seen_posts[: len(seen_posts) - 2000]

    return new_tweets


# ---------------------------------------------------------------------------
# Step 4: alert subscribers
# ---------------------------------------------------------------------------

def alert_subscribers(subscribers, tweets):
    if not tweets or not subscribers:
        return

    for tweet in tweets:
        author = tweet.get("author", {}).get("userName", "unknown")
        text = tweet.get("text", "")[:150]
        tweet_id = tweet.get("id", "")
        url = f"https://x.com/{author}/status/{tweet_id}"
        message = f"New post from @{author}:\n\n{text}\n\n{url}"

        for chat_id in subscribers:
            tg_send(chat_id, message)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    subscribers = load_json(SUBSCRIBERS_FILE, {})
    tokens = load_json(TOKENS_FILE, {})
    seen_posts = load_json(SEEN_POSTS_FILE, [])

    process_telegram_updates(subscribers, tokens)
    expire_subscribers(subscribers)

    new_tweets = check_new_tweets(seen_posts)
    alert_subscribers(subscribers, new_tweets)

    save_json(SUBSCRIBERS_FILE, subscribers)
    save_json(TOKENS_FILE, tokens)
    save_json(SEEN_POSTS_FILE, seen_posts)


if __name__ == "__main__":
    main()
