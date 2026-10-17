import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bot.sqlite3"
API_URL = "https://be-svitlo.oe.if.ua/schedule-by-queue?queue={}"
VALID_QUEUES = {f"{number}.{group}" for number in range(1, 7) for group in range(1, 3)}
POLL_INTERVAL_SECONDS = 10 * 60
TIMEOUT_SECONDS = 30
KYIV = ZoneInfo("Europe/Kyiv")

if load_dotenv:
    load_dotenv(BASE_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def connect_db():
    import sqlite3

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE IF NOT EXISTS subscriptions (chat_id INTEGER NOT NULL, queue TEXT NOT NULL, "
        "PRIMARY KEY (chat_id, queue))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schedules (queue TEXT NOT NULL, event_date TEXT NOT NULL, "
        "payload TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY (queue, event_date))"
    )
    connection.commit()
    return connection


def normalize_queue(value: str) -> str | None:
    value = value.strip().replace(" ", "")
    if "." not in value:
        value = f"{value}.1"
    return value if value in VALID_QUEUES else None


def parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%d.%m.%Y")


def format_intervals(items: list[dict[str, Any]]) -> str:
    active = [item.get("shutdownHours") for item in items if item.get("status") == 1]
    return ", ".join(active) if active else "без планових відключень"


def today_schedule_status(items: list[dict[str, Any]], event_date: str) -> tuple[str, str]:
    planned = [item for item in items if item.get("status") == 1 and item.get("shutdownHours")]
    if not planned:
        return "🟢 Планових відключень немає", "без планових відключень"
    if event_date != datetime.now(KYIV).strftime("%d.%m.%Y"):
        return "🔴 Є планові відключення", format_intervals(items)

    now = datetime.now(KYIV)
    states: list[str] = []
    all_finished = True
    has_current = False
    for item in planned:
        try:
            start_value, end_value = item["shutdownHours"].split("-", maxsplit=1)
            start = datetime.combine(now.date(), datetime.strptime(start_value, "%H:%M").time(), KYIV)
            end = datetime.combine(now.date(), datetime.strptime(end_value, "%H:%M").time(), KYIV)
            if end <= start:
                end += timedelta(days=1)
            if now < start:
                all_finished = False
                states.append(f"{item['shutdownHours']} — заплановано")
            elif now < end:
                all_finished = False
                has_current = True
                states.append(f"{item['shutdownHours']} — триває")
            else:
                states.append(f"{item['shutdownHours']} — завершено")
        except (KeyError, ValueError):
            all_finished = False
            states.append(str(item.get("shutdownHours")))

    if all_finished:
        return "✅ Відключення на сьогодні вже пройшли", ", ".join(states)
    if has_current:
        return "🔴 Відключення триває", ", ".join(states)
    return "🟠 Є заплановані відключення", ", ".join(states)


def format_schedule(queue: str, payload: dict[str, Any], title: str = "Графік") -> str:
    event_date = payload.get("eventDate", "невідома дата")
    intervals = payload.get("queues", {}).get(queue, [])
    status, interval_text = today_schedule_status(intervals, event_date)
    lines = [
        f"⚡ <b>{title}</b>",
        "",
        f"🗓 Дата: <b>{event_date}</b>",
        f"🚦 Черга: <b>{queue}</b>",
        f"📌 Статус: <b>{status}</b>",
        f"⏰ Час: <b>{interval_text}</b>",
    ]
    created = payload.get("createdAt")
    approved = payload.get("scheduleApprovedSince")
    if created:
        lines.extend(["", f"🔄 Оновлено: {created}"])
    if approved:
        lines.append(f"✅ Затверджено з: {approved}")
    return "\n".join(lines)


def human_duration(seconds: int) -> str:
    minutes = max(0, seconds) // 60
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} год {minutes} хв"
    if hours:
        return f"{hours} год"
    return f"{minutes} хв"


