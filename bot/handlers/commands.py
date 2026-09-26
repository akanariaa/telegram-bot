"""Telegram bot command and message handlers registration."""

from telegram import BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.modules.chatbot import (
    handle_message,
    handle_rp_command,
    handle_prompt_command,
    handle_reset_command,
    handle_mode_command,
    handle_help_command,
)
from bot.modules.todo import check_reminders

BOT_COMMANDS = [
    BotCommand("help", "명령어 목록 보기"),
    BotCommand("rp", "RP 모드 활성화 (캐릭터 설정)"),
    BotCommand("prompt", "시스템 프롬프트 오버라이드"),
    BotCommand("reset", "프롬프트 및 대화 초기화"),
    BotCommand("mode", "현재 모드 확인"),
]


async def post_init(app: Application) -> None:
    """Run after the bot starts — register commands and schedule reminder checker."""
    await app.bot.set_my_commands(BOT_COMMANDS)
    app.job_queue.run_repeating(
        callback=_reminder_callback,
        interval=30,
        first=10,
        name="reminder_checker",
    )


async def _reminder_callback(context) -> None:
    await check_reminders(context.application)


def register_handlers(app: Application) -> None:
    """Register all command and message handlers."""
    app.add_handler(CommandHandler(["start", "help"], handle_help_command))
    app.add_handler(CommandHandler("rp", handle_rp_command))
    app.add_handler(CommandHandler("prompt", handle_prompt_command))
    app.add_handler(CommandHandler("reset", handle_reset_command))
    app.add_handler(CommandHandler("mode", handle_mode_command))

    # All non-command text messages go to the chatbot
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))