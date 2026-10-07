#!/usr/bin/env python3
"""
HotUKDeals watcher - scans HotUKDeals' public RSS feeds (new + trending
deals) for posts matching the patterns under `hotukdeals:` in config.yaml,
and sends a Telegram message for each match.

Uses the RSS feeds rather than HUKD's search page, since robots.txt
disallows /search for automated clients.

Seen deal ids live in seen_listings.json alongside the eBay watcher's
(under "hotukdeals: <name>" keys), so watch.yml's existing state commit and
push-conflict merge cover this too.

Run:
    python hukd_watcher.py          # single pass (run by watch.yml)
"""

import os
import re
import html
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import requests
import yaml

from ebay_watcher import CONFIG_PATH, load_state, save_state, send_telegram

FEEDS = [
    "https://www.hotukdeals.com/rss/new",
    "https://www.hotukdeals.com/rss/trending",
]
HEADERS = {"User-Agent": "Mozilla/5.0 (personal deal alert; github.com/Samiul687/ebay-watcher)"}


def load_watches():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    watches = (config or {}).get("hotukdeals") or []
    for w in watches:
        if "name" not in w or "pattern" not in w:
            raise SystemExit("Every hotukdeals entry needs 'name' and 'pattern'.")
        w["_re"] = re.compile(w["pattern"], re.IGNORECASE)
    telegram_cfg = {
        "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN"),
        "chat_id": os.environ.get("TELEGRAM_CHAT_ID"),
    }
    if not all(telegram_cfg.values()):
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID environment variables.")
    return telegram_cfg, watches


def fetch_deals():
    """All deals currently in the feeds, newest first, de-duplicated."""
    deals = {}
    oldest_new = None
    for url in FEEDS:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        for item in ET.fromstring(resp.content).iter("item"):
            guid = item.findtext("guid") or item.findtext("link")
            if not guid:
                continue
            description = re.sub(r"<[^>]+>", " ", item.findtext("description") or "")
            published = parsedate_to_datetime(item.findtext("pubDate")).timestamp()
            if url.endswith("/new"):
                oldest_new = published if oldest_new is None else min(oldest_new, published)
            deals[guid] = {
                "title": (item.findtext("title") or "").strip(),
                "link": item.findtext("link") or guid,
                "description": html.unescape(" ".join(description.split())),
                "published": published,
            }
    return deals, oldest_new


def main():
    telegram_cfg, watches = load_watches()
    if not watches:
        print("No hotukdeals entries in config.yaml.")
        return
    seen, last_checked = load_state()
    deals, oldest_new = fetch_deals()

    for w in watches:
        key = f"hotukdeals: {w['name']}"
        seen_ids = seen.setdefault(key, set())
        previous = last_checked.get(key)
        if previous and oldest_new and oldest_new > previous:
            # More deals were posted since the last run than the feed holds -
            # anything posted in that gap can't be seen any more.
            print(f"[{key}] Warning: feed doesn't reach back to the last check "
                  f"({(oldest_new - previous) / 60:.0f} min gap)")

        for guid, deal in deals.items():
            if guid in seen_ids:
                continue
            if w["_re"].search(f"{deal['title']} {deal['description']}"):
                send_telegram(
                    telegram_cfg,
                    f"🔥 <b>HotUKDeals: {html.escape(w['name'])}</b>\n"
                    f"<a href=\"{html.escape(deal['link'])}\">{html.escape(deal['title'])}</a>\n"
                    f"{html.escape(deal['description'][:200])}",
                )
                print(f"[{key}] Notified: {deal['title']}")
                seen_ids.add(guid)
        last_checked[key] = time.time()

    save_state(seen, last_checked)
    print(f"Scanned {len(deals)} deals.")


if __name__ == "__main__":
    main()
