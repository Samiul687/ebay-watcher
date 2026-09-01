#!/usr/bin/env python3
"""
Generic eBay watcher (official Browse API) - runs any number of independent
searches defined in config.yaml, each with its own price range, keyword
filters, and poll interval, and sends a Telegram message for every new
matching listing.

Setup:
    pip install requests pyyaml

    Fill in config.yaml next to this script: Telegram bot token + chat id,
    eBay Developer App ID/Cert ID, and one or more entries under `searches:`.
    See the comments in config.yaml for what each field does.

Run:
    python ebay_watcher.py
"""

import os
import re
import sys
import time
import json
import base64
import requests
import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("WATCHER_CONFIG", os.path.join(SCRIPT_DIR, "config.yaml"))
STATE_FILE = os.path.join(SCRIPT_DIR, "seen_listings.json")

EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not config or "searches" not in config or not config["searches"]:
        raise SystemExit(f"No searches defined in {CONFIG_PATH}.")

    # Secrets always come from the environment (never from config.yaml,
    # since that file is meant to be safe to commit to git). A `telegram:`
    # / `ebay:` block in the YAML is still honoured as a local-only
    # fallback if present, but env vars take priority.
    yaml_telegram = config.get("telegram", {})
    yaml_ebay = config.get("ebay", {})

    telegram_cfg = {
        "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", yaml_telegram.get("bot_token")),
        "chat_id": os.environ.get("TELEGRAM_CHAT_ID", yaml_telegram.get("chat_id")),
    }
    ebay_cfg = {
        "app_id": os.environ.get("EBAY_APP_ID", yaml_ebay.get("app_id")),
        "cert_id": os.environ.get("EBAY_CERT_ID", yaml_ebay.get("cert_id")),
    }
    missing = [
        k for k, v in {**{f"telegram.{k}": v for k, v in telegram_cfg.items()},
                        **{f"ebay.{k}": v for k, v in ebay_cfg.items()}}.items()
        if not v
    ]
    if missing:
        raise SystemExit(
            "Missing config: " + ", ".join(missing) +
            ". Set them as environment variables (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, "
            "EBAY_APP_ID, EBAY_CERT_ID) or in config.yaml."
        )

    defaults = config.get("defaults", {})
    resolved = []
    for s in config["searches"]:
        if "name" not in s or "query" not in s:
            raise SystemExit("Every search needs at least 'name' and 'query'.")
        merged = {
            "marketplace": defaults.get("marketplace", "EBAY_US"),
            "currency": defaults.get("currency", "USD"),
            "poll_seconds": defaults.get("poll_seconds", 300),
            "min_price": None,
            "max_price": None,
            "exclude_keywords": [],
            "require_keywords": [],
            "title_regex": None,
        }
        merged.update(s)
        merged["_title_re"] = re.compile(merged["title_regex"], re.IGNORECASE) if merged["title_regex"] else None
        resolved.append(merged)

    return telegram_cfg, ebay_cfg, resolved


def title_matches(search, title):
    if search["_title_re"] and not search["_title_re"].search(title):
        return False
    lowered = title.lower()
    if any(w.lower() in lowered for w in search.get("exclude_keywords") or []):
        return False
    if any(w.lower() not in lowered for w in search.get("require_keywords") or []):
        return False
    return True


# ---------------------------------------------------------------------------
# State: which listings we've already alerted on, and when each search was
# last actually run - both keyed by search name, both persisted to disk so
# they survive between runs. This matters a lot once this script is invoked
# fresh by a scheduler (cron / GitHub Actions) rather than left running:
# without a persisted last_checked, every search would look "due" on every
# single invocation, regardless of its configured poll_seconds.
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            # stray file from the old single-search script (a flat list of
            # ids, no per-search structure) - nothing sensible to migrate,
            # start fresh rather than crash.
            return {}, {}
        # migrate from the older flat {name: [ids]} format if found
        if "seen" not in data and "last_checked" not in data:
            return {name: set(ids) for name, ids in data.items()}, {}
        seen = {name: set(ids) for name, ids in data.get("seen", {}).items()}
        last_checked = data.get("last_checked", {})
        return seen, last_checked
    return {}, {}


def save_state(seen, last_checked):
    with open(STATE_FILE, "w") as f:
        json.dump(
            {
                "seen": {name: list(ids) for name, ids in seen.items()},
                "last_checked": last_checked,
            },
            f,
        )


# ---------------------------------------------------------------------------
# eBay
# ---------------------------------------------------------------------------

_token_cache = {"access_token": None, "expires_at": 0}


def get_access_token(ebay_cfg):
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    creds = f"{ebay_cfg['app_id']}:{ebay_cfg['cert_id']}".encode("utf-8")
    basic_auth = base64.b64encode(creds).decode("utf-8")

    resp = requests.post(
        EBAY_OAUTH_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic_auth}",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 7200)) - 60
    return _token_cache["access_token"]


