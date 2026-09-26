"""Telegram bot command and message handlers registration."""

import logging
import tempfile
import os

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
    _send_reply,
)
from bot.modules.todo import check_reminders

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("help", "명령어 목록을 보여준다냥"),
    BotCommand("rp", "RP 모드를 활성화한다냥 (캐릭터 설정)"),
    BotCommand("prompt", "시스템 프롬프트를 오버라이드한다냥"),
    BotCommand("reset", "프롬프트 및 대화를 초기화한다냥"),
    BotCommand("mode", "현재 모드를 확인한다냥"),
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


async def handle_document(update, context) -> None:
    """Handle PDF document uploads — download, extract text, and summarize."""
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".pdf"):
        await _send_reply(update, "PDF 파일만 처리할 수 있다냥. PDF를 보내줘 nya.")
        return

    await _send_reply(update, "PDF를 분석하고 있다냥... 잠깐만 기다려줘 nya.")

    tmp_path = None
    try:
        tg_file = await doc.get_file()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
            await tg_file.download_to_drive(tmp_path)

        from bot.modules.summarizer import summarize_pdf_file
        import json
        result_json = await summarize_pdf_file(tmp_path)
        result = json.loads(result_json)

        if result.get("status") == "ok":
            pages = result.get("pages", "?")
            text = (
                f"<b>PDF 요약 결과</b> 다냥 ({pages}페이지)\n\n"
                f"{result['summary']}"
            )
        else:
            text = f"PDF 요약 실패했어 nya: {result.get('error', '알 수 없는 오류')}"

        await _send_reply(update, text)
    except Exception as e:
        logger.error("PDF handling failed: %s", e)
        await _send_reply(update, f"PDF 처리 중 오류가 발생했어 nya: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def register_handlers(app: Application) -> None:
    """Register all command and message handlers."""
    app.add_handler(CommandHandler(["start", "help"], handle_help_command))
    app.add_handler(CommandHandler("rp", handle_rp_command))
    app.add_handler(CommandHandler("prompt", handle_prompt_command))
    app.add_handler(CommandHandler("reset", handle_reset_command))
    app.add_handler(CommandHandler("mode", handle_mode_command))

    # PDF document handler (must be before text handler)
    app.add_handler(MessageHandler(filters.Document.PDF, handle_document))

    # All non-command text messages go to the chatbot
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))