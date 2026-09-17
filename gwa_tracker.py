"""
gwa-tracker main script.

Runs once per invocation (designed to be triggered every 10 minutes by
GitHub Actions cron). Each run:

  1. Processes any new Telegram messages since the last run
     - admin commands: /generate, /addsub <chat_id>, /removesub <chat_id>
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
ADMIN_HANDLE = "@amredox"
KEYWORDS = ["@metawin", "username", "metawin.com"]
SUBSCRIPTION_DAYS = 31
MAX_QUERY_CHARS = 480  # stay safely under twitterapi.io's ~512 char limit

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TWITTERAPI_KEY = os.environ["TWITTERAPI_KEY"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TWITTERAPI_SEARCH_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"

SUBSCRIBE_MESSAGE = (
    "Subscribe to gain access to all giveaway tracked posts. "
    f"DM {ADMIN_HANDLE} to get a token."
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
        for chat