def build_price_filter(search):
    lo, hi = search.get("min_price"), search.get("max_price")
    if lo is None and hi is None:
        return None
    lo_s = "" if lo is None else str(lo)
    hi_s = "" if hi is None else str(hi)
    return f"price:[{lo_s}..{hi_s}],priceCurrency:{search['currency']}"


def _search_request(token, search):
    params = {
        "q": search["query"],
        "sort": "newlyListed",
        "limit": 50,
    }
    price_filter = build_price_filter(search)
    if price_filter:
        params["filter"] = price_filter

    return requests.get(
        EBAY_SEARCH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": search["marketplace"],
        },
        params=params,
        timeout=20,
    )


def fetch_listings(ebay_cfg, search):
    """Returns a list of (id, title, link, price_text) tuples for one search."""
    token = get_access_token(ebay_cfg)
    resp = _search_request(token, search)

    if resp.status_code == 401:
        _token_cache["access_token"] = None
        token = get_access_token(ebay_cfg)
        resp = _search_request(token, search)

    resp.raise_for_status()
    data = resp.json()

    listings = []
    for item in data.get("itemSummaries", []):
        item_id = item.get("itemId")
        title = item.get("title", "")
        link = item.get("itemWebUrl")
        price = item.get("price") or {}
        price_value = price.get("value")
        price_currency = price.get("currency", search["currency"])

        # belt-and-braces client-side price check
        if price_value is not None:
            value = float(price_value)
            if search.get("min_price") is not None and value < search["min_price"]:
                continue
            if search.get("max_price") is not None and value > search["max_price"]:
                continue

        price_text = f"{price_value} {price_currency}" if price_value is not None else "price unknown"

        if item_id and link and title_matches(search, title):
            listings.append((item_id, title, link, price_text))
    return listings


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(telegram_cfg, text):
    url = f"https://api.telegram.org/bot{telegram_cfg['bot_token']}/sendMessage"
    payload = {
        "chat_id": telegram_cfg["chat_id"],
        "text": text,
        "disable_web_page_preview": False,
    }
    resp = requests.post(url, data=payload, timeout=20)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_search(telegram_cfg, ebay_cfg, search, seen, first_run):
    name = search["name"]
    seen_ids = seen.setdefault(name, set())

    listings = fetch_listings(ebay_cfg, search)
    for item_id, title, link, price_text in listings:
        if item_id not in seen_ids:
            if not first_run:
                send_telegram(telegram_cfg, f"[{name}] New listing ({price_text}):\n{title}\n{link}")
                print(f"[{name}] Notified: {title} - {price_text}")
            seen_ids.add(item_id)


def due_searches(searches, last_checked, now):
    due = []
    for s in searches:
        checked_at = last_checked.get(s["name"], 0)
        if now - checked_at >= s["poll_seconds"]:
            due.append(s)
    return due


def run_due_searches(telegram_cfg, ebay_cfg, searches, seen, last_checked):
    """Runs every search that's currently due, updating + saving state as it
    goes. Returns the list of search names that were actually run."""
    now = time.time()
    ran = []
    for search in due_searches(searches, last_checked, now):
        name = search["name"]
        first_run = name not in seen
        try:
            run_search(telegram_cfg, ebay_cfg, search, seen, first_run)
        except requests.RequestException as e:
            print(f"[{name}] Request failed, will retry next cycle: {e}")
        except (KeyError, ValueError) as e:
            print(f"[{name}] Unexpected eBay response, will retry next cycle: {e}")
        finally:
            # mark it checked either way, so one bad cycle doesn't tighten
            # the effective interval on the next attempt
            last_checked[name] = time.time()
            ran.append(name)
            save_state(seen, last_checked)
    return ran


def main():
    telegram_cfg, ebay_cfg, searches = load_config()
    seen, last_checked = load_state()
    once = "--once" in sys.argv or os.environ.get("WATCHER_ONCE") == "1"

    for s in searches:
        print(f"Watching '{s['query']}' ({s['marketplace']}) as '{s['name']}', every {s['poll_seconds']}s...")

    if once:
        ran = run_due_searches(telegram_cfg, ebay_cfg, searches, seen, last_checked)
        print(f"Done. Ran: {ran or '(none due yet)'}")
        return

    while True:
        run_due_searches(telegram_cfg, ebay_cfg, searches, seen, last_checked)
        now = time.time()
        next_due_in = min(
            (s["poll_seconds"] - (now - last_checked.get(s["name"], 0))) for s in searches
        )
        time.sleep(max(1.0, next_due_in))


if __name__ == "__main__":
    main()
