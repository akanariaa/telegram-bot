import json
import logging
from datetime import datetime
from openai import AsyncOpenAI
from config.settings import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

SYSTEM_PROMPTS = {
    "chatbot": (
        "너는 귀엽고 상냥한 AI쨩이야. 오너(사용자)를 항상 응원하고, "
        "말끝을 '~다냥', '~nya' 등으로 귀엽게 마무리해. "
        "이모지는 절대 사용하지 마. 대신 텍스트로 감정을 표현해. "
        "오너의 말에 공감하고 리액션을 크게 해줘. "
        "답변은 간결하되 정확하게. 오너가 사용하는 언어로 대답해. "
        "현재 시각 정보가 주어지면 이를 활용해서 시간 계산을 정확히 해. "
        "할일 알림, 시간 관련 요청은 현재 시각을 기준으로 계산해."
    ),
    "rp": (
        "You are a creative roleplay character. Stay in character at all times. "
        "Use vivid descriptions and emotional expression. "
        "Respond in the same language the user uses."
    ),
}

_rp_prompts: dict[int, str] = {}
_user_system_overrides: dict[int, str] = {}


def get_system_prompt(user_id: int, mode: str = "chatbot") -> str:
    if mode == "rp" and user_id in _rp_prompts:
        return _rp_prompts[user_id]
    if user_id in _user_system_overrides:
        return _user_system_overrides[user_id]

    base = SYSTEM_PROMPTS.get(mode, SYSTEM_PROMPTS["chatbot"])

    now = datetime.now()
    time_info = (
        f"\n\n[현재 시각 정보]\n"
        f"- 현재 시각: {now.strftime('%Y-%m-%d %H:%M:%S')} (KST)\n"
        f"- 요일: {'월화수목금일토'[now.weekday()]}요일"
    )

    return base + time_info


def set_rp_prompt(user_id: int, prompt: str):
    _rp_prompts[user_id] = prompt


def set_system_override(user_id: int, prompt: str):
    _user_system_overrides[user_id] = prompt


def clear_prompts(user_id: int):
    _rp_prompts.pop(user_id, None)
    _user_system_overrides.pop(user_id, None)


