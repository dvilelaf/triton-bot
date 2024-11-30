""""Triton Telegram bot"""

import asyncio
import datetime
import logging
import os
from pathlib import Path

import dotenv
import pytz
import yaml
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from triton.chain import get_olas_price, get_slots
from triton.constants import (AGENT_BALANCE_THRESHOLD, AUTOCLAIM,
                              AUTOCLAIM_DAY, AUTOCLAIM_HOUR_UTC,
                              GNOSISSCAN_URL, MANUAL_CLAIM,
                              SAFE_BALANCE_THRESHOLD)
from triton.tools import escape_markdown_v2
from triton.trader import Trader

logger = logging.getLogger("telegram_bot")

# Secrets
dotenv.load_dotenv(override=True)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")


def run_triton() -> None:
    """Main"""

    # Load configuration
    with open("config.yaml", "r", encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)

    # Instantiate the traders
    traders = {
        trader_name: Trader(trader_name, Path(trader_path))
        for trader_name, trader_path in config["traders"].items()
    }

    # Commands
    async def staking_status(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        messages = []
        total_rewards = 0
        for trader_name, trader in traders.items():
            status = trader.get_staking_status()
            total_rewards += float(status["accrued_rewards"].split(" ")[0])
            messages.append(
                f"[{trader_name}] {status['accrued_rewards']} [{status['mech_requests_this_epoch']}/{status['required_mech_requests']}]\nNext epoch: {status['epoch_end']}"
            )

        olas_price = get_olas_price()
        rewards_value = total_rewards * olas_price if olas_price else None
        message = f"Total rewards = {total_rewards:.2f} OLAS"
        if rewards_value:
            message += f" [€{rewards_value:.2f}]"
        messages.append(message)

        await update.message.reply_text(text=("\n\n").join(messages))

    async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
        messages = []
        for trader_name, trader in traders.items():
            balances = trader.check_balance()
            agent_native_balance = balances["agent_native_balance"]
            safe_native_balance = balances["safe_native_balance"]
            safe_olas_balance = balances["safe_olas_balance"]
            operator_native_balance = balances["operator_native_balance"]

            message = (
                r"\["
                + escape_markdown_v2(trader_name)
                + r"]"
                + f"\n[Agent]({GNOSISSCAN_URL.format(address=trader.agent_address)}) = {agent_native_balance:.2f} xDAI"
                + f"\n[Safe]({GNOSISSCAN_URL.format(address=trader.service_safe_address)}) = {safe_native_balance:.2f} xDAI  {safe_olas_balance:.2f} OLAS"
                + f"\n[Operator]({GNOSISSCAN_URL.format(address=trader.operator_address)}) = {operator_native_balance:.2f} xDAI"
            )

            messages.append(message)

        await update.message.reply_text(
            text=("\n\n").join(messages),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )

    async def claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Claim rewards"""

        if not MANUAL_CLAIM:
            await update.message.reply_text(text="Manual claim is disabled")
            return

        messages = []
        for trader_name, trader in traders.items():
            trader.claim_rewards()
            messages.append(
                f"[{trader_name}] Sent claim transaction. Rewards will be sent to the Safe."
            )

        await update.message.reply_text(
            text=("\n").join(messages),
        )

    async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Withdraw rewards"""
        messages = []
        for trader_name, trader in traders.items():
            ok, value = trader.withdraw_rewards()
            message = (
                r"\["
                + escape_markdown_v2(trader_name)
                + r"] "
                + f"Sent withdrawal transaction. €{value:.2f} of OLAS sent from the Safe to [{trader.withdrawal_address}]({GNOSISSCAN_URL.format(address=trader.withdrawal_address)}) #withdraw"
                if ok
                else r"\["
                + escape_markdown_v2(trader_name)
                + r"] "
                + "Cannot withdraw rewards (recipient not set)"
            )

            messages.append(message)

        await update.message.reply_text(
            text=("\n\n").join(messages),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )

    async def slots(update: Update, context: ContextTypes.DEFAULT_TYPE):

        slots = get_slots()

        messages = [
            f"[{contract_name}] {n_slots} available slots"
            for contract_name, n_slots in slots.items()
        ]

        await update.message.reply_text(
            text=("\n").join(messages),
        )

    async def scheduled_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
        jobs = context.job_queue.jobs()

        if not jobs:
            await update.message.reply_text("No scheduled jobs")
            return

        message = ""
        for job in jobs:
            next_execution = job.next_t.astimezone(
                pytz.timezone("Europe/Madrid")
            ).strftime("%Y-%m-%d %H:%M:%S")
            message += f"• {job.name}: {next_execution}\n"

        await update.message.reply_text(message)

    # Tasks
    async def start(context: ContextTypes.DEFAULT_TYPE):
        """Start"""
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="Triton has started",
        )

    async def balance_check(context: ContextTypes.DEFAULT_TYPE):
        logger.info("Running balance check task")
        for trader in traders.values():
            balances = trader.check_balance()
            agent_native_balance = balances["agent_native_balance"]
            safe_native_balance = balances["safe_native_balance"]

            if agent_native_balance < AGENT_BALANCE_THRESHOLD:
                message = f"[{trader.name}] [Agent]({GNOSISSCAN_URL.format(address=trader.agent_address)}) balance is {agent_native_balance:.2f} xDAI"
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )

            if safe_native_balance < SAFE_BALANCE_THRESHOLD:
                message = f"[{trader.name}] [Safe]({GNOSISSCAN_URL.format(address=trader.service_safe_address)}) balance is {safe_native_balance:.2f} xDAI"
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )

    async def post_init(app):
        # await app.bot.set_my_name("Triton")
        await app.bot.set_my_description("A bot to manage Olas staked services")
        await app.bot.set_my_short_description("A bot to manage Olas staked services")
        await app.bot.set_my_commands(
            [
                ("staking_status", "Staking status"),
                ("balance", "Check wallet balances"),
                ("claim", "Claim rewards"),
                ("withdraw", "Withdraw rewards"),
                ("slots", "Check available staking slots"),
                ("jobs", "Check the scheduled jobs"),
            ]
        )

    async def autoclaim(context: ContextTypes.DEFAULT_TYPE):
        logger.info("Running autoclaim task")

        if not AUTOCLAIM:
            logger.info("Autoclaim task is disabled")
            return

        messages = []

        # Claim
        for trader in traders.values():
            trader.claim_rewards()

        # Wait for confirmation
        await asyncio.sleep(10)

        # Withdraw
        for trader in traders.values():
            ok, value = trader.withdraw_rewards()
            message = (
                r"\["
                + escape_markdown_v2(trader.name)
                + r"] "
                + f"(Autoclaim) Sent #withdraw transaction. €{value:.2f} of OLAS sent from the Safe to [{trader.withdrawal_address}]({GNOSISSCAN_URL.format(address=trader.withdrawal_address)})"
                if ok
                else r"\["
                + escape_markdown_v2(trader.name)
                + r"] "
                + "(Autoclaim) Cannot withdraw rewards (recipient not set)"
            )

            messages.append(message)

        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=("\n\n").join(messages),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )

    # Create bot
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    job_queue = app.job_queue

    # Add commands
    app.add_handler(CommandHandler("staking_status", staking_status))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("claim", claim))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CommandHandler("slots", slots))
    app.add_handler(CommandHandler("jobs", scheduled_jobs))

    # Add tasks
    job_queue.run_once(start, when=3)  # in 3 seconds
    job_queue.run_repeating(
        balance_check,
        interval=datetime.timedelta(hours=1),
        first=5,  # in 5 seconds
    )
    job_queue.run_monthly(
        autoclaim,
        day=AUTOCLAIM_DAY,
        when=datetime.time(
            hour=AUTOCLAIM_HOUR_UTC, minute=0, second=0, microsecond=0, tzinfo=None
        ),
    )

    # Start
    logger.info("Starting bot")
    app.run_polling()