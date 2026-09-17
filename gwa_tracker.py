"""
gwa-tracker main script (v2).

Runs once per invocation, triggered every 10 minutes by GitHub Actions
cron. Telegram message handling (/generate, /addsub, /removesub,
/broadcast, token redemption, /start fallback) now happens instantly
via a Cloudflare Worker + KV — this script no longer polls Telegram
at all.

This script's job, each run:
  1. Read the current subscriber list from Cloudflare KV (the same
     store the Worker writes to)
  2. Expire anyone past their 31-day access, notify them, write the
     updated list back to KV
  3. Check tracked accounts for new keyword-matching tweets via
     twitterapi.io's advanced_search, batched into a handful of OR
     queries to keep costs low
  4. Send Telegram alerts for any new matching tweets to all
     currently active subscribers

Local state files (still plain JSON/text, created on first run if
missing):
  - accounts.json   : list of tracked X/Twitter usernames (yours to edit)
  - seen_posts.json : list of tweet IDs already alerted on
  - last_check.txt  : ISO timestamp of the last tweet-check window

Required secrets / env vars:
  - TELEGRAM_BOT_TOKEN
  - TWITTERAPI_KEY
  - CF_ACCOUNT_ID
  - CF_KV_NAMESPACE_ID
  - CF_API_TOKEN
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

KEYWORDS = ["@metawin", "username", "metawin.com"]
MAX_QUERY_CHARS = 480  # stay safely under twitterapi.io's ~512 char limit

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TWITTERAPI_KEY = os.environ["TWITTERAPI_KEY"]
CF_ACCOUNT_ID = os.environ["CF_ACCOUNT_ID"]
CF_KV_NAMESPACE_ID = os.environ["CF_KV_NAMESPACE_ID"]
CF_API_TOKEN = os.environ["CF_API_TOKEN"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TWITTERAPI_SEARCH_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
CF_KV_BASE = (
    f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
    f"/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values"
)

ADMIN_HANDLE = "https://t.me/amredox"
EXPIRED_MESSAGE = (
    "Your subscription has ended. "
    "Subscribe to gain access to all notifications from Metawin giveaway host. "
    f"DM me here to get a token: {ADMIN_HANDLE}"
)

ACCOUNTS_FILE = "accounts.json"
SEEN_POSTS_FILE = "seen_posts.json"
LAST_CHECK_FILE = "last_check.txt"


# ---------------------------------------------------------------------------
# Small local JSON/state helpers
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
# Cloudflare KV helpers (shared state with the Worker)
# ---------------------------------------------------------------------------

def kv_get(key, default):
    resp = requests.get(
        f"{CF_KV_BASE}/{key}",
        headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
        timeout=15,
    )
    if resp.status_code != 200:
        return default
    try:
        return json.loads(resp.text)
    except json.JSONDecodeError:
        return default


def kv_put(key, value):
    resp = requests.put(
        f"{CF_KV_BASE}/{key}",
        headers={
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "Content-Type": "application/json",
        },
        data=json.dumps(value),
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"[warn] failed to write KV key '{key}' ({resp.status_code}): {resp.text[:200]}")


# ---------------------------------------------------------------------------
# Telegram helper
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


# ---------------------------------------------------------------------------
# Step 1: expire old subscriptions
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
# Step 2: check tracked accounts for new keyword-matching tweets
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
# Step 3: alert subscribers
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
    subscribers = kv_get("subscribers", {})
    seen_posts = load_json(SEEN_POSTS_FILE, [])

    expire_subscribers(subscribers)

    new_tweets = check_new_tweets(seen_posts)
    alert_subscribers(subscribers, new_tweets)

    kv_put("subscribers", subscribers)
    save_json(SEEN_POSTS_FILE, seen_posts)


if __name__ == "__main__":
    main()
