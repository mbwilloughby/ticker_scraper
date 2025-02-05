import asyncio
import json
import os
import sys
from datetime import datetime

import aiohttp
import pytz
from dotenv import load_dotenv

from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
CHECK_INTERVAL = 15
PROCESSED_TRADES_FILE = "data/ibd_processed_trades.json"
TOKENS_FILE = "data/ibd_tokens.json"
CRED_FILE = "cred/ibd_creds.json"
TELEGRAM_BOT_TOKEN = os.getenv("IBD_TELEGRAM_BOT_TOKEN")
TELEGRAM_GRP = os.getenv("IBD_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

os.makedirs("data", exist_ok=True)


def load_creds():
    try:
        with open(CRED_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log_message(f"Error loading credentials: {e}", "ERROR")
        return None


def load_processed_trades():
    try:
        with open(PROCESSED_TRADES_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_processed_trades(trades):
    with open(PROCESSED_TRADES_FILE, "w") as f:
        json.dump(list(trades), f, indent=2)
    log_message("Processed trades saved.", "INFO")


def save_token(token):
    with open(TOKENS_FILE, "w") as f:
        json.dump(token, f, indent=2)
    log_message("Token saved.", "INFO")


def get_cookies(creds):
    return {
        ".ASPXAUTH": creds["auth_token"],
        "ibdSession": f"Webuserid={creds['user_id']}&RolesUpdated=True&LogInFlag=1&SessionId={creds['session_id']}",
    }


async def get_new_token(session, creds):
    headers = {
        "accept": "application/json, text/plain, */*",
        "origin": "https://swingtrader.investors.com",
        "referer": "https://swingtrader.investors.com/?ibdsilentlogin=true",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    }

    try:
        async with session.post(
            "https://swingtrader.investors.com/api/token",
            cookies=get_cookies(creds),
            headers=headers,
        ) as response:
            if response.status == 200:
                token = await response.json()
                save_token(token)
                log_message("New token obtained and saved.", "INFO")
                return token
            else:
                log_message(f"Failed to get token: HTTP {response.status}", "ERROR")
                return None
    except Exception as e:
        log_message(f"Error getting token: {e}", "ERROR")
        return None


async def fetch_trades(session, creds, token):
    params = {
        "pageIdx": "1",
        "pageSize": "0",
        "orderBy": "updated",
        "order": "DESC",
        "caller": "web-currenttradescontroller",
        "refresh": "manual",
    }

    headers = {
        "accept": "application/json, text/plain, */*",
        "origin": "https://swingtrader.investors.com",
        "authorization": f"Bearer {token}",
        "referer": "https://swingtrader.investors.com/?ibdsilentlogin=true",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "webuserid": creds["user_id"],
    }

    try:
        async with session.get(
            "https://swingtrader.investors.com/api/trade/state/CURRENT",
            params=params,
            cookies=get_cookies(creds),
            headers=headers,
        ) as response:
            if response.status == 200:
                return await response.json()
            elif response.status == 426:
                log_message("Token expired, getting new token", "INFO")
                new_token = await get_new_token(session, creds)
                if new_token:
                    return await fetch_trades(session, creds, new_token)
            elif response.status == 429:
                log_message(f"Too Many requests, slow down...", "ERROR")
                await asyncio.sleep(CHECK_INTERVAL * 5)
                return None
            log_message(f"Failed to fetch trades: HTTP {response.status}", "ERROR")
            return None
    except Exception as e:
        log_message(f"Error fetching trades: {e}", "ERROR")
        return None


async def send_to_telegram(trade):
    await send_ws_message(
        {
            "name": "IBD SwingTrader",
            "type": "Buy",
            "ticker": trade["stockSymbol"],
            "sender": "ibd_swing",
            "target": "CSS",
        },
        WS_SERVER_URL,
    )

    current_time = datetime.now(pytz.timezone("US/Eastern"))
    created_time = datetime.fromisoformat(trade["created"].replace("Z", "+00:00"))

    message = f"<b>New IBD SwingTrader Alert!</b>\n\n"
    message += f"<b>ID:</b> {trade['id']}\n"
    message += f"<b>Symbol:</b> {trade['stockSymbol']}\n"
    message += f"<b>Company:</b> {trade['companyName']}\n"
    message += f"<b>Created:</b> {created_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    message += f"<b>Current Time:</b> {current_time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"

    await send_telegram_message(message, TELEGRAM_BOT_TOKEN, TELEGRAM_GRP)
    log_message(
        f"Trade alert sent to Telegram & Websocket: {trade['stockSymbol']}", "INFO"
    )


async def run_scraper():
    creds = load_creds()
    if not creds:
        log_message("Failed to load credentials", "CRITICAL")
        return

    processed_trades = load_processed_trades()

    async with aiohttp.ClientSession() as session:
        token = await get_new_token(session, creds)
        if not token:
            log_message("Failed to get initial token", "CRITICAL")
            return

        while True:
            await sleep_until_market_open()
            log_message("Market is open. Starting to check for new trades...")
            _, _, market_close_time = get_next_market_times()

            while True:
                current_time = datetime.now(pytz.timezone("America/New_York"))
                if current_time > market_close_time:
                    log_message("Market is closed. Waiting for next market open...")
                    break

                trades_data = await fetch_trades(session, creds, token)
                if trades_data:
                    new_trades = [
                        trade
                        for trade in trades_data["trades"]
                        if str(trade["id"]) not in processed_trades
                    ]

                    if new_trades:
                        for trade in new_trades:
                            await send_to_telegram(trade)
                            processed_trades.add(str(trade["id"]))
                        save_processed_trades(processed_trades)
                    else:
                        log_message("No new trades found.", "INFO")

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
