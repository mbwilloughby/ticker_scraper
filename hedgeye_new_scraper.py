import asyncio
import json
import os
import random
import re
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import aiohttp
import pytz
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumrequests import Chrome

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN = os.getenv("HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN")
HEDGEYE_SCRAPER_TELEGRAM_GRP = os.getenv("HEDGEYE_SCRAPER_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")
DATA_DIR = "data"
RATE_LIMIT_PROXY_FILE = os.path.join(DATA_DIR, "hedgeye_new_rate_limited_proxy.json")
RATE_LIMIT_ACCOUNTS_FILE = os.path.join(
    DATA_DIR, "hedgeye_new_rate_limited_accounts.json"
)
LAST_ALERT_FILE = os.path.join(DATA_DIR, "hedgeye_new_last_alert.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs("data/hedgeye_cookies", exist_ok=True)

options = Options()
options.add_argument("--headless")
options.add_argument("--maximize-window")
options.add_argument("--disable-extensions")
options.add_argument("--disable-popup-blocking")
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_experimental_option("excludeSwitches", ["enable-automation"])


class ProxyManager:
    def __init__(self, proxies: List[str]):
        self.proxies = proxies
        self.current_index = 0
        self.rate_limited: Dict[str, datetime] = self._load_rate_limited()

    def _load_rate_limited(self) -> Dict[str, datetime]:
        if os.path.exists(RATE_LIMIT_PROXY_FILE):
            with open(RATE_LIMIT_PROXY_FILE, "r") as f:
                rate_limited = json.load(f)
                return {k: datetime.fromisoformat(v) for k, v in rate_limited.items()}
        return {}

    def _save_rate_limited(self):
        with open(RATE_LIMIT_PROXY_FILE, "w") as f:
            rate_limited = {k: v.isoformat() for k, v in self.rate_limited.items()}
            json.dump(rate_limited, f)

    def get_next_proxy(self) -> str:
        current_time = datetime.now()

        expired_proxies = [
            proxy
            for proxy, limit_time in self.rate_limited.items()
            if (current_time - limit_time).total_seconds() >= 900  # 15 minutes
        ]

        for proxy in expired_proxies:
            del self.rate_limited[proxy]
            log_message(
                f"Proxy {proxy} removed from rate limits (15-minute expired)", "INFO"
            )

        if expired_proxies:
            self._save_rate_limited()

        available_proxies = [
            proxy for proxy in self.proxies if proxy not in self.rate_limited
        ]

        if not available_proxies:
            raise Exception("No available proxies")

        proxy = random.choice(available_proxies)
        return proxy

    def mark_rate_limited(self, proxy: str):
        self.rate_limited[proxy] = datetime.now()
        self._save_rate_limited()

    def clear_rate_limits(self):
        self.rate_limited.clear()
        if os.path.exists(RATE_LIMIT_PROXY_FILE):
            os.remove(RATE_LIMIT_PROXY_FILE)
        log_message("All proxy rate limits cleared", "INFO")


class AccountManager:
    def __init__(self, accounts: List[Tuple[str, str]]):
        self.accounts = accounts
        self.rate_limited: Set[str] = self._load_rate_limited()
        self.currently_running: Set[str] = set()
        self.lock = asyncio.Lock()

    def _load_rate_limited(self) -> Set[str]:
        if os.path.exists(RATE_LIMIT_ACCOUNTS_FILE):
            with open(RATE_LIMIT_ACCOUNTS_FILE, "r") as f:
                return set(json.load(f))
        return set()

    def _save_rate_limited(self):
        with open(RATE_LIMIT_ACCOUNTS_FILE, "w") as f:
            json.dump(list(self.rate_limited), f)

    async def get_available_accounts(self, count: int) -> List[Tuple[str, str]]:
        async with self.lock:
            available = [
                acc
                for acc in self.accounts
                if acc[0] not in self.rate_limited
                and acc[0] not in self.currently_running
            ]
            selected = random.sample(available, min(count, len(available)))
            self.currently_running.update(email for email, _ in selected)
            return selected

    async def release_account(self, email: str):
        async with self.lock:
            if email in self.currently_running:
                self.currently_running.remove(email)

    def mark_rate_limited(self, email: str):
        self.rate_limited.add(email)
        self._save_rate_limited()

    def clear_rate_limits(self):
        self.rate_limited.clear()
        self.currently_running.clear()
        if os.path.exists(RATE_LIMIT_ACCOUNTS_FILE):
            os.remove(RATE_LIMIT_ACCOUNTS_FILE)
        log_message("All account rate limits cleared", "INFO")


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


async def get_public_ip(proxy: Optional[str]) -> str:
    """Get public IP address using the proxy"""
    ip_check_url = "https://api.ipify.org?format=text"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                ip_check_url, proxy=f"http://{proxy}" if proxy else None
            ) as response:
                if response.status == 200:
                    ip = await response.text()
                    return ip.strip()
                return f"Code: {response.status}"
    except Exception as e:
        return f"Error: {e}"