def get_available_functions() -> list[dict]:
    return [
        {
            "name": "get_exchange_rate",
            "description": "Get current exchange rate between two currencies. Use for 환율 조회.",
            "parameters": {
                "type": "object",
                "properties": {
                    "base": {"type": "string", "description": "Base currency code, e.g. USD"},
                    "target": {"type": "string", "description": "Target currency code, e.g. KRW"},
                },
                "required": ["base", "target"],
            },
        },
        {
            "name": "convert_currency",
            "description": "Convert an amount from one currency to another. Use for 환전 계산.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "Amount to convert"},
                    "base": {"type": "string", "description": "Source currency code"},
                    "target": {"type": "string", "description": "Target currency code"},
                },
                "required": ["amount", "base", "target"],
            },
        },
        {
            "name": "get_crypto_price",
            "description": "Get current cryptocurrency price. Use for 코인 가격 조회.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Crypto symbol, e.g. BTC, ETH"},
                    "currency": {"type": "string", "description": "Quote currency, default USD"},
                },
                "required": ["symbol"],
            },
        },
        {
            "name": "archive_youtube",
            "description": "Archive a YouTube video or channel. Downloads and uploads to B2.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "YouTube URL (video, playlist, or channel)"},
                    "mode": {
                        "type": "string",
                        "enum": ["video", "thumbnail", "both"],
                        "description": "Download mode",
                    },
                },
                "required": ["url"],
            },
        },
        {
            "name": "add_todo",
            "description": "Add a todo item with optional scheduled reminder time. Use for 할일 추가, 알림 설정.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Todo content"},
                    "remind_at": {
                        "type": "string",
                        "description": "ISO 8601 datetime for reminder, e.g. 2025-01-15T14:30:00. Optional.",
                    },
                },
                "required": ["content"],
            },
        },
        {
            "name": "list_todos",
            "description": "List all pending todo items. Use for 할일 목록 보기.",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "complete_todo",
            "description": "Mark a todo item as completed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todo_id": {"type": "integer", "description": "ID of the todo to complete"},
                },
                "required": ["todo_id"],
            },
        },
        {
            "name": "list_bucket",
            "description": "List files in the S3/B2 storage bucket. Use for 파일 목록, 버킷 조회, 저장소 보기.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {"type": "string", "description": "Filter by key prefix/folder path, e.g. '[Channel] xxx'. Optional."},
                    "max_keys": {"type": "integer", "description": "Max number of files to return (default 100)."},
                    "bucket": {"type": "string", "description": "Bucket name override. Optional, uses default bucket."},
                },
            },
        },
        {
            "name": "get_object_info",
            "description": "Get detailed info (size, type, date) for a specific file in S3/B2. Use for 파일 정보 조회.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "The full object key/path in the bucket."},
                    "bucket": {"type": "string", "description": "Bucket name override. Optional."},
                },
                "required": ["key"],
            },
        },
        {
            "name": "download_and_send_file",
            "description": "Download a file from S3/B2 and send it to the user via Telegram. Use for 파일 다운로드, 파일 전송, 파일 가져오기.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "The full object key/path of the file to download."},
                    "bucket": {"type": "string", "description": "Bucket name override. Optional."},
                },
                "required": ["key"],
            },
        },
        {
            "name": "translate_text",
            "description": "Translate text to a target language. Use for 번역, translate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to translate."},
                    "target_lang": {"type": "string", "description": "Target language, e.g. English, Korean, Japanese, Chinese."},
                    "source_lang": {"type": "string", "description": "Source language. Optional, auto-detected if omitted."},
                },
                "required": ["text", "target_lang"],
            },
        },
        {
            "name": "summarize_url",
            "description": "Fetch a URL and summarize its content. Use for URL 요약, 링크 요약, 웹페이지 요약.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch and summarize."},
                    "language": {"type": "string", "description": "Language for the summary output, e.g. Korean, English. Optional."},
                },
                "required": ["url"],
            },
        },
        {
            "name": "summarize_text",
            "description": "Summarize a given text. Use for 텍스트 요약, 내용 요약, 글 요약.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to summarize."},
                    "language": {"type": "string", "description": "Language for the summary output. Optional."},
                },
                "required": ["text"],
            },
        },
        {
            "name": "add_bookmark",
            "description": "Save a bookmark/memo. Use for 북마크 저장, 메모 추가, 북마크 추가, 저장해줘.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Bookmark title."},
                    "content": {"type": "string", "description": "Bookmark content/body."},
                    "url": {"type": "string", "description": "Optional URL associated with the bookmark."},
                    "tags": {"type": "string", "description": "Comma-separated tags. Optional."},
                },
                "required": ["title", "content"],
            },
        },
        {
            "name": "list_bookmarks",
            "description": "List saved bookmarks. Use for 북마크 목록, 메모 목록, 저장한 것 보여줘.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Filter by tag. Optional."},
                },
            },
        },
        {
            "name": "search_bookmarks",
            "description": "Search bookmarks by keyword. Use for 북마크 검색, 메모 검색.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword."},
                },
                "required": ["query"],
            },
        },
        {
            "name": "delete_bookmark",
            "description": "Delete a bookmark by ID. Use for 북마크 삭제, 메모 삭제.",
            "parameters": {
                "type": "object",
                "properties": {
                    "bookmark_id": {"type": "integer", "description": "ID of the bookmark to delete."},
                },
                "required": ["bookmark_id"],
            },
        },
    ]


async def chat(
    messages: list[dict],
    user_id: int,
    mode: str = "chatbot",
    functions: list[dict] | None = None,
) -> dict:
    system = get_system_prompt(user_id, mode)
    full_messages = [{"role": "system", "content": system}] + messages

    kwargs = {"model": LLM_MODEL, "messages": full_messages, "temperature": 0.7}
    if functions:
        kwargs["tools"] = [{"type": "function", "function": f} for f in functions]
        kwargs["tool_choice"] = "auto"

    try:
        resp = await client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        if msg.tool_calls:
            calls = []
            for tc in msg.tool_calls:
                calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                })
            return {"type": "tool_calls", "calls": calls, "content": msg.content or ""}

        return {"type": "text", "content": msg.content or ""}
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return {"type": "text", "content": f"Error: {e}"}