def countdown_text(queue: str, payload: dict[str, Any]) -> str:
    today = datetime.now(KYIV).date()
    event_date = parse_date(payload.get("eventDate", "01.01.1900")).date()
    if event_date != today:
        return f"⚡ <b>Черга {queue}</b>\n\nℹ️ На сьогодні графік ще не опубліковано."

    now = datetime.now(KYIV)
    intervals = []
    for item in payload.get("queues", {}).get(queue, []):
        if item.get("status") != 1:
            continue
        try:
            start_value, end_value = item["shutdownHours"].split("-", maxsplit=1)
            start = datetime.combine(today, datetime.strptime(start_value, "%H:%M").time(), KYIV)
            end = datetime.combine(today, datetime.strptime(end_value, "%H:%M").time(), KYIV)
            if end <= start:
                end += timedelta(days=1)
            intervals.append((start, end, item["shutdownHours"]))
        except (KeyError, ValueError):
            continue

    intervals.sort(key=lambda item: item[0])
    for start, end, hours in intervals:
        if now < start:
            return (f"⚡ <b>Черга {queue}</b>\n\n🟢 Світло зараз є\n"
                    f"⏳ До відключення: <b>{human_duration(int((start - now).total_seconds()))}</b>\n"
                    f"🕒 Період: {hours}")
        if start <= now < end:
            return (f"⚡ <b>Черга {queue}</b>\n\n🔴 Світла зараз немає\n"
                    f"⏳ До включення: <b>{human_duration(int((end - now).total_seconds()))}</b>\n"
                    f"🕒 Поточне відключення: {hours}")
    return f"⚡ <b>Черга {queue}</b>\n\n🟢 Світло зараз є\n📌 Сьогодні запланованих відключень більше немає."


