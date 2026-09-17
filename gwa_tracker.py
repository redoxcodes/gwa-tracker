"""
gwa-tracker main script (v5).

Runs once per invocation, triggered every 10 minutes by GitHub Actions
cron. Telegram message handling happens instantly via a Cloudflare
Worker + KV — this script only checks tweets and expiry.

Required secrets / env vars:
  - TELEGRAM_BOT_TOKEN
  - TWITTERAPI_KEY
  - WORKER_URL
  - WEBHOOK_SECRET
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

KEYWORDS = ["@metawin", "username", "metawin.com"]
MAX_QUERY_CHARS = 480

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TWITTERAPI_KEY = os.environ["TWITTERAPI_KEY"]
WORKER_URL = os.environ["WORKER_URL"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TWITTERAPI_SEARCH_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
SUBSCRIBERS_ENDPOINT = f"{WORKER_URL}/subscribers"

ADMIN_HANDLE = "https://t.me/amredox"
EXPIRED_MESSAGE = (
    "Your subscription has ended. "
    "Subscribe to gain access to all notifications from Metawin giveaway host. "
    f"DM me here to get a token: {ADMIN_HANDLE}"
)

ACCOUNTS_FILE = "accounts.json"
SEEN_POSTS_FILE = "seen_posts.json"
LAST_CHECK_FILE = "last_check.txt"


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


def get_subscribers():
    try:
        resp = requests.get(
            SUBSCRIBERS_ENDPOINT,
            headers={"X-Api-Secret": WEBHOOK_SECRET},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[warn] failed to read subscribers ({resp.status_code}): {resp.text[:200]}")
            return {}
        return resp.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"[warn] failed to read subscribers: {e}")
        return {}


def save_subscribers(subscribers):
    try:
        resp = requests.post(
            SUBSCRIBERS_ENDPOINT,
            headers={"X-Api-Secret": WEBHOOK_SECRET, "Content-Type": "application/json"},
            data=json.dumps(subscribers),
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[warn] failed to save subscribers ({resp.status_code}): {resp.text[:200]}")
    except requests.RequestException as e:
        print(f"[warn] failed to save subscribers: {e}")


def tg_send(chat_id, text):
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[warn] failed to send Telegram message to {chat_id}: {e}")


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


def batch_accounts(accounts, keyword_clause, max_chars):
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
    print(f"[debug] query: {query}")

    headers = {"X-API-Key": TWITTERAPI_KEY}
    all_tweets = []
    cursor = None

    while True:
        params = {"query": query, "queryType": "Latest"}
        if cursor:
            params["cursor"] = cursor

        try:
            resp = requests.get(TWITTERAPI_SEARCH_URL, headers=headers, params=params, timeout=25)
        except requests.RequestException as e:
            print(f"[warn] search request failed, skipping this batch: {e}")
            break

        if resp.status_code == 429:
            print("[warn] rate limited (429), waiting 6s and retrying once")
            time.sleep(6)
            try:
                resp = requests.get(TWITTERAPI_SEARCH_URL, headers=headers, params=params, timeout=25)
            except requests.RequestException as e:
                print(f"[warn] retry also failed, skipping this batch: {e}")
                break

        if resp.status_code != 200:
            print(f"[warn] search failed ({resp.status_code}): {resp.text[:200]}")
            break

        data = resp.json()
        print(f"[debug] raw response snippet: {resp.text[:500]}")
        batch_tweets = data.get("tweets", [])
        print(f"[debug] got {len(batch_tweets)} tweets in this page")
        all_tweets.extend(batch_tweets)

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

    # --- TEMPORARY SANITY CHECK: bare single-account query, wide window, no keywords ---
    sanity_since = (utcnow() - timedelta(days=7)).strftime("%Y-%m-%d_%H:%M:%S_UTC")
    sanity_until = utcnow().strftime("%Y-%m-%d_%H:%M:%S_UTC")
    sanity_query = f"from:amredox since:{sanity_since} until:{sanity_until}"
    print(f"[sanity] query: {sanity_query}")
    try:
        sanity_resp = requests.get(
            TWITTERAPI_SEARCH_URL,
            headers={"X-API-Key": TWITTERAPI_KEY},
            params={"query": sanity_query, "queryType": "Latest"},
            timeout=25,
        )
        print(f"[sanity] status: {sanity_resp.status_code}")
        print(f"[sanity] response: {sanity_resp.text[:1000]}")
    except requests.RequestException as e:
        print(f"[sanity] request failed: {e}")
    # --- END SANITY CHECK ---

    keyword_clause = " OR ".join(KEYWORDS)


    since_str_raw = load_text(LAST_CHECK_FILE, "")
    if since_str_raw:
        since_dt = parse_iso(since_str_raw)
    else:
        since_dt = utcnow() - timedelta(minutes=15)
    until_dt = utcnow()

    since_str = since_dt.strftime("%Y-%m-%d_%H:%M:%S_UTC")
    until_str = until_dt.strftime("%Y-%m-%d_%H:%M:%S_UTC")
    print(f"[debug] checking window: {since_str} to {until_str}")
    print(f"[debug] tracking {len(accounts)} accounts")

    new_tweets = []
    for batch in batch_accounts(accounts, keyword_clause, MAX_QUERY_CHARS):
        tweets = search_batch(batch, keyword_clause, since_str, until_str)
        for tweet in tweets:
            tweet_id = tweet.get("id")
            if tweet_id and tweet_id not in seen_posts:
                new_tweets.append(tweet)
                seen_posts.append(tweet_id)
        time.sleep(6)  # free tier allows 1 request every 5 seconds; stay safely under

    save_text(LAST_CHECK_FILE, iso(until_dt))

    if len(seen_posts) > 2000:
        del seen_posts[: len(seen_posts) - 2000]

    return new_tweets


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


def main():
    subscribers = get_subscribers()
    seen_posts = load_json(SEEN_POSTS_FILE, [])

    expire_subscribers(subscribers)

    new_tweets = check_new_tweets(seen_posts)
    alert_subscribers(subscribers, new_tweets)

    save_subscribers(subscribers)
    save_json(SEEN_POSTS_FILE, seen_posts)


if __name__ == "__main__":
    main()