def login(driver, email, password) -> Optional[Dict[str, str]]:
    login_url = "https://accounts.hedgeye.com/users/sign_in"
    driver.get(login_url)

    try:
        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located((By.ID, "user_email"))
        )

        email_input = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "user_email"))
        )
        email_input.send_keys(email)

        password_input = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "user_password"))
        )
        password_input.send_keys(password)
        password_input.send_keys(Keys.RETURN)

        WebDriverWait(driver, 60).until(EC.url_changes(login_url))

        # Get required cookies
        cookies = driver.get_cookies()
        session_cookie = next(
            (c for c in cookies if c["name"] == "_hedgeye_session"), None
        )
        customer_type = next((c for c in cookies if c["name"] == "customer_type"), None)

        if session_cookie and customer_type:
            return {
                "_hedgeye_session": session_cookie["value"],
                "customer_type": customer_type["value"],
            }
        return None

    except Exception as e:
        log_message(f"Error during login: {str(e)}", "ERROR")
        return None


def save_cookies(cookies: Dict[str, str], email: str):
    os.makedirs("data/hedgeye_cookies", exist_ok=True)
    filename = f"data/hedgeye_cookies/{email.replace('@', '_').replace('.', '_')}.json"
    with open(filename, "w") as f:
        json.dump(cookies, f)

    log_message(
        f"Logged in and saved cookies for account: {email}",
        "INFO",
    )


def load_cookies(email: str) -> Optional[Dict[str, str]]:
    filename = f"data/hedgeye_cookies/{email.replace('@', '_').replace('.', '_')}.json"
    if os.path.exists(filename):
        with open(filename, "r") as f:
            return json.load(f)
    return None


async def validate_credentials(email: str, password: str) -> Optional[Dict[str, str]]:
    """Validate credentials and get new cookies if needed"""
    try:
        cookies = load_cookies(email)
        if cookies:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://app.hedgeye.com/logged_in", cookies=cookies
                ) as response:
                    if response.status == 200:
                        log_message(f"Using existing cookies for {email}", "INFO")
                        return cookies

        # If no cookies or invalid, get new ones
        log_message(f"Getting new cookies for {email}", "INFO")
        driver = Chrome(options=options)
        try:
            new_cookies = login(driver, email, password)
            if new_cookies:
                save_cookies(new_cookies, email)
                return new_cookies
        finally:
            driver.quit()

        return None
    except Exception as e:
        log_message(f"Error validating credentials for {email}: {str(e)}", "ERROR")
        return None


async def initialize_accounts(accounts: List[tuple]) -> List[tuple]:
    """Initialize all accounts and return only valid ones"""
    valid_accounts = []

    # Process accounts in groups of 3 to avoid overwhelming the server
    for i in range(0, len(accounts), 3):
        batch = accounts[i : i + 3]
        batch_tasks = [
            validate_credentials(email, password) for email, password in batch
        ]
        results = await asyncio.gather(*batch_tasks)

        for (email, password), cookies in zip(batch, results):
            if cookies:
                valid_accounts.append((email, password))
                log_message(f"Successfully validated account: {email}", "INFO")
            else:
                log_message(f"Failed to validate account: {email}", "ERROR")

        # Small delay between batches
        if i + 3 < len(accounts):
            await asyncio.sleep(0.8)

    return valid_accounts


