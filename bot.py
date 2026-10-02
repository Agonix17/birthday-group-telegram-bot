from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import gspread
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("birthday-bot")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
DATA_FILE = Path(os.getenv("DELIVERY_FILE", "data/delivery.json"))


@dataclass(frozen=True)
class Person:
    name: str
    birth_date: date
    preferred_name: str | None = None


class ConfigError(RuntimeError):
    pass


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Не задана переменная окружения {name}")
    return value


def parse_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value.strip())


def get_timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("TIMEZONE", "Asia/Novosibirsk"))


def admin_ids() -> set[int]:
    result: set[int] = set()
    for raw in os.getenv("ADMIN_USER_IDS", "").split(","):
        if raw.strip():
            result.add(int(raw.strip()))
    return result


def load_delivery() -> tuple[int | None, int | None]:
    chat_id = parse_int(os.getenv("TARGET_CHAT_ID"))
    thread_id = parse_int(os.getenv("MESSAGE_THREAD_ID"))
    if DATA_FILE.exists():
        try:
            saved = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            chat_id = saved.get("chat_id", chat_id)
            thread_id = saved.get("thread_id", thread_id)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Не удалось прочитать %s: %s", DATA_FILE, exc)
    return chat_id, thread_id


def save_delivery(chat_id: int, thread_id: int | None) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps({"chat_id": chat_id, "thread_id": thread_id}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-zа-яё0-9+]", "", value.lower())


def parse_birth_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"неизвестный формат даты: {text!r}")


def read_people() -> list[Person]:
    credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "service-account.json")
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if credentials_json:
        info = json.loads(credentials_json)
        credentials = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        credentials = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)

    client = gspread.authorize(credentials)
    sheet = client.open_by_url(required("GOOGLE_SHEET_URL"))
    worksheet = sheet.worksheet(os.getenv("GOOGLE_WORKSHEET", "Лист1"))
    rows = worksheet.get_all_records()
    people: list[Person] = []
    for number, row in enumerate(rows, start=2):
        normalized = {normalize_key(str(k)): v for k, v in row.items()}
        name = str(normalized.get("name") or normalized.get("имя") or "").strip()
        preferred_name = str(normalized.get("name+") or normalized.get("имя+") or "").strip()
        raw_date = normalized.get("birthdate") or normalized.get("датырождения")
        active = normalized.get("active") or normalized.get("активен")
        if not name and not raw_date:
            continue
        if str(active).strip().lower() in {"false", "0", "no", "нет", "inactive"}:
            continue
        if not name or not raw_date:
            log.warning("Строка %s пропущена: нужны name и birth_date", number)
            continue
        try:
            people.append(
                Person(
                    name=name,
                    birth_date=parse_birth_date(raw_date),
                    preferred_name=preferred_name or None,
                )
            )
        except ValueError as exc:
            log.warning("Строка %s пропущена: %s", number, exc)
    return people


def birthday_on(person: Person, year: int) -> date:
    # В невисокосный год день рождения 29 февраля считаем 28 февраля.
    if person.birth_date.month == 2 and person.birth_date.day == 29:
        try:
            return date(year, 2, 29)
        except ValueError:
            return date(year, 2, 28)
    return date(year, person.birth_date.month, person.birth_date.day)


def age_on(person: Person, event_date: date) -> int:
    return event_date.year - person.birth_date.year


def age_word(age: int) -> str:
    if age % 100 in {11, 12, 13, 14}:
        return "лет"
    if age % 10 == 1:
        return "год"
    if age % 10 in {2, 3, 4}:
        return "года"
    return "лет"


def age_phrase(age: int) -> str:
    return f"{age} {age_word(age)}"


def days_phrase(days: int) -> str:
    if days % 100 in {11, 12, 13, 14}:
        word = "дней"
    elif days % 10 == 1:
        word = "день"
    elif days % 10 in {2, 3, 4}:
        word = "дня"
    else:
        word = "дней"
    return f"{days} {word}"


