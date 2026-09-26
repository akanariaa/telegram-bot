"""
Chatbot module — default message handler for the Telegram bot.

Modes:
  • chatbot  (default) — general assistant
  • rp       — roleplay, activated via /rp <character_description>

Commands:
  /rp <description>   — activate RP mode with a character prompt
  /prompt <prompt>     — override the system prompt entirely
  /reset               — clear all prompts and chat history
  /mode                — show current mode info
"""

from __future__ import annotations

import json
import logging
import html as html_mod
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from bot.services import llm, database
from bot.utils.helpers import chunk_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAX_TOOL_CALL_DEPTH = 5  # safety limit for chained tool calls


def _user_id(update: Update) -> int:
    return update.effective_user.id


def _determine_mode(user_id: int) -> str:
    """Return 'rp' if the user has an RP prompt set, else 'chatbot'."""
    from bot.services.llm import _rp_prompts  # noqa: WPS450
    return "rp" if user_id in _rp_prompts else "chatbot"


async def _send_reply(update: Update, text: str) -> None:
    """Send *text* back to the user, chunking if it exceeds Telegram's limit.

    Tries HTML parse mode first. If it fails (e.g. LLM returned unescaped
    HTML special chars), escapes the text and retries as plain text.
    """
    for chunk in chunk_text(text, max_len=4000):
        try:
            await update.message.reply_text(chunk, parse_mode="HTML")
        except BadRequest:
            safe = html_mod.escape(chunk)
            await update.message.reply_text(safe, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Function-call dispatch
# ---------------------------------------------------------------------------

async def dispatch_function_call(
    name: str,
    arguments: dict[str, Any],
    user_id: int,
    application=None,
) -> str:
    """Route a tool/function call to the appropriate module and return the result as a string."""

    # --- Todo functions (backed by the database) --------------------------
    if name == "add_todo":
        from bot.services.database import add_todo
        remind_at = arguments.get("remind_at")
        todo_id = add_todo(user_id, arguments["content"], remind_at)
        return json.dumps({"status": "created", "todo_id": todo_id})

    if name == "list_todos":
        from bot.services.database import list_todos
        todos = list_todos(user_id)
        return json.dumps({"todos": todos})

    if name == "complete_todo":
        from bot.services.database import complete_todo
        ok = complete_todo(user_id, int(arguments["todo_id"]))
        return json.dumps({"status": "completed" if ok else "not_found"})

    # --- Currency / exchange-rate functions --------------------------------
    if name == "get_exchange_rate":
        from bot.modules.finance import get_exchange_rate
        result = await get_exchange_rate(arguments["base"], arguments["target"])
        return result

    if name == "convert_currency":
        from bot.modules.finance import convert_currency
        result = await convert_currency(
            arguments["amount"], arguments["base"], arguments["target"]
        )
        return result

    # --- Crypto functions --------------------------------------------------
    if name == "get_crypto_price":
        from bot.modules.finance import get_crypto_price
        result = await get_crypto_price(
            arguments["symbol"], arguments.get("currency", "usd")
        )
        return result

    # --- YouTube archive ---------------------------------------------------
    if name == "archive_youtube":
        from bot.modules.youtube_archive import archive_youtube
        result = await archive_youtube(
            arguments["url"], arguments.get("mode", "both")
        )
        return result

    # --- S3/B2 storage -----------------------------------------------------
    if name == "list_bucket":
        from bot.modules.storage import list_bucket
        result = await list_bucket(
            prefix=arguments.get("prefix", ""),
            max_keys=arguments.get("max_keys", 100),
            bucket=arguments.get("bucket"),
        )
        return result

    if name == "get_object_info":
        from bot.modules.storage import get_object_info
        result = await get_object_info(
            key=arguments["key"],
            bucket=arguments.get("bucket"),
        )
        return result

    if name == "download_and_send_file":
        from bot.modules.storage import download_and_send
        result = await download_and_send(
            key=arguments["key"],
            application=application,
            chat_id=user_id,
            bucket=arguments.get("bucket"),
        )
        return result

    # --- Translation -------------------------------------------------------
    if name == "translate_text":
        from bot.modules.translator import translate_text
        result = await translate_text(
            text=arguments["text"],
            target_lang=arguments["target_lang"],
            source_lang=arguments.get("source_lang", ""),
        )
        return result

    # --- Summarization -----------------------------------------------------
    if name == "summarize_url":
        from bot.modules.summarizer import summarize_url
        result = await summarize_url(
            url=arguments["url"],
            language=arguments.get("language", ""),
        )
        return result

    if name == "summarize_text":
        from bot.modules.summarizer import summarize_text
        result = await summarize_text(
            text=arguments["text"],
            language=arguments.get("language", ""),
        )
        return result

    # --- Bookmarks ---------------------------------------------------------
    if name == "add_bookmark":
        from bot.modules.bookmark import add_bookmark
        result = add_bookmark(
            user_id=user_id,
            title=arguments["title"],
            content=arguments["content"],
            url=arguments.get("url", ""),
            tags=arguments.get("tags", ""),
        )
        return result

    if name == "list_bookmarks":
        from bot.modules.bookmark import list_bookmarks
        result = list_bookmarks(user_id, tag=arguments.get("tag", ""))
        return result

    if name == "search_bookmarks":
        from bot.modules.bookmark import search_bookmarks
        result = search_bookmarks(user_id, query=arguments["query"])
        return result

    if name == "delete_bookmark":
        from bot.modules.bookmark import delete_bookmark
        result = delete_bookmark(user_id, bookmark_id=int(arguments["bookmark_id"]))
        return result

    # --- Unknown function --------------------------------------------------
    logger.warning("Unknown function call requested: %s", name)
    return json.dumps({"error": f"Unknown function: {name}"})


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def handle_rp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rp <character_description> — activate roleplay mode."""
    uid = _user_id(update)
    args = context.args

    if not args:
        await _send_reply(
            update,
            "사용법: <code>/rp &lt;캐릭터 설명&gt;</code> 다냥\n"
            "예시: <code>/rp 해적 말투로 대화해</code> nya",
        )
        return

    prompt = " ".join(args)
    llm.set_rp_prompt(uid, prompt)
    database.clear_chat_history(uid)

    await _send_reply(
        update,
        f"RP 모드 활성화했어냥!\n\n"
        f"<b>캐릭터 프롬프트:</b>\n{prompt}\n\n"
        f"대화 기록이 초기화되었어 nya. 이제부터 이 캐릭터로 대화하자냥!",
    )


async def handle_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/prompt <system_prompt> — override the system prompt."""
    uid = _user_id(update)
    args = context.args

    if not args:
        await _send_reply(
            update,
            "사용법: <code>/prompt &lt;시스템 프롬프트&gt;</code> 다냥\n"
            "기본 시스템 프롬프트를 덮어씌울 수 있어 nya.",
        )
        return

    prompt = " ".join(args)
    llm.set_system_override(uid, prompt)
    database.clear_chat_history(uid)

    await _send_reply(
        update,
        f"시스템 프롬프트 변경 완료했어냥!\n\n"
        f"<b>새 프롬프트:</b>\n{prompt}\n\n"
        f"대화 기록이 초기화되었어 nya.",
    )


async def handle_reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reset — clear all prompts and chat history."""
    uid = _user_id(update)
    llm.clear_prompts(uid)
    database.clear_chat_history(uid)
    await _send_reply(update, "초기화 완료했어냥! 프롬프트랑 대화 기록 다 날렸어 nya.")


async def handle_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mode — show current mode info."""
    uid = _user_id(update)
    mode = _determine_mode(uid)

    from bot.services.llm import _rp_prompts, _user_system_overrides  # noqa: WPS450

    lines = [f"<b>현재 모드:</b> <code>{mode}</code> 다냥"]

    if uid in _rp_prompts:
        lines.append(f"<b>RP 프롬프트:</b> {_rp_prompts[uid]} nya")
    if uid in _user_system_overrides:
        lines.append(f"<b>시스템 오버라이드:</b> {_user_system_overrides[uid]} nya")

    await _send_reply(update, "\n".join(lines))


async def handle_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — show all available commands."""
    text = (
        "<b>명령어 목록</b> 다냥\n"
        "─────────────────\n\n"
        "<b>챗봇 / RP</b>\n"
        "  <code>/rp &lt;캐릭터 설명&gt;</code> RP 모드 활성화\n"
        "  <code>/prompt &lt;프롬프트&gt;</code> 시스템 프롬프트 오버라이드\n"
        "  <code>/reset</code> 프롬프트 및 대화 초기화\n"
        "  <code>/mode</code> 현재 모드 확인\n\n"
        "<b>금융</b> (자연어로도 가능해 nya)\n"
        "  \"USD 환율 알려줘\" 환율 조회\n"
        "  \"100달러를 원화로\" 환전 계산\n"
        "  \"비트코인 가격\" 코인 시세 조회\n\n"
        "<b>저장소</b> (자연어로도 가능해 nya)\n"
        "  \"버킷 파일 목록\" S3/B2 파일 목록\n"
        "  \"파일 정보 알려줘\" 파일 상세 조회\n"
        "  \"파일 다운로드해줘\" 파일 전송\n\n"
        "<b>YouTube 아카이빙</b> (자연어로도 가능해 nya)\n"
        "  \"이 유튜브 영상 아카이브해줘\" + 링크\n\n"
        "<b>할일 관리</b> (자연어로도 가능해 nya)\n"
        "  \"할일 추가해줘\" 할일 등록\n"
        "  \"할일 목록\" 목록 보기\n"
        "  \"할일 완료\" 완료 처리\n"
        "  \"30분 후에 알려줘\" 예약 알림\n\n"
        "<b>번역</b> (자연어로도 가능해 nya)\n"
        "  \"이 문장 영어로 번역해줘\" + 텍스트\n"
        "  \"일본어로 번역\" + 텍스트\n\n"
        "<b>요약</b> (자연어로도 가능해 nya)\n"
        "  \"이 URL 요약해줘\" + 링크\n"
        "  \"이 텍스트 요약해줘\" + 텍스트\n"
        "  PDF 파일을 보내면 자동으로 요약해준다냥\n\n"
        "<b>북마크 / 메모</b> (자연어로도 가능해 nya)\n"
        "  \"이거 북마크해줘\" 내용 저장\n"
        "  \"북마크 목록\" 저장된 목록 보기\n"
        "  \"북마크 검색\" 키워드로 검색\n"
        "  \"북마크 삭제\" 삭제\n\n"
        "─────────────────\n"
        "명령어 없이 메시지를 보내면 챗봇 모드로 동작한다냥.\n"
        "LLM이 자연어를 분석해서 위 기능들을 자동으로 실행해준다 nya."
    )
    await _send_reply(update, text)


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process an incoming chat message through the LLM pipeline.

    Flow:
      1. Persist the user message to the database.
      2. Load recent chat history.
      3. Call the LLM (with available tool definitions).
      4. If the LLM returns tool_calls, execute them, feed results back,
         and repeat until a text response is produced (up to MAX_TOOL_CALL_DEPTH).
      5. Persist and send the final text response.
    """
    uid = _user_id(update)
    user_text = update.message.text

    if not user_text:
        return

    # 1. Save user message
    database.add_chat_message(uid, "user", user_text)

    # 2. Load history
    history = database.get_chat_history(uid, limit=30)
    mode = _determine_mode(uid)
    functions = llm.get_available_functions()

    # 3–4. LLM call loop (handles chained tool calls)
    for _ in range(MAX_TOOL_CALL_DEPTH):
        response = await llm.chat(
            messages=history,
            user_id=uid,
            mode=mode,
            functions=functions,
        )

        if response["type"] == "text":
            # Final text response — save and send
            assistant_text = response["content"]
            if assistant_text:
                database.add_chat_message(uid, "assistant", assistant_text)
                await _send_reply(update, assistant_text)
            return

        if response["type"] == "tool_calls":
            # Append the assistant's partial message (may include content)
            if response.get("content"):
                database.add_chat_message(uid, "assistant", response["content"])

            # Execute each tool call and collect results
            for call in response["calls"]:
                logger.info(
                    "Dispatching tool call: %s(%s) for user %s",
                    call["name"],
                    call["arguments"],
                    uid,
                )
                result = await dispatch_function_call(
                    name=call["name"],
                    arguments=call["arguments"],
                    user_id=uid,
                    application=context.application,
                )

                # Record the tool result in history so the LLM can see it
                tool_message = (
                    f"[Tool result for {call['name']}]\n{result}"
                )
                database.add_chat_message(uid, "user", tool_message)
                history.append({"role": "user", "content": tool_message})

            # Refresh history and loop back for the LLM's next response
            history = database.get_chat_history(uid, limit=30)
            continue

        # Unexpected response type — bail out
        logger.error("Unexpected LLM response type: %s", response.get("type"))
        await _send_reply(update, "뭔가 잘못된 것 같아 nya. 다시 시도해줘 다냥.")
        return

    # Exhausted tool-call iterations
    logger.warning("Tool-call depth limit reached for user %s", uid)
    await _send_reply(update, "너무 많은 도구 호출이 이어졌어 nya. 다시 시도해줘 다냥.")