async def fetch_alert_details(
    session: aiohttp.ClientSession, cookies: Dict[str, str], proxy: Optional[str]
):
    try:
        cache_buster = get_random_cache_buster()
        url = f"https://app.hedgeye.com/logged_in?{cache_buster}"

        start_time = time.time()
        async with session.get(
            url,
            cookies=cookies,
            proxy=f"http://{proxy}" if proxy else None,
            timeout=aiohttp.ClientTimeout(
                total=3
            ),  # FIXME: Later try to bring this down to 1 or 2
        ) as response:
            if response.status == 429:
                raise Exception("Rate limited")
            html = await response.text()

        soup = BeautifulSoup(html, "html.parser")

        alert_title = soup.select_one(".article__header")
        if not alert_title:
            return None
        alert_title = alert_title.get_text(strip=True)

        alert_price = soup.select_one(".currency.se-live-or-close-price")
        if not alert_price:
            return None
        alert_price = alert_price.get_text(strip=True)

        created_at_utc = soup.select_one("time[datetime]")["datetime"]
        created_at = datetime.fromisoformat(created_at_utc.replace("Z", "+00:00"))
        created_at_edt = created_at.astimezone(pytz.timezone("America/New_York"))
        current_time_edt = datetime.now(pytz.timezone("America/New_York"))

        return {
            "title": alert_title,
            "price": alert_price,
            "created_at": created_at_edt,
            "current_time": current_time_edt,
            "fetch_time": time.time() - start_time,
        }

    except asyncio.TimeoutError:
        public_ip = await get_public_ip(proxy)
        log_message(
            f"Fetch alert took more then 3 seconds with ip: {public_ip}, Gotta fix this ASAP",
            "WARNING",
        )

        return None
    except Exception as e:
        if "Rate limited" in str(e):
            raise
        log_message(f"Error fetching alert: {str(e)}", "ERROR")
        return None


async def process_account(
    email: str,
    password: str,
    proxy: str,
    proxy_manager,
    account_manager,
    last_alert_lock: asyncio.Lock,
):
    try:
        cookies = load_cookies(email)
        if not cookies:
            driver = Chrome(options=options)
            try:
                cookies = login(driver, email, password)
                if cookies:
                    save_cookies(cookies, email)
            finally:
                driver.quit()

        if not cookies:
            return

        async with aiohttp.ClientSession() as session:
            alert_details = await fetch_alert_details(session, cookies, proxy)
            if alert_details is None:
                return

            log_message(
                f"fetch_alert_details took {alert_details['fetch_time']:.2f} seconds. for {email}, {proxy}",
                "INFO",
            )

            if not alert_details:
                return

            async with last_alert_lock:
                last_alert = {}
                if os.path.exists(LAST_ALERT_FILE):
                    with open(LAST_ALERT_FILE, "r") as f:
                        last_alert = json.load(f)

                if not last_alert or alert_details["title"] != last_alert.get("title"):
                    signal_type = (
                        "Buy"
                        if "buy" in alert_details["title"].lower()
                        else (
                            "Sell"
                            if "sell" in alert_details["title"].lower()
                            else "None"
                        )
                    )

                    ticker_match = re.search(
                        r"\b([A-Z]{1,5})\b(?=\s*\$)", alert_details["title"]
                    )
                    ticker = ticker_match.group(0) if ticker_match else "-"

                    await send_ws_message(
                        {
                            "name": "Hedgeye",
                            "type": signal_type,
                            "ticker": ticker,
                            "sender": "hedgeye",
                        },
                        WS_SERVER_URL,
                    )

                    message = (
                        f"Title: {alert_details['title']}\n"
                        f"Price: {alert_details['price']}\n"
                        f"Created At: {alert_details['created_at'].strftime('%Y-%m-%d %H:%M:%S %Z%z')}\n"
                        f"Current Time: {alert_details['current_time'].strftime('%Y-%m-%d %H:%M:%S %Z%z')}\n"
                        f"Fetch Time: {alert_details['fetch_time']:.2f}s"
                    )
                    await send_telegram_message(
                        message,
                        HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN,
                        HEDGEYE_SCRAPER_TELEGRAM_GRP,
                    )

                    with open(LAST_ALERT_FILE, "w") as f:
                        json.dump(
                            {
                                "title": alert_details["title"],
                                "price": alert_details["price"],
                                "created_at": alert_details["created_at"].isoformat(),
                            },
                            f,
                            indent=2,
                        )

                    log_message(
                        f"New alert Sent to telegram: {alert_details['title']}",
                        "INFO",
                    )

            # FIXME: Also try to bring this into 1 sec if not move on to different proxy provider
            if alert_details["fetch_time"] > 2:
                public_ip = await get_public_ip(proxy)
                log_message(
                    f"Slow fetch detected Publid IP: {public_ip}, took {alert_details['fetch_time']} seconds",
                    "WARNING",
                )

    except Exception as e:
        if "Rate limited" in str(e):
            proxy_manager.mark_rate_limited(proxy)
            account_manager.mark_rate_limited(email)
        log_message(f"Error processing account {email}: {str(e)}", "ERROR")