MONTH_NAMES = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def build_report(people: list[Person], today: date | None = None) -> str:
    today = today or datetime.now(get_timezone()).date()
    upcoming: list[tuple[int, date, Person]] = []
    for person in people:
        event = birthday_on(person, today.year)
        if event < today:
            event = birthday_on(person, today.year + 1)
        upcoming.append(((event - today).days, event, person))
    upcoming.sort(key=lambda item: (item[0], item[1], item[2].name.casefold()))

    today_people = [item for item in upcoming if item[0] == 0]
    lines = [f"🎂 Дни рождения — {today.strftime('%d.%m.%Y')}\n"]
    if today_people:
        lines.append("🎉 Сегодня день рождения:")
        for _, event, person in today_people:
            lines.append(
                f"• {html.escape(person.name)} — исполняется "
                f"{age_phrase(age_on(person, event))}"
            )
    else:
        lines.append("Сегодня дней рождения нет.")

    next_people = [item for item in upcoming if item[0] > 0][:5]
    if next_people:
        lines.append("\n📅 Ближайшие дни рождения:")
        for days, event, person in next_people:
            lines.append(
                f"• {html.escape(person.name)} — через {days_phrase(days)}, "
                f"{event.day} {MONTH_NAMES[event.month - 1]} "
                f"({age_phrase(age_on(person, event))})"
            )
    else:
        lines.append("\nБлижайших дней рождения нет.")
    return "\n".join(lines)


def build_today_report(people: list[Person], today: date | None = None) -> str | None:
    """Return an automatic greeting only when someone has a birthday today."""
    today = today or datetime.now(get_timezone()).date()
    birthday_people = [
        person
        for person in people
        if birthday_on(person, today.year) == today
    ]
    if not birthday_people:
        return None

    lines: list[str] = []
    for person in sorted(birthday_people, key=lambda item: item.name.casefold()):
        display_name = html.escape(person.preferred_name or person.name)
        age = age_on(person, today)
        verb = "исполнился" if age_word(age) == "год" else "исполнилось"
        lines.extend(
            [
                f"🎉 <b>Поздравляем <u>{display_name}</u>!</b>",
                "",
                f"Сегодня, {today.day} {MONTH_NAMES[today.month - 1]} {today.year} года, "
                f"ему {verb} <u>{age_phrase(age)}</u>.",
            ]
        )
    return "\n".join(lines)


def build_next_report(people: list[Person], today: date | None = None) -> str:
    today = today or datetime.now(get_timezone()).date()
    upcoming: list[tuple[int, date, Person]] = []
    for person in people:
        event = birthday_on(person, today.year)
        if event < today:
            event = birthday_on(person, today.year + 1)
        upcoming.append(((event - today).days, event, person))

    upcoming = [item for item in upcoming if item[0] > 0]
    if not upcoming:
        return "В таблице пока нет дней рождения."

    lines = [
        f"<b>📅 Ближайшие дни рождения на <u>{today.strftime('%d.%m.%Y')}</u>:</b>",
        "",
    ]
    for days, event, person in sorted(
        upcoming,
        key=lambda item: (item[0], item[1], item[2].name.casefold()),
    )[:3]:
        lines.append(
            f"• {html.escape(person.name)} — через {days_phrase(days)}, "
            f"{event.day} {MONTH_NAMES[event.month - 1]} "
            f"({age_phrase(age_on(person, event))})"
        )
    return "\n".join(lines)


router = Router()


async def can_manage(message: Message) -> bool:
    user = message.from_user
    if user and user.id in admin_ids():
        return True
    # In a private chat the sender controls their own setup. In a group,
    # verify the sender's real Telegram administrator status.
    if not message.chat or not message.from_user:
        return False
    if message.chat.type == "private":
        return message.from_user.id == message.chat.id
    if message.chat.type in {"group", "supergroup"}:
        try:
            member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
            return member.status in {"creator", "administrator"}
        except Exception:
            log.exception("Не удалось проверить права администратора")
    return False


