from __future__ import annotations

import asyncio
import calendar
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
SCHEDULER_STATE_FILE = Path(
    os.getenv("SCHEDULER_STATE_FILE", "data/scheduler_state.json")
)


@dataclass(frozen=True)
class Person:
    name: str
    birth_date: date
    preferred_name: str | None = None
    gender: str = "male"


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


def load_scheduler_state() -> dict[str, dict[str, str]]:
    try:
        state = json.loads(SCHEDULER_STATE_FILE.read_text(encoding="utf-8"))
        return {
            "monthly_sent": state.get("monthly_sent", {}),
            "daily_sent": state.get("daily_sent", {}),
        }
    except FileNotFoundError:
        return {"monthly_sent": {}, "daily_sent": {}}
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        log.warning("Не удалось прочитать состояние расписания: %s", exc)
        return {"monthly_sent": {}, "daily_sent": {}}


def save_scheduler_state(state: dict[str, dict[str, str]]) -> None:
    SCHEDULER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = SCHEDULER_STATE_FILE.with_suffix(
        SCHEDULER_STATE_FILE.suffix + ".tmp"
    )
    temporary_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_file.replace(SCHEDULER_STATE_FILE)


def delivery_key(chat_id: int, thread_id: int | None) -> str:
    return f"{chat_id}:{thread_id or 0}"


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
        gender = str(normalized.get("gender") or normalized.get("пол") or "").strip().lower()
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
                    gender=gender,
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

MONTH_NAMES_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

MONTH_INTROS = {
    1: "🎄 С Новым годом!",
    2: "🛗 С Днём работника лифтового хозяйства!",
    3: "🌸 С первым днём весны!",
    4: "😸 С Днём твоей белой спины!",
    5: "🌼 С Праздником весны и труда!",
    6: "☀️ С первым днём лета!",
    7: "🛖 С Днём добровольного вхождения Бурятии в состав России!",
    8: "💵 С Днём инкассатора!",
    9: "🍁 С Днём знаний!",
    10: "👵 С Днём старшего поколения!",
    11: "💙 С Днём менеджера!",
    12: "❄️ С первым днём зимы!",
}


