"""Telegram Bot entry point."""

import logging
from telegram.ext import Application

from config.settings import TELEGRAM_BOT_TOKEN
from bot.services.database import init_db
from bot.handlers.commands import register_handlers, post_init

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Check your .env file.")

    logger.info("Initializing database...")
    init_db()

    logger.info("Starting bot...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    register_handlers(app)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()