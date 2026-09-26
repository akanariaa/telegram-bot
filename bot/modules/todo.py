"""Telegram bot todo management module with scheduled reminders."""

from datetime import datetime, timedelta

from bot.services.database import (
    add_todo,
    complete_todo,
    get_due_reminders,
    list_todos,
    mark_reminded,
)


def parse_relative_time(text: str) -> str | None:
    """Parse Korean relative times into ISO datetime strings.

    Handles common patterns like:
    - "30분 후", "10분 뒤"
    - "1시간 후", "2시간 뒤"
    - "내일", "내일 오후 3시", "모레"
    - "오늘 오후 5시"
    """
    now = datetime.now()
    text = text.strip()

    # Patterns: N분 후/뒤
    if "분" in text:
        for word in ["후", "뒤"]:
            if word in text:
                try:
                    minutes = int(text.split("분")[0].strip())
                    target = now + timedelta(minutes=minutes)
                    return target.isoformat()
                except ValueError:
                    pass

    # Patterns: N시간 후/뒤
    if "시간" in text:
        for word in ["후", "뒤"]:
            if word in text:
                try:
                    hours = int(text.split("시간")[0].strip())
                    target = now + timedelta(hours=hours)
                    return target.isoformat()
                except ValueError:
                    pass

    # Patterns: 내일/모레 + optional time
    base_date = None
    if "모레" in text:
        base_date = (now + timedelta(days=2)).date()
    elif "내일" in text:
        base_date = (now + timedelta(days=1)).date()
    elif "오늘" in text:
        base_date = now.date()

    if base_date is not None:
        # Try to extract a specific time like "오후 3시", "오전 10시 30분"
        hour = 9  # default morning
        minute = 0

        if "오후" in text:
            time_part = text.split("오후")[1]
            hour, minute = _parse_korean_time(time_part)
            if hour < 12:
                hour += 12
        elif "오전" in text:
            time_part = text.split("오전")[1]
            hour, minute = _parse_korean_time(time_part)
            if hour == 12:
                hour = 0

        target = datetime.combine(base_date, datetime.min.time().replace(
            hour=hour, minute=minute,
        ))
        return target.isoformat()

    return None


def _parse_korean_time(text: str) -> tuple[int, int]:
    """Extract hour and minute from Korean time text like '3시 30분'."""
    hour = 9
    minute = 0

    if "시" in text:
        try:
            hour = int(text.split("시")[0].strip())
        except ValueError:
            pass
    if "분" in text:
        try:
            minute_part = text.split("시")[1] if "시" in text else text
            minute = int(minute_part.split("분")[0].strip())
        except ValueError:
            pass

    return hour, minute


async def handle_add_todo(content: str, remind_at: str | None, user_id: int) -> str:
    """Add a new todo item and return a confirmation message."""
    todo_id = add_todo(
        user_id=user_id,
        content=content,
        remind_at=remind_at,
    )

    lines = [f"할 일 추가했어냥!", f"", f"{content}"]
    if remind_at:
        lines.append(f"알림 시간: {remind_at}")
    lines.append(f"ID: {todo_id}")

    return "\n".join(lines)


async def handle_list_todos(user_id: int) -> str:
    """Return a formatted list of pending todos for the user."""
    todos = list_todos(user_id=user_id)

    if not todos:
        return "등록된 할 일이 없어 nya!\n\n할 일을 추가하려면 자연어로 \"할일 추가해줘\"라고 말해줘 다냥."

    lines = ["<b>할 일 목록</b> 다냥\n"]
    for todo in todos:
        status = "[ ]"
        remind_info = ""
        if todo.get("remind_at"):
            remind_info = f" (알림: {todo['remind_at']})"
        lines.append(f"{status} <code>{todo['id']}</code> {todo['content']}{remind_info}")

    lines.append(f"\n총 {len(todos)}개의 할 일이 있어 nya.")
    lines.append("완료하려면 \"할일 완료\"라고 말해줘 다냥.")

    return "\n".join(lines)


async def handle_complete_todo(todo_id: int, user_id: int) -> str:
    """Mark a todo as completed and return a confirmation message."""
    success = complete_todo(todo_id=todo_id, user_id=user_id)

    if success:
        return f"할 일 완료했어냥! 수고했어 nya!\n\nID <code>{todo_id}</code> 완료 처리되었어 다냥."
    else:
        return f"ID <code>{todo_id}</code> 할 일을 못 찾았거나 이미 완료된 거 같아 nya."


async def check_reminders(application) -> None:
    """Check for due reminders and send them via Telegram.

    Called periodically by the job queue (APScheduler / python-telegram-bot).
    """
    reminders = get_due_reminders()

    for reminder in reminders:
        user_id = reminder["user_id"]
        content = reminder["content"]
        reminder_id = reminder["id"]

        message = (
            f"<b>할 일 알림이다냥!</b>\n\n"
            f"{content}\n\n"
            f"잊지 말아줘 nya!"
        )

        try:
            await application.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode="HTML",
            )
        except Exception:
            pass

        mark_reminded(reminder_id)