def month_birthday_report(
    people: list[Person],
    month: int,
    year: int,
    *,
    include_greeting: bool = False,
    reference_date: date | None = None,
) -> str:
    if not 1 <= month <= 12:
        raise ValueError("Номер месяца должен быть от 1 до 12.")
    first_day = reference_date or date(year, month, 1)
    birthdays: list[tuple[int, Person]] = []
    for person in people:
        if person.birth_date.month != month:
            continue
        event_day = min(person.birth_date.day, calendar.monthrange(year, month)[1])
        event_date = date(year, month, event_day)
        birthdays.append((event_day, person))
    birthdays.sort(key=lambda item: (item[0], item[1].name.casefold()))

    month_label = MONTH_NAMES_GENITIVE[month - 1]
    month_heading = month_label
    if include_greeting:
        intro = MONTH_INTROS[month]
        custom_heart_id = os.getenv("CUSTOM_BLUE_HEART_ID", "").strip()
        if month == 11 and custom_heart_id:
            intro = f'<tg-emoji emoji-id="{html.escape(custom_heart_id)}">💙</tg-emoji> С Днём менеджера!'
        heading = f"<b>{intro} Именинники <u>{month_heading}</u>:</b>"
    else:
        heading = f"<b>Именинники <u>{month_heading}</u>:</b>"
    lines = [heading, ""]
    for event_day, person in birthdays:
        event_date = date(year, month, min(event_day, calendar.monthrange(year, month)[1]))
        days = (event_date - first_day).days
        age = year - person.birth_date.year
        if include_greeting:
            timing = "сегодня" if days == 0 else f"через {days_phrase(days)}"
            line = f"• {html.escape(person.name)} — {timing}, {event_day} {month_label} ({age_phrase(age)})"
        else:
            line = f"• {html.escape(person.name)} — {event_day} {month_label} ({age_phrase(age)})"
        lines.append(line)
    if not birthdays:
        lines.append("В этом месяце именинников нет.")
    return "\n".join(lines)


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
        pronoun = "ей" if person.gender in {"female", "f", "женский", "ж", "жен"} else "ему"
        lines.extend(
            [
                f"🎉 <b>Поздравляем <u>{display_name}</u>!</b>",
                "",
                f"Сегодня, {today.day} {MONTH_NAMES[today.month - 1]} {today.year} года, "
                f"{pronoun} {verb} <u>{age_phrase(age)}</u>.",
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


def parse_month(value: str | None, current_month: int) -> int:
    if not value:
        return current_month
    normalized = value.strip().lower()
    if normalized.isdigit() and 1 <= int(normalized) <= 12:
        return int(normalized)
    aliases = {name: index for index, name in enumerate(MONTH_NAMES_GENITIVE, start=1)}
    short_aliases = {
        "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "июн": 6,
        "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    }
    if normalized in aliases:
        return aliases[normalized]
    if normalized in short_aliases:
        return short_aliases[normalized]
    raise ValueError(
        "Укажите месяц числом от 1 до 12 или названием, например: /month 12"
    )


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


@router.message(Command("month", "birthdaysmonth", "monthbirthdays"))
async def month_birthdays(message: Message) -> None:
    try:
        today_date = datetime.now(get_timezone()).date()
        argument = message.text.split(maxsplit=1)[1] if message.text and " " in message.text else None
        month = parse_month(argument, today_date.month)
        await message.answer(
            month_birthday_report(
                read_people(),
                month,
                today_date.year,
                include_greeting=False,
                reference_date=today_date if month == today_date.month else date(today_date.year, month, 1),
            )
        )
    except ValueError as exc:
        await message.answer(str(exc))
    except Exception:
        log.exception("Ошибка формирования списка именинников месяца")
        await message.answer("Не удалось прочитать Google Sheets. Проверьте настройки и доступ таблицы.")


@router.message(Command("previewmonth"))
async def preview_month(message: Message) -> None:
    """Admin-only preview of the automatic first-day monthly post."""
    if not await can_manage(message):
        await message.answer("Эта команда доступна только администратору.")
        return
    try:
        today_date = datetime.now(get_timezone()).date()
        argument = message.text.split(maxsplit=1)[1] if message.text and " " in message.text else "11"
        month = parse_month(argument, 11)
        await message.answer(
            month_birthday_report(
                read_people(),
                month,
                today_date.year,
                include_greeting=True,
                reference_date=date(today_date.year, month, 1),
            )
        )
    except ValueError as exc:
        await message.answer(str(exc))
    except Exception:
        log.exception("Ошибка предпросмотра месячного сообщения")
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
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ConfigError("SEND_HOUR должен быть 0–23, SEND_MINUTE — 0–59")
    tz = get_timezone()
    state = load_scheduler_state()
    monthly_sent = state["monthly_sent"]
    daily_sent = state["daily_sent"]
    while True:
        now = datetime.now(tz)
        chat_id, thread_id = load_delivery()
        key = delivery_key(chat_id, thread_id) if chat_id is not None else None

        # The monthly post is strictly limited to the first day of a month.
        # Persist the month per destination so process restarts cannot replay it.
        if (
            key is not None
            and now.day == 1
            and now.hour == 0
            and now.minute == 0
            and monthly_sent.get(key) != now.strftime("%Y-%m")
        ):
            try:
                monthly_report = month_birthday_report(
                    read_people(),
                    now.month,
                    now.year,
                    include_greeting=True,
                    reference_date=now.date(),
                )
                await bot.send_message(chat_id, monthly_report, message_thread_id=thread_id)
                monthly_sent[key] = now.strftime("%Y-%m")
                save_scheduler_state(state)
                log.info("Месячный список именинников отправлен в chat_id=%s", chat_id)
            except Exception:
                log.exception("Не удалось отправить месячный список именинников")

        if (
            key is not None
            and now.hour == hour
            and now.minute == minute
            and daily_sent.get(key) != now.date().isoformat()
        ):
            try:
                report = build_today_report(read_people(), now.date())
                if report is not None:
                    await bot.send_message(chat_id, report, message_thread_id=thread_id)
                    log.info(
                        "Поздравление отправлено "
                        "в chat_id=%s thread_id=%s",
                        chat_id,
                        thread_id,
                    )
                else:
                    log.info("На %s именинников нет, сообщение не отправляется", now.date())
                daily_sent[key] = now.date().isoformat()
                save_scheduler_state(state)
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