async def fetch_schedule(session: aiohttp.ClientSession, queue: str) -> dict[str, Any] | None:
    try:
        async with session.get(API_URL.format(queue), timeout=TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        if not isinstance(data, list) or not data:
            return None
        candidates = [item for item in data if isinstance(item, dict) and item.get("eventDate")]
        if not candidates:
            return None
        tomorrow = (datetime.now(KYIV) + timedelta(days=1)).date()
        dated = [(parse_date(item["eventDate"]).date(), item) for item in candidates]
        exact_tomorrow = [item for date, item in dated if date == tomorrow]
        if exact_tomorrow:
            return exact_tomorrow[-1]
        future = [item for date, item in dated if date > tomorrow]
        if future:
            return min(future, key=lambda item: parse_date(item["eventDate"]))
        return max(candidates, key=lambda item: parse_date(item["eventDate"]))
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
        logger.warning("Не вдалося отримати чергу %s: %s", queue, exc)
        return None


async def fetch_today_schedule(session: aiohttp.ClientSession, queue: str) -> dict[str, Any] | None:
    try:
        async with session.get(API_URL.format(queue), timeout=TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        today = datetime.now(KYIV).date()
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and item.get("eventDate") and parse_date(item["eventDate"]).date() == today:
                return item
        return None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
        logger.warning("Не вдалося отримати сьогоднішню чергу %s: %s", queue, exc)
        return None


async def fetch_tomorrow_schedule(session: aiohttp.ClientSession, queue: str) -> dict[str, Any] | None:
    try:
        async with session.get(API_URL.format(queue), timeout=TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        tomorrow = (datetime.now(KYIV) + timedelta(days=1)).date()
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and item.get("eventDate") and parse_date(item["eventDate"]).date() == tomorrow:
                return item
        return None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
        logger.warning("Не вдалося отримати завтрашню чергу %s: %s", queue, exc)
        return None


def save_and_check_changed(queue: str, payload: dict[str, Any]) -> bool:
    event_date = payload["eventDate"]
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    now = datetime.now(KYIV).isoformat(timespec="seconds")
    with connect_db() as db:
        old = db.execute(
            "SELECT payload FROM schedules WHERE queue = ? AND event_date = ?", (queue, event_date)
        ).fetchone()
        db.execute(
            "INSERT INTO schedules(queue, event_date, payload, fetched_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(queue, event_date) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (queue, event_date, serialized, now),
        )
        if old is None:
            return False
        try:
            previous_payload = json.loads(old["payload"])
        except (TypeError, json.JSONDecodeError):
            return False
        return schedule_signature(previous_payload, queue) != schedule_signature(payload, queue)


def schedule_signature(payload: dict[str, Any], queue: str) -> list[tuple[str, str, str, int]]:
    """Повертає тільки дані про години конкретної черги, без дат і службових полів."""
    intervals = payload.get("queues", {}).get(queue, [])
    signature = []
    for item in intervals:
        signature.append(
            (
                str(item.get("shutdownHours", "")),
                str(item.get("from", "")),
                str(item.get("to", "")),
                int(item.get("status", 0)),
            )
        )
    return sorted(signature)


def subscribed_chats(queue: str) -> list[int]:
    with connect_db() as db:
        rows = db.execute("SELECT chat_id FROM subscriptions WHERE queue = ?", (queue,)).fetchall()
        return [row["chat_id"] for row in rows]


def queue_subscriptions(chat_id: int) -> list[str]:
    with connect_db() as db:
        rows = db.execute("SELECT queue FROM subscriptions WHERE chat_id = ? ORDER BY queue", (chat_id,)).fetchall()
        return [row["queue"] for row in rows]


def add_subscription(chat_id: int, queue: str) -> None:
    with connect_db() as db:
        db.execute("INSERT OR IGNORE INTO subscriptions(chat_id, queue) VALUES (?, ?)", (chat_id, queue))
        db.commit()


def remove_subscription(chat_id: int, queue: str) -> None:
    with connect_db() as db:
        db.execute("DELETE FROM subscriptions WHERE chat_id = ? AND queue = ?", (chat_id, queue))
        db.commit()


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Підписатися"), KeyboardButton(text="➖ Відписатися")],
            [KeyboardButton(text="📋 Мої черги"), KeyboardButton(text="📅 Графік зараз")],
            [KeyboardButton(text="📅 Графік на завтра")],
            [KeyboardButton(text="⏱ До зміни світла")],
            [KeyboardButton(text="ℹ️ Допомога")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def queue_keyboard() -> ReplyKeyboardMarkup:
    queues = sorted(VALID_QUEUES, key=lambda value: (int(value.split(".")[0]), value))
    rows = [[KeyboardButton(text=queue) for queue in queues[index:index + 3]] for index in range(0, len(queues), 3)]
    rows.append([KeyboardButton(text="⬅️ Назад")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


async def get_queue_arg(message: Message) -> str | None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Вкажіть чергу, наприклад: <code>/subscribe 5.2</code>")
        return None
    queue = normalize_queue(parts[1])
    if not queue:
        await message.answer("Черга має бути у форматі X.G, де X — 1–6, G — 1–2. Наприклад: <code>5.2</code>")
    return queue


async def send_schedule(message: Message, queue: str, session: aiohttp.ClientSession, title: str = "Графік") -> None:
    payload = await fetch_schedule(session, queue)
    if payload is None:
        await message.answer("Не вдалося отримати графік. Спробуйте трохи пізніше.")
        return
    await message.answer(format_schedule(queue, payload, title))


async def send_countdown(message: Message, queue: str, session: aiohttp.ClientSession) -> None:
    payload = await fetch_today_schedule(session, queue)
    if payload is None:
        await message.answer(
            f"<b>Черга {queue}</b>\nНе вдалося отримати сьогоднішній графік.",
            reply_markup=main_keyboard(),
        )
        return
    await message.answer(countdown_text(queue, payload), reply_markup=main_keyboard())


async def send_today_schedule(message: Message, queue: str, session: aiohttp.ClientSession) -> None:
    payload = await fetch_today_schedule(session, queue)
    if payload is None:
        await message.answer(
            f"⚡ <b>Черга {queue}</b>\n\nℹ️ Сьогоднішній графік ще не опубліковано.",
            reply_markup=main_keyboard(),
        )
        return
    await message.answer(format_schedule(queue, payload, "Графік на сьогодні"), reply_markup=main_keyboard())


async def send_tomorrow_schedule(message: Message, queue: str, session: aiohttp.ClientSession) -> None:
    payload = await fetch_tomorrow_schedule(session, queue)
    if payload is None:
        await message.answer(
            "📅 <b>Графіку на завтра ще немає.</b>\n\n☀️ Насолоджуйтесь світлом!",
            reply_markup=main_keyboard(),
        )
        return
    await message.answer(format_schedule(queue, payload, "Графік на завтра"), reply_markup=main_keyboard())


async def main() -> None:
    token = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задано BOT_TOKEN у файлі .env")

    connect_db().close()
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    pending_action: dict[int, str] = {}

    @dispatcher.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(
            "Привіт! Я стежу за графіками відключень.\n\n"
            "Користуйтеся кнопками внизу екрана. Також доступні команди "
            "<code>/subscribe 5.2</code>, <code>/unsubscribe 5.2</code>, "
            "<code>/tomorrow 5.2</code> і <code>/subscriptions</code>.",
            reply_markup=main_keyboard(),
        )

    @dispatcher.message(Command("help"))
    async def help_command(message: Message) -> None:
        await start(message)

    @dispatcher.message(Command("subscribe"))
    async def subscribe(message: Message) -> None:
        queue = await get_queue_arg(message)
        if not queue:
            return
        add_subscription(message.chat.id, queue)
        await message.answer(f"✅ Підписку на чергу <b>{queue}</b> увімкнено.\n🔄 Перевіряю оновлення кожні 10 хвилин.")
        async with aiohttp.ClientSession() as session:
            await send_schedule(message, queue, session, "Поточний графік")

    @dispatcher.message(F.text == "➕ Підписатися")
    async def subscribe_button(message: Message) -> None:
        pending_action[message.chat.id] = "subscribe"
        await message.answer("Оберіть чергу:", reply_markup=queue_keyboard())

    @dispatcher.message(Command("unsubscribe"))
    async def unsubscribe(message: Message) -> None:
        queue = await get_queue_arg(message)
        if not queue:
            return
        remove_subscription(message.chat.id, queue)
        await message.answer(f"🔕 Підписку на чергу <b>{queue}</b> вимкнено.")

    @dispatcher.message(F.text == "➖ Відписатися")
    async def unsubscribe_button(message: Message) -> None:
        queues = queue_subscriptions(message.chat.id)
        if not queues:
            await message.answer("У вас немає активних підписок.", reply_markup=main_keyboard())
            return
        pending_action[message.chat.id] = "unsubscribe"
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=queue) for queue in queues], [KeyboardButton(text="⬅️ Назад")]],
            resize_keyboard=True,
            is_persistent=True,
        )
        await message.answer("Оберіть чергу для відписки:", reply_markup=keyboard)

    @dispatcher.message(Command("subscriptions"))
    async def subscriptions(message: Message) -> None:
        queues = queue_subscriptions(message.chat.id)
        await message.answer(
            "Ваші підписки: " + (", ".join(f"<b>{q}</b>" for q in queues) if queues else "немає"),
            reply_markup=main_keyboard(),
        )

    @dispatcher.message(F.text == "📋 Мої черги")
    async def subscriptions_button(message: Message) -> None:
        await subscriptions(message)

    @dispatcher.message(Command("tomorrow"))
    async def tomorrow(message: Message) -> None:
        queue = await get_queue_arg(message)
        if not queue:
            return
        async with aiohttp.ClientSession() as session:
            await send_tomorrow_schedule(message, queue, session)

    @dispatcher.message(F.text == "📅 Графік на завтра")
    async def tomorrow_button(message: Message) -> None:
        queues = queue_subscriptions(message.chat.id)
        if not queues:
            await message.answer("Спочатку підпишіться хоча б на одну чергу.", reply_markup=main_keyboard())
            return
        if len(queues) == 1:
            async with aiohttp.ClientSession() as session:
                await send_tomorrow_schedule(message, queues[0], session)
            return
        pending_action[message.chat.id] = "tomorrow"
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=queue) for queue in queues], [KeyboardButton(text="⬅️ Назад")]],
            resize_keyboard=True,
            is_persistent=True,
        )
        await message.answer("Оберіть чергу:", reply_markup=keyboard)

    @dispatcher.message(F.text == "📅 Графік зараз")
    async def today_button(message: Message) -> None:
        queues = queue_subscriptions(message.chat.id)
        if not queues:
            await message.answer("Спочатку підпишіться хоча б на одну чергу.", reply_markup=main_keyboard())
            return
        if len(queues) == 1:
            async with aiohttp.ClientSession() as session:
                await send_today_schedule(message, queues[0], session)
            return
        pending_action[message.chat.id] = "today"
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=queue) for queue in queues], [KeyboardButton(text="⬅️ Назад")]],
            resize_keyboard=True,
            is_persistent=True,
        )
        await message.answer("Оберіть чергу:", reply_markup=keyboard)

    @dispatcher.message(F.text == "⏱ До зміни світла")
    async def countdown_button(message: Message) -> None:
        queues = queue_subscriptions(message.chat.id)
        if not queues:
            await message.answer("Спочатку підпишіться хоча б на одну чергу.", reply_markup=main_keyboard())
            return
        if len(queues) == 1:
            async with aiohttp.ClientSession() as session:
                await send_countdown(message, queues[0], session)
            return
        pending_action[message.chat.id] = "countdown"
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=queue) for queue in queues], [KeyboardButton(text="⬅️ Назад")]],
            resize_keyboard=True,
            is_persistent=True,
        )
        await message.answer("Оберіть чергу:", reply_markup=keyboard)

    @dispatcher.message(F.text == "ℹ️ Допомога")
    async def help_button(message: Message) -> None:
        await help_command(message)

    @dispatcher.message(F.text == "⬅️ Назад")
    async def back_button(message: Message) -> None:
        pending_action.pop(message.chat.id, None)
        await message.answer("Головне меню.", reply_markup=main_keyboard())

    @dispatcher.message(F.text)
    async def queue_choice(message: Message) -> None:
        action = pending_action.get(message.chat.id)
        queue = normalize_queue(message.text or "")
        if not action or not queue:
            return
        pending_action.pop(message.chat.id, None)
        if action == "subscribe":
            add_subscription(message.chat.id, queue)
            await message.answer(
                f"✅ Підписку на чергу <b>{queue}</b> увімкнено.\n🔄 Перевіряю оновлення кожні 10 хвилин.",
                reply_markup=main_keyboard(),
            )
            async with aiohttp.ClientSession() as session:
                await send_schedule(message, queue, session, "Поточний графік")
        elif action == "unsubscribe":
            remove_subscription(message.chat.id, queue)
            await message.answer(f"🔕 Підписку на чергу <b>{queue}</b> вимкнено.", reply_markup=main_keyboard())
        elif action == "tomorrow":
            async with aiohttp.ClientSession() as session:
                await send_tomorrow_schedule(message, queue, session)
        elif action == "countdown":
            async with aiohttp.ClientSession() as session:
                await send_countdown(message, queue, session)
        elif action == "today":
            async with aiohttp.ClientSession() as session:
                await send_today_schedule(message, queue, session)

    async def monitor() -> None:
        async with aiohttp.ClientSession() as session:
            while True:
                queues = {queue for chat_id in all_chats() for queue in queue_subscriptions(chat_id)}
                for queue in queues:
                    today_payload = await fetch_today_schedule(session, queue)
                    tomorrow_payload = await fetch_tomorrow_schedule(session, queue)
                    for payload, title in (
                        (today_payload, "Графік на сьогодні оновлено"),
                        (tomorrow_payload, "Графік на завтра оновлено"),
                    ):
                        if payload and save_and_check_changed(queue, payload):
                            text = format_schedule(queue, payload, title)
                            for chat_id in subscribed_chats(queue):
                                try:
                                    await bot.send_message(chat_id, text)
                                except Exception as exc:
                                    logger.warning("Не вдалося надіслати оновлення chat_id=%s: %s", chat_id, exc)
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def all_chats() -> list[int]:
        with connect_db() as db:
            rows = db.execute("SELECT DISTINCT chat_id FROM subscriptions").fetchall()
            return [row["chat_id"] for row in rows]

    monitor_task = asyncio.create_task(monitor())
    try:
        await dispatcher.start_polling(bot)
    finally:
        monitor_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
