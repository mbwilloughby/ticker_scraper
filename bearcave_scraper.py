import asyncio
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime

import aiohttp
import pytz
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import (
    get_current_time,
    get_next_market_times,
    sleep_until_market_open,
)
from utils.websocket_sender import initialize_websocket, send_ws_message

load_dotenv()

# Constants
JSON_URL = "https://thebearcave.substack.com/api/v1/posts"
CHECK_INTERVAL = 0.05  # seconds
PROCESSED_URLS_FILE = "data/bearcave_processed_urls.json"
PROXY_FILE = "cred/proxies.json"
TELEGRAM_BOT_TOKEN = os.getenv("BEARCAVE_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("BEARCAVE_TELEGRAM_GRP")

os.makedirs("data", exist_ok=True)

# User agents list
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 Edg/91.0.864.59",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 OPR/78.0.4093.112",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/91.0.4472.80 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36",
]


def load_proxies():
    """Load proxies from json file"""
    try:
        with open(PROXY_FILE, "r") as f:
            data = json.load(f)
            return data["bearcave"]
    except Exception as e:
        log_message(f"Error loading proxies: {e}", "ERROR")
        return []


def load_processed_urls():
    try:
        with open(PROCESSED_URLS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_urls(urls):
    with open(PROCESSED_URLS_FILE, "w") as f:
        json.dump(list(urls), f, indent=2)
    log_message("Processed URLs saved.", "INFO")


def get_random_headers():
    """Generate random headers for requests"""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Requested-With": str(uuid.uuid4()),
        "X-Request-Time": str(int(time.time())),
    }


def get_random_cache_buster():
    """Generate random cache busting url variable for requests"""
    cache_busters = [
        ("timestamp", lambda: int(time.time() * 10000)),
        ("request_uuid", lambda: str(uuid.uuid4())),
        ("cache_time", lambda: int(time.time())),
        ("ran_time", lambda: int(time.time() * 1000)),
        ("no_cache_uuid", lambda: str(uuid.uuid4().hex[:16])),
        ("unique", lambda: f"{int(time.time())}-{random.randint(1000, 9999)}"),
        ("req_uuid", lambda: f"req-{uuid.uuid4().hex[:8]}"),
        ("tist", lambda: str(int(time.time()))),
    ]

    variable, value_generator = random.choice(cache_busters)
    return f"{variable}={value_generator()}"


async def fetch_json(session, raw_proxy=None):
    """Fetch JSON data with proxy support and custom headers"""
    headers = get_random_headers()
    random_cache_buster = get_random_cache_buster()
    proxy = raw_proxy if raw_proxy is None else f"http://{raw_proxy}"

    try:
        start_time = time.time()

        async with session.get(
            f"{JSON_URL}?limit=10&{random_cache_buster}",
            headers=headers,
            proxy=proxy,
            timeout=1,  # FIXME: Try to bring it down to 0.2 or 0.1 seconds later down the line when we have the proper proxy
        ) as response:
            if response.status == 200:
                data = await response.json()
                log_message(
                    f"Fetched posts from JSON in {(time.time() - start_time):.2f}s using proxy: {raw_proxy}",
                    "INFO",
                )
                return data
            elif 500 <= response.status < 600:
                log_message(
                    f"Server error {response.status}: Temporary issue, safe to ignore if infrequent.",
                    "WARNING",
                )
                return []
            else:
                log_message(f"Failed to fetch JSON: HTTP {response.status}", "ERROR")
                return []
    except asyncio.TimeoutError:
        log_message(f"Took more then 1 sec to fetch with proxy: {raw_proxy}", "WARNING")
        return []
    except Exception as e:
        log_message(f"Error fetching JSON with proxy {raw_proxy}: {e}", "ERROR")
        return []


def is_draft_post(url):
    """Check if the URL is a draft post"""
    return "/publish/post/" in url


def extract_ticker(title):
    if title is not None and title.find("Problems at") != -1:
        match = re.search(r"\((.*?)\)", title)
        if match:
            potential_ticker = match.group(1)
            if potential_ticker.isupper():
                return potential_ticker
    return None


def get_post_title(post):
    """Get the most appropriate title from the post data"""
    title = post.get("title", "")
    social_title = post.get("social_title", "")

    if not isinstance(title, str) or not title.strip():
        return (
            social_title
            if social_title
            else "No title found in either title/social_title"
        )
    return title


async def send_to_telegram(post_data, ticker=None):
    current_time = get_current_time()
    post_date = datetime.fromisoformat(post_data["post_date"].replace("Z", "+00:00"))
    post_date_est = post_date.astimezone(pytz.timezone("America/Chicago"))
    update_date = datetime.fromisoformat(post_data["updated_at"].replace("Z", "+00:00"))
    update_date_est = update_date.astimezone(pytz.timezone("America/Chicago"))

    is_draft = is_draft_post(post_data.get("canonical_url", ""))
    title = post_data.get("title", "")
    social_title = post_data.get("social_title", "")

    message = f"<b>{'[DRAFT] ' if is_draft else ''}New Bear Cave Article!</b>\n\n"
    message += (
        f"<b>Published Date:</b> {post_date_est.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    )
    message += (
        f"<b>Updated Date:</b> {update_date_est.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    )
    message += f"<b>Current Date:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    message += f"<b>Title:</b> {title}\n"
    message += f"<b>Social Title:</b> {social_title}\n"
    message += f"<b>URL:</b> {post_data['canonical_url']}\n"

    if ticker:
        message += f"<b>Ticker:</b> {ticker}\n"
        await send_ws_message(
            {
                "name": "The Bear Cave",
                "type": "Sell",
                "ticker": ticker,
                "sender": "bearcave",
            },
        )
        log_message(
            f"Ticker sent to WebSocket: {ticker} - {post_data['canonical_url']}", "INFO"
        )

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(f"Article sent to Telegram: {post_data['canonical_url']}", "INFO")


async def run_scraper():
    processed_urls = load_processed_urls()
    proxies = load_proxies()

    if not proxies:
        log_message("No proxies available. Running without proxies.", "WARNING")
        proxies = [None]

    async with aiohttp.ClientSession() as session:
        while True:
            await sleep_until_market_open()
            await initialize_websocket()

            log_message("Market is open. Starting to check for new posts...", "DEBUG")
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = get_current_time()

                if current_time > market_close_time:
                    log_message(
                        "Market is closed. Waiting for next market open...", "DEBUG"
                    )
                    break

                log_message("Checking for new posts...")
                proxy = random.choice(proxies)
                posts = await fetch_json(session, proxy)

                new_posts = [
                    post
                    for post in posts
                    if post.get("canonical_url")
                    and post["canonical_url"] not in processed_urls
                ]

                if new_posts:
                    log_message(f"Found {len(new_posts)} new posts to process.", "INFO")

                    for post in new_posts:
                        title = get_post_title(post)
                        ticker = extract_ticker(title)
                        await send_to_telegram(post, ticker)
                        processed_urls.add(post["canonical_url"])

                    save_processed_urls(processed_urls)
                else:
                    log_message("No new posts found.", "INFO")

                await asyncio.sleep(CHECK_INTERVAL)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_GRP]):
        log_message("Missing required environment variables", "CRITICAL")
        sys.exit(1)

    try:
        asyncio.run(run_scraper())
    except KeyboardInterrupt:
        log_message("Shutting down gracefully...", "INFO")
    except Exception as e:
        log_message(f"Critical error in main: {e}", "CRITICAL")
        sys.exit(1)


if __name__ == "__main__":
    main()