async def process_accounts_continuously(
    account_manager: AccountManager,
    proxy_manager: ProxyManager,
    last_alert_lock: asyncio.Lock,
):
    while True:
        try:
            accounts = await account_manager.get_available_accounts(2)
            if not accounts:
                # If no accounts available, wait briefly and try again
                await asyncio.sleep(0.4)
                continue

            for i, (email, password) in enumerate(accounts):
                await asyncio.sleep(0.8)

                proxy = proxy_manager.get_next_proxy()
                asyncio.create_task(
                    process_account_with_release(
                        email,
                        password,
                        proxy,
                        proxy_manager,
                        last_alert_lock,
                        account_manager,
                    )
                )

            await asyncio.sleep(0.1)

        except Exception as e:
            log_message(f"Error in process_accounts_continuously: {str(e)}", "ERROR")
            await asyncio.sleep(1)


async def process_account_with_release(
    email: str,
    password: str,
    proxy: str,
    proxy_manager: ProxyManager,
    last_alert_lock: asyncio.Lock,
    account_manager: AccountManager,
):
    try:
        await process_account(
            email, password, proxy, proxy_manager, account_manager, last_alert_lock
        )
    finally:
        await account_manager.release_account(email)


async def main():
    if not all(
        [
            HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN,
            HEDGEYE_SCRAPER_TELEGRAM_GRP,
            WS_SERVER_URL,
        ]
    ):
        log_message("Missing required environment variables", "CRITICAL")
        return

    try:
        # Load accounts and proxies
        with open("cred/hedgeye_credentials.json", "r") as f:
            all_accounts = [
                (acc["email"], acc["password"]) for acc in json.load(f)["accounts"]
            ]

        with open("cred/proxies.json", "r") as f:
            proxies = json.load(f)["hedgeye"]

        log_message("Initializing accounts...", "INFO")
        valid_accounts = await initialize_accounts(all_accounts)

        if not valid_accounts:
            log_message("No valid accounts available. Exiting...", "CRITICAL")
            return

        log_message(f"Successfully initialized {len(valid_accounts)} accounts", "INFO")

        last_alert_lock = asyncio.Lock()
        proxy_manager = ProxyManager(proxies)
        account_manager = AccountManager(valid_accounts)

        while True:
            await sleep_until_market_open(start=8, end=15)
            log_message("Market is open. Starting to check for posts...", "DEBUG")
            _, _, market_close_time = get_next_market_times(start=8, end=15)

            process_task = asyncio.create_task(
                process_accounts_continuously(
                    account_manager, proxy_manager, last_alert_lock
                )
            )

            while datetime.now(pytz.timezone("America/New_York")) <= market_close_time:
                # 10 sec sleep between checking to avoid overheat in the server
                await asyncio.sleep(10)

            # Cancel the processing task
            process_task.cancel()
            try:
                await process_task
            except asyncio.CancelledError:
                pass

            log_message("Market is closed. Waiting for next market open...", "DEBUG")

            # Clear rate limits at end of day
            proxy_manager.clear_rate_limits()
            account_manager.clear_rate_limits()

    except Exception as e:
        log_message(f"Critical error: {str(e)}", "CRITICAL")


if __name__ == "__main__":
    asyncio.run(main())