@router.message(Command("start"))
async def start(message: Message) -> None:
    await message.answer(
        "Привет! Я публикую ежедневную сводку дней рождения из Google Sheets.\n"
        "В группе администратор может выполнить /setup в нужной теме."
    )


@router.message(Command("setup"))
async def setup(message: Message) -> None:
    if not await can_manage(message):
        await message.answer("Настраивать публикацию может только администратор.")
        return
    save_delivery(message.chat.id, message.message_thread_id)
    where = f"в теме {message.message_thread_id}" if message.message_thread_id else "в этом чате"
    await message.answer(f"Готово: ежедневная сводка будет отправляться {where}.")


@router.message(Command("where"))
async def where(message: Message) -> None:
    chat_id, thread_id = load_delivery()
    await message.answer(f"Чат: {chat_id or 'не настроен'}; тема: {thread_id or 'общая'}")


@router.message(Command("today", "birthdays"))
async def today(message: Message) -> None:
    try:
        await message.answer(build_report(read_people()))
    except Exception:
        log.exception("Ошибка формирования отчёта")
        await message.answer("Не удалось прочитать Google Sheets. Проверьте настройки и доступ таблицы.")


@router.message(Command("birthday", "todaybirthday"))
async def birthday(message: Message) -> None:
    """Manually show today's birthday greeting, if anyone has a birthday today."""
    try:
        report = build_today_report(read_people())
        await message.answer(report or "Сегодня именинников нет.")
    except Exception:
        log.exception("Ошибка формирования поздравления")
        await message.answer("Не удалось прочитать Google Sheets. Проверьте настройки и доступ таблицы.")


@router.message(Command("next", "nextbirthday"))
async def next_birthday(message: Message) -> None:
    try:
        await message.answer(build_next_report(read_people()))
    except Exception:
        log.exception("Ошибка формирования ближайшего дня рождения")
        await message.answer("Не удалось прочитать Google Sheets. Проверьте настройки и доступ таблицы.")


@router.message(Command("test"))
async def test(message: Message) -> None:
    if not await can_manage(message):
        await message.answer("Тестовую публикацию может запустить только администратор.")
        return
    chat_id, thread_id = load_delivery()
    if chat_id is None:
        await message.answer("Сначала выполните /setup в нужном чате или задайте TARGET_CHAT_ID.")
        return
    try:
        await message.bot.send_message(chat_id, build_report(read_people()), message_thread_id=thread_id)
        await message.answer("Тестовое сообщение отправлено.")
    except Exception:
        log.exception("Ошибка тестовой отправки")
        await message.answer("Не удалось отправить тестовое сообщение. Проверьте права бота.")


async def scheduler(bot: Bot) -> None:
    hour = int(os.getenv("SEND_HOUR", "0"))
    minute = int(os.getenv("SEND_MINUTE", "0"))
    tz = get_timezone()
    last_sent: date | None = None
    while True:
        now = datetime.now(tz)
        chat_id, thread_id = load_delivery()
        if now.hour == hour and now.minute == minute and last_sent != now.date() and chat_id is not None:
            try:
                report = build_today_report(read_people(), now.date())
                if report is not None:
                    await bot.send_message(chat_id, report, message_thread_id=thread_id)
                    log.info("Поздравление отправлено в chat_id=%s thread_id=%s", chat_id, thread_id)
                else:
                    log.info("На %s именинников нет, сообщение не отправляется", now.date())
                last_sent = now.date()
            except Exception:
                log.exception("Не удалось отправить ежедневную сводку")
        await asyncio.sleep(20)


async def main() -> None:
    token = required("BOT_TOKEN")
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    scheduler_task = asyncio.create_task(scheduler(bot))
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        scheduler_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (ConfigError, ValueError) as exc:
        raise SystemExit(f"Ошибка конфигурации: {exc}") from exc
