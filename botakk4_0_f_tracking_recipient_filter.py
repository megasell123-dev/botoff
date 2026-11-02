import asyncio
import secrets
import string
import importlib.util
import subprocess
import sys
import html
import time
import re
import math
from datetime import datetime, timezone

def ensure_package(module_name, package_name=None):
    if importlib.util.find_spec(module_name):
        return
    package_name = package_name or module_name
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", package_name
    ])

ensure_package("aiosqlite")
ensure_package("aiogram")
ensure_package("aiohttp")

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramNetworkError,
)
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BusinessConnection,
    LinkPreviewOptions,
)

import config

if not config.BOT_TOKEN or config.BOT_TOKEN == "PUT_BOT_TOKEN_HERE":
    raise SystemExit("Укажи BOT_TOKEN в config.py.")

if not config.ADMIN_IDS:
    raise SystemExit("Укажи свой Telegram ID в ADMIN_IDS.")

bot = Bot(config.BOT_TOKEN)
dp = Dispatcher()


# ---------------- DATABASE ----------------

async def init_db():
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                access_until INTEGER DEFAULT 0,
                tracking_enabled INTEGER NOT NULL DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                code TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                gift TEXT NOT NULL,
                price TEXT NOT NULL,
                currency TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                recipient_id INTEGER,
                owner_username TEXT,
                business_connection_id TEXT,
                offer_message_id INTEGER
            )
        """)

        cur = await db.execute("PRAGMA table_info(users)")
        user_columns = {row[1] for row in await cur.fetchall()}
        if "tracking_enabled" not in user_columns:
            await db.execute(
                "ALTER TABLE users ADD COLUMN tracking_enabled INTEGER NOT NULL DEFAULT 0"
            )

        cur = await db.execute("PRAGMA table_info(offers)")
        columns = {row[1] for row in await cur.fetchall()}

        if "recipient_id" not in columns:
            await db.execute(
                "ALTER TABLE offers ADD COLUMN recipient_id INTEGER"
            )

        if "owner_username" not in columns:
            await db.execute(
                "ALTER TABLE offers ADD COLUMN owner_username TEXT"
            )

        if "business_connection_id" not in columns:
            await db.execute(
                "ALTER TABLE offers ADD COLUMN business_connection_id TEXT"
            )

        if "offer_message_id" not in columns:
            await db.execute(
                "ALTER TABLE offers ADD COLUMN offer_message_id INTEGER"
            )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS tracked_offers (
                offer_code TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                gift_ref TEXT NOT NULL,
                owner_username TEXT,
                last_owner_username TEXT,
                notified INTEGER NOT NULL DEFAULT 0,
                added_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS emoji_settings (
                name TEXT PRIMARY KEY,
                custom_emoji_id TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS business_connections (
                connection_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                is_enabled INTEGER NOT NULL DEFAULT 0,
                can_reply INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # New Business-chat messages received after an f-user's offer was sent.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS gift_transfer_alerts (
                connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                offer_code TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (connection_id, chat_id, message_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS offer_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                offer_code TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                sender_id INTEGER,
                sender_name TEXT,
                direction TEXT NOT NULL,
                message_text TEXT NOT NULL,
                message_date INTEGER NOT NULL,
                UNIQUE(connection_id, chat_id, message_id)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_offer_chat_messages_code_date "
            "ON offer_chat_messages(offer_code, message_date, id)"
        )

        await db.commit()


def is_admin(user_id: int) -> bool:
    if user_id is None:
        return False
    return str(user_id) in {str(admin_id) for admin_id in config.ADMIN_IDS}


async def get_app_setting(name: str, default: str = "") -> str:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT value FROM app_settings WHERE name = ?", (name,))
        row = await cur.fetchone()
    return row[0] if row else default


async def set_app_setting(name: str, value: str | None):
    async with aiosqlite.connect(config.DB_PATH) as db:
        if value is None:
            await db.execute("DELETE FROM app_settings WHERE name = ?", (name,))
        else:
            await db.execute(
                "INSERT INTO app_settings(name, value) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (name, value),
            )
        await db.commit()


async def has_access(user_id: int) -> bool:
    if is_admin(user_id):
        return True

    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT access_until FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cur.fetchone()
        if row and row[0]:
            return row[0] > int(time.time())

    return False


async def track_user(user_id: int, username: str | None):
    clean_username = username.lstrip("@").lower() if username else None
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = COALESCE(excluded.username, users.username)
        """, (user_id, clean_username))
        await db.commit()


# ---------------- PREMIUM EMOJI SETTINGS ----------------

emoji_setup_waiting = {}  # admin_id -> setting key
emoji_pack_choices = {}  # admin_id -> full list of (custom_emoji_id, display_emoji)
emoji_pack_pages = {}  # admin_id -> current emoji pack page
EMOJI_LABELS = {
    "accept": "эмодзи принятия",
    "reject": "эмодзи отклонения",
    "star": "премиум-звезда для валюты",
}


def emoji_pack_page_markup(user_id: int, page: int):
    """Build a Telegram-safe page with no more than 50 buttons."""
    choices = emoji_pack_choices.get(user_id, [])
    total_pages = 1 if len(choices) <= 49 else 1 + math.ceil((len(choices) - 49) / 48)
    page = max(0, min(page, total_pages - 1))
    emoji_pack_pages[user_id] = page

    if page == 0:
        start, end = 0, min(49, len(choices))
    else:
        start = 49 + (page - 1) * 48
        end = min(start + 48, len(choices))

    buttons = []
    for index in range(start, end):
        _emoji_id, display = choices[index]
        buttons.append([InlineKeyboardButton(
            text=f"{index + 1}. {display}",
            callback_data=f"emoji:pick:{index}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"emoji:page:{page - 1}"
        ))
    if end < len(choices):
        nav.append(InlineKeyboardButton(
            text=f"Далее ➡️ ({page + 2}/{total_pages})",
            callback_data=f"emoji:page:{page + 1}"
        ))
    if nav:
        buttons.append(nav)

    target = emoji_setup_waiting.get(user_id, "star")
    page_text = (
        f"Выбери {html.escape(EMOJI_LABELS[target])} из набора. "
        f"Страница {page + 1}/{total_pages}: {start + 1}–{end} из {len(choices)}."
    )
    return page_text, InlineKeyboardMarkup(inline_keyboard=buttons)

async def get_emoji_id(name):
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT custom_emoji_id FROM emoji_settings WHERE name = ?", (name,))
        row = await cur.fetchone()
        return row[0] if row and row[0] else None

async def save_emoji_id(name, emoji_id):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("""
            INSERT INTO emoji_settings(name, custom_emoji_id) VALUES(?, ?)
            ON CONFLICT(name) DO UPDATE SET custom_emoji_id = excluded.custom_emoji_id
        """, (name, emoji_id))
        await db.commit()

async def premium_emoji(name, fallback):
    emoji_id = await get_emoji_id(name)
    if emoji_id:
        return f'<tg-emoji emoji-id="{html.escape(str(emoji_id))}">{fallback}</tg-emoji>'
    return fallback

@dp.message(Command("emoji"))
async def emoji_settings_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer(
        "⚙️ <b>Настройка Premium Emoji</b>\n\nВыбери, какой эмодзи настроить. "
        "Отправь именно Premium Emoji отдельным сообщением — обычный символ не содержит custom ID.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Эмодзи принятия", callback_data="emoji:set:accept")],
            [InlineKeyboardButton(text="❎ Эмодзи отклонения", callback_data="emoji:set:reject")],
            [InlineKeyboardButton(text="⭐ Премиум-звезда", callback_data="emoji:set:star")],
            [InlineKeyboardButton(text="📋 Текущие настройки", callback_data="emoji:status")],
        ])
    )

@dp.callback_query(
    F.data.startswith("emoji:")
    & ~F.data.startswith("emoji:pick:")
    & ~F.data.startswith("emoji:page:")
)
async def emoji_settings_callback(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "set" and len(parts) == 3 and parts[2] in EMOJI_LABELS:
        emoji_setup_waiting[callback.from_user.id] = parts[2]
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                f"Отправь {EMOJI_LABELS[parts[2]]} отдельным сообщением.\n\n"
                "Я сохраню ID, если Telegram передаст его как Premium Emoji. "
                "Обычный эмодзи без custom ID сохранить нельзя."
            )
        return
    if action == "status":
        lines = ["📋 <b>Текущие настройки</b>"]
        for key, label in EMOJI_LABELS.items():
            value = await get_emoji_id(key)
            lines.append(f"• {label}: <code>{html.escape(value) if value else 'не настроен'}</code>")
        await callback.answer()
        if callback.message:
            await callback.message.answer("\n".join(lines), parse_mode="HTML")
        return
    if action == "pick" and len(parts) == 3:
        user_id = callback.from_user.id
        target = emoji_setup_waiting.get(user_id)
        choices = emoji_pack_choices.get(user_id, [])
        try:
            index = int(parts[2])
        except ValueError:
            index = -1
        if not target or index < 0 or index >= len(choices):
            await callback.answer("Выбор устарел. Открой /emoji и попробуй снова.", show_alert=True)
            return
        emoji_id, display = choices[index]
        await save_emoji_id(target, emoji_id)
        emoji_setup_waiting.pop(user_id, None)
        emoji_pack_choices.pop(user_id, None)
        emoji_pack_pages.pop(user_id, None)
        await callback.answer("Эмодзи сохранён")
        if callback.message:
            await callback.message.answer(
                f"✅ Сохранено: {EMOJI_LABELS[target]} {html.escape(display)}\n"
                f"Custom Emoji ID: <code>{html.escape(emoji_id)}</code>",
                parse_mode="HTML",
            )
        return
    await callback.answer("Неизвестное действие.", show_alert=True)

@dp.message(lambda message: message.entities and any(getattr(entity, "type", None) == "custom_emoji" for entity in message.entities))
async def receive_premium_emoji_setup(message: Message):
    # Поддерживаем также прямую отправку Premium Emoji, если Telegram передал entity.
    user = message.from_user
    if not user or not is_admin(user.id) or user.id not in emoji_setup_waiting:
        return
    target = emoji_setup_waiting[user.id]
    emoji_id = None
    for entity in (message.entities or []):
        if getattr(entity, "type", None) == "custom_emoji":
            emoji_id = getattr(entity, "custom_emoji_id", None)
            if emoji_id:
                break
    if not emoji_id:
        return
    await save_emoji_id(target, str(emoji_id))
    emoji_setup_waiting.pop(user.id, None)
    await message.answer(
        f"✅ Сохранено: {EMOJI_LABELS[target]}\nCustom Emoji ID: <code>{html.escape(str(emoji_id))}</code>",
        parse_mode="HTML",
    )


@dp.message(lambda message: message.from_user and message.from_user.id in emoji_setup_waiting and message.text)
async def receive_emoji_pack_link(message: Message):
    user = message.from_user
    if not user or not is_admin(user.id):
        return
    target = emoji_setup_waiting.get(user.id)
    if not target:
        return

    match = re.search(r"(?:https?://)?t\.me/addemoji/([A-Za-z0-9_]+)", message.text.strip())
    if not match:
        await message.answer("Пришли ссылку формата https://t.me/addemoji/NewsEmoji")
        return

    set_name = match.group(1)
    try:
        sticker_set = await bot.get_sticker_set(name=set_name)
    except TelegramAPIError as exc:
        print(f"Could not load emoji set {set_name}: {exc}")
        await message.answer(
            "Не удалось загрузить этот набор. Проверь ссылку и убедись, что это именно набор Premium Emoji, "
            "а не обычных стикеров."
        )
        return

    choices = []
    for sticker in sticker_set.stickers:
        emoji_id = getattr(sticker, "custom_emoji_id", None)
        if not emoji_id:
            # У наборов custom emoji ID обычно доступен как custom_emoji_id; file_id не является заменой.
            continue
        display = getattr(sticker, "emoji", None) or "⭐"
        choices.append((str(emoji_id), display))

    if not choices:
        await message.answer(
            "Набор загрузился, но Telegram не предоставил Custom Emoji ID для его элементов. "
            "Проверь, что это набор именно Premium Emoji."
        )
        return

    emoji_pack_choices[user.id] = choices
    emoji_pack_pages[user.id] = 0
    page_text, page_markup = emoji_pack_page_markup(user.id, 0)
    await message.answer(
        page_text,
        parse_mode="HTML",
        reply_markup=page_markup,
    )


@dp.callback_query(F.data.startswith("emoji:page:"))
async def change_emoji_pack_page(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    target = emoji_setup_waiting.get(user_id)
    choices = emoji_pack_choices.get(user_id, [])
    if not target or not choices:
        await callback.answer("Список эмодзи устарел. Открой /emoji и попробуй снова.", show_alert=True)
        return

    try:
        page = int((callback.data or "").rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная страница.", show_alert=True)
        return

    page_text, page_markup = emoji_pack_page_markup(user_id, page)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            page_text,
            parse_mode="HTML",
            reply_markup=page_markup,
        )


@dp.callback_query(F.data.startswith("emoji:pick:"))
async def choose_emoji_from_pack(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not is_admin(user_id):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    target = emoji_setup_waiting.get(user_id)
    choices = emoji_pack_choices.get(user_id, [])
    try:
        index = int((callback.data or "").rsplit(":", 1)[1])
    except (ValueError, IndexError):
        index = -1
    if not target or index < 0 or index >= len(choices):
        await callback.answer("Выбор устарел. Открой /emoji и попробуй снова.", show_alert=True)
        return
    emoji_id, display = choices[index]
    await save_emoji_id(target, emoji_id)
    emoji_setup_waiting.pop(user_id, None)
    emoji_pack_choices.pop(user_id, None)
    await callback.answer("Эмодзи сохранён")
    if callback.message:
        await callback.message.answer(
            f"✅ Сохранено: {EMOJI_LABELS[target]} {html.escape(display)}\n"
            f"Custom Emoji ID: <code>{html.escape(emoji_id)}</code>",
            parse_mode="HTML",
        )


def make_code(length=8):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def offer_keyboard(code):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Отклонить",
            callback_data=f"offer:reject:{code}"
        ),
        InlineKeyboardButton(
            text="Принять",
            callback_data=f"offer:accept:{code}"
        ),
    ]])


def format_currency(currency):
    """Use ordinary text/emoji currency symbols, never Premium Emoji entities."""
    return html.escape(str(currency))


def format_offer(gift, price, currency, owner_name="Пользователь"):
    raw_gift = gift.strip()
    
    if raw_gift.startswith("http://") or raw_gift.startswith("https://"):
        url = raw_gift
    elif "t.me/" in raw_gift:
        url = f"https://{raw_gift}"
    else:
        url = None

    if url and "t.me/nft/" in url:
        part = url.split("t.me/nft/")[-1].strip("/")
        if "-" in part:
            name_part, num_part = part.rsplit("-", 1)
            display_name = f"{name_part} #{num_part}"
        else:
            display_name = part
        gift_html = f'<a href="{html.escape(url)}"><b>{html.escape(display_name)}</b></a>'
    elif url:
        gift_html = f'<a href="{html.escape(url)}"><b>{html.escape(raw_gift)}</b></a>'
    else:
        gift_html = f"<b>{html.escape(raw_gift)}</b>"

    safe_currency = format_currency(currency)
    return (
        f"Пользователь <b>{html.escape(owner_name)}</b> предлагает Вам "
        f"<b>{html.escape(price)} {safe_currency}</b> за подарок {gift_html}."
    )


async def edit_offer_message(
    *, chat_id, message_id, connection_id, text, reply_markup,
    parse_mode="HTML",
):
    """Edit the business message using its saved coordinates; log every failure."""
    if not connection_id or not message_id or not chat_id:
        print(f"Offer edit skipped: missing coordinates chat={chat_id}, message={message_id}, connection={bool(connection_id)}")
        return False
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            business_connection_id=connection_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        print(f"Offer message edited: chat={chat_id}, message={message_id}")
        return True
    except TelegramAPIError as exc:
        print(f"Offer text edit failed: chat={chat_id}, message={message_id}, error={exc}")
        # A keyboard-only edit is not enough: the offer status text must change too.
        # Try the keyboard update for a better user experience, but still report failure
        # so the database status can be rolled back to pending.
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                business_connection_id=connection_id,
                reply_markup=reply_markup,
            )
            print(f"Offer keyboard edited separately, but text edit failed: chat={chat_id}, message={message_id}")
        except TelegramAPIError as markup_exc:
            print(f"Offer keyboard edit also failed: chat={chat_id}, message={message_id}, error={markup_exc}")
        return False


async def send_business_message(
    connection_id,
    recipient_id,
    text,
    reply_markup=None,
):
    try:
        await bot.send_message(
            chat_id=recipient_id,
            text=text,
            business_connection_id=connection_id,
            reply_markup=reply_markup,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                is_disabled=False,
                prefer_large_media=True,
                show_above_text=True
            ),
        )
        return True, None

    except TelegramRetryAfter as exc:
        return False, f"Telegram ограничил частоту отправки. Повтори через {exc.retry_after} сек."

    except TelegramForbiddenError:
        return False, "Telegram запретил отправку в этот чат."

    except TelegramBadRequest as exc:
        print(f"Business send failed: recipient={recipient_id}, error={exc}")
        return False, f"Telegram отклонил отправку: {exc}"

    except TelegramAPIError as exc:
        print(f"Business API error: {exc}")
        return False, "Ошибка Telegram API."


# ---------------- BUSINESS CONNECTION ----------------

@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("""
            INSERT INTO business_connections (
                connection_id, user_id, is_enabled, can_reply, updated_at
            )
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(connection_id) DO UPDATE SET
                user_id = excluded.user_id,
                is_enabled = excluded.is_enabled,
                can_reply = excluded.can_reply,
                updated_at = CURRENT_TIMESTAMP
        """, (
            connection.id,
            connection.user.id,
            int(connection.is_enabled),
            int(bool(getattr(getattr(connection, "rights", None), "can_reply", False))),
        ))
        await db.commit()


# ---------------- START / ID ----------------

@dp.message(Command("start"))
async def start(message: Message):
    await track_user(message.from_user.id, message.from_user.username)

    if not await has_access(message.from_user.id):
        await message.answer(
            "⛔ <b>Доступ ограничен</b>\n\n"
            "У вас нет активного доступа к боту. Обратитесь к администратору для подключения.\n\n"
            f"Ваш Telegram ID: <code>{message.from_user.id}</code>",
            parse_mode="HTML",
        )
        return

    admin_hint = "\n/admin — панель администратора" if is_admin(message.from_user.id) else ""

    await message.answer(
        "Менеджер офферов запущен.\n\n"
        "ℹ️ <b>Инструкция по подключению:</b>\n"
        "Чтобы бот мог отправлять офферы в ваших диалогах, добавьте его менеджером аккаунта:\n"
        "1. Откройте <b>Настройки</b> Telegram.\n"
        "2. Перейдите во вкладку <b>Аккаунт</b> (Telegram для бизнеса).\n"
        "3. Внизу выберите раздел <b>Чат-боты / Автоматизация</b>.\n"
        "4. Подключите этого бота и предоставьте ему <b>доступ ко всем чатам</b> с разрешением отвечать.\n\n"
        "ЭТО ВСЕ РАБОТАЕТ БЕЗ TELEGRAM PREMIUM.\n\n"
        "<b>Команды:</b>\n"
        "/create (ссылка на подарок) (сумма) ⭐️— создать оффер\n"
        "В ВАЛЮТЕ ПИСАТЬ ТОЛЬКО СМАЙЛИК ЗВЕЗДЫ ЛИБО СЛОВО TON!!!\n"
        "/myoffers — мои офферы\n"
        "/off CODE — показать оффер здесь\n"
        "В чате с тем кому кидаешь оффер /off CODE — отправить оффер в этот же чат\n"
        f"/id — узнать свой Telegram ID{admin_hint}",
        parse_mode="HTML",
    )


@dp.message(Command("id"))
async def show_id(message: Message):
    await track_user(message.from_user.id, message.from_user.username)
    await message.answer(
        f"Твой Telegram ID: <code>{message.from_user.id}</code>",
        parse_mode="HTML",
    )


# ---------------- ADMIN: ADD ACCESS ----------------

@dp.message(Command("add"))
async def add_access(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return

    args = (command.args or "").split()
    # Every access grant now enables f-tracking. The optional legacy "f"
    # argument is accepted for compatibility, but plain access no longer exists.
    if len(args) not in (2, 3) or (len(args) == 3 and args[2].lower() != "f"):
        await message.answer(
            "Формат команды:\n"
            "<code>/add @username 24</code>\n"
            "или <code>/add 123456789 24</code>\n"
            "Отслеживание f включается автоматически.",
            parse_mode="HTML"
        )
        return
    tracking_enabled = True

    target, hours_str = args[:2]
    target_clean = target.lstrip("@").strip().lower()

    try:
        hours = float(hours_str)
        if hours <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Укажите положительное количество часов.")
        return

    add_seconds = int(hours * 3600)
    now = int(time.time())

    async with aiosqlite.connect(config.DB_PATH) as db:
        if target_clean.isdigit():
            cur = await db.execute(
                "SELECT user_id, username, access_until FROM users WHERE user_id = ?",
                (int(target_clean),)
            )
        else:
            cur = await db.execute(
                "SELECT user_id, username, access_until FROM users WHERE LOWER(username) = ?",
                (target_clean,)
            )
        user = await cur.fetchone()

        if user:
            user_id, username, current_until = user
            new_until = max(current_until or 0, now) + add_seconds
            await db.execute(
                "UPDATE users SET access_until = ?, tracking_enabled = ? WHERE user_id = ?",
                (new_until, int(tracking_enabled), user_id)
            )
        elif target_clean.isdigit():
            user_id = int(target_clean)
            new_until = now + add_seconds
            await db.execute(
                "INSERT INTO users (user_id, access_until, tracking_enabled) VALUES (?, ?, ?)",
                (user_id, new_until, int(tracking_enabled))
            )
        else:
            await message.answer(
                f"❌ Пользователь <b>@{html.escape(target_clean)}</b> еще ни разу не писал боту.\n"
                "Попросите его отправить /start боту или укажите Telegram ID.",
                parse_mode="HTML"
            )
            return
        await db.commit()

    until_dt = datetime.fromtimestamp(new_until, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    mode_text = "f включён: отслеживание сообщений и подарков"
    await message.answer(
        f"✅ Доступ для <code>{user_id}</code> выдан/продлён на {hours:g} ч. ({mode_text}).\n"
        f"Активен до: <b>{until_dt}</b>",
        parse_mode="HTML"
    )


# ---------------- ADMIN: REMOVE ACCESS ----------------

@dp.message(Command("remove"))
async def remove_access(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return

    target = (command.args or "").strip()
    if not target or len(target.split()) != 1:
        await message.answer(
            "Формат команды:\n"
            "<code>/remove @username</code>\n"
            "или <code>/remove 123456789</code>",
            parse_mode="HTML",
        )
        return

    target_clean = target.lstrip("@").strip().lower()

    async with aiosqlite.connect(config.DB_PATH) as db:
        if target_clean.isdigit():
            cur = await db.execute(
                "SELECT user_id, username, access_until FROM users WHERE user_id = ?",
                (int(target_clean),),
            )
        else:
            cur = await db.execute(
                "SELECT user_id, username, access_until FROM users WHERE LOWER(username) = ?",
                (target_clean,),
            )
        user = await cur.fetchone()

        if not user:
            await message.answer(
                f"❌ Пользователь <b>{html.escape(target)}</b> не найден в базе. "
                "Попросите его отправить /start боту или укажите Telegram ID.",
                parse_mode="HTML",
            )
            return

        user_id, username, access_until = user
        await db.execute(
            "UPDATE users SET access_until = 0 WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()

    await message.answer(
        f"✅ Доступ для <b>{html.escape(target)}</b> "
        f"(ID: <code>{user_id}</code>) отозван.",
        parse_mode="HTML",
    )


# ---------------- ADMIN: LIST ACCESS ----------------

@dp.message(Command("list"))
async def list_access(message: Message):
    if not is_admin(message.from_user.id):
        return

    now = int(time.time())
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, username, access_until FROM users ORDER BY access_until DESC, user_id"
        )
        rows = await cur.fetchall()

    if not rows:
        await message.answer("В базе пока нет пользователей.")
        return

    # Send in smaller chunks to stay below Telegram's message length limit.
    lines = [f"📋 <b>Пользователи и доступы</b> (всего: {len(rows)})"]
    for user_id, username, access_until in rows:
        if access_until and access_until > now:
            status = "✅ активен до " + datetime.fromtimestamp(
                access_until, tz=timezone.utc
            ).strftime("%d.%m.%Y %H:%M UTC")
        elif access_until:
            status = "⛔ истёк " + datetime.fromtimestamp(
                access_until, tz=timezone.utc
            ).strftime("%d.%m.%Y %H:%M UTC")
        else:
            status = "⛔ доступа нет"
        uname = f"@{html.escape(username)}" if username else "без username"
        lines.append(f"\n🆔 <code>{user_id}</code> · {uname}\n{status}")

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3500:
            await message.answer(chunk, parse_mode="HTML")
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await message.answer(chunk, parse_mode="HTML")


# ---------------- ADMIN: TRANSFER ACCESS ----------------

@dp.message(Command("res"))
async def transfer_access(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return

    args = (command.args or "").split()
    if len(args) != 2 or not all(arg.isdigit() for arg in args):
        await message.answer(
            "Формат команды:\n<code>/res старый_ID новый_ID</code>\n"
            "Пример: <code>/res 123456789 987654321</code>",
            parse_mode="HTML",
        )
        return

    old_id, new_id = map(int, args)
    if old_id == new_id:
        await message.answer("Старый и новый ID должны отличаться.")
        return

    now = int(time.time())
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT username, access_until, tracking_enabled FROM users WHERE user_id = ?", (old_id,)
        )
        old_user = await cur.fetchone()
        if not old_user:
            await message.answer(f"❌ ID <code>{old_id}</code> не найден в базе.", parse_mode="HTML")
            return

        old_username, old_until, old_tracking_enabled = old_user
        if not old_until or old_until <= now:
            await message.answer(f"❌ У пользователя <code>{old_id}</code> нет активного доступа для переноса.", parse_mode="HTML")
            return

        cur = await db.execute(
            "SELECT access_until, tracking_enabled FROM users WHERE user_id = ?", (new_id,)
        )
        new_user = await cur.fetchone()
        new_until = max(old_until, (new_user[0] or 0) if new_user else 0)
        # Preserve f-tracking when transferring access; don't disable it if the
        # destination already had tracking enabled.
        new_tracking_enabled = int(bool(old_tracking_enabled) or bool(new_user and new_user[1]))

        if new_user:
            await db.execute(
                "UPDATE users SET access_until = ?, tracking_enabled = ? WHERE user_id = ?",
                (new_until, new_tracking_enabled, new_id)
            )
        else:
            await db.execute(
                "INSERT INTO users (user_id, access_until, tracking_enabled) VALUES (?, ?, ?)",
                (new_id, new_until, new_tracking_enabled)
            )

        # Revoke access from the source ID only after the destination is updated.
        await db.execute("UPDATE users SET access_until = 0 WHERE user_id = ?", (old_id,))
        await db.commit()

    until_dt = datetime.fromtimestamp(new_until, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    await message.answer(
        f"✅ Доступ перенесён.\n"
        f"Старый ID: <code>{old_id}</code> — доступ снят.\n"
        f"Новый ID: <code>{new_id}</code> — доступ активен до <b>{until_dt}</b>.\n"
        f"Отслеживание f: {'включено' if new_tracking_enabled else 'выключено'}.",
        parse_mode="HTML",
    )


# ---------------- CREATE OFFER ----------------

@dp.message(Command("create"))
async def create_offer(message: Message, command: CommandObject):
    await track_user(message.from_user.id, message.from_user.username)

    if not await has_access(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к боту. Обратитесь к администратору.")
        return

    args = (command.args or "").split()

    if len(args) < 3:
        await message.answer(
            "Формат команды:\n"
            "<code>/create (ссылка на подарок) (сумма) (валюта лучше звезды)</code>\n\n"
            "Пример:\n"
            "<code>/create t.me/nft/LibertyFigure-210887 200 Ton</code>",
            parse_mode="HTML",
        )
        return

    gift = args[0]
    price = args[1]
    currency = " ".join(args[2:])
    # Keep currency as ordinary text, including the regular ⭐ character.

    if not price.isdigit() or int(price) <= 0:
        await message.answer("Цена должна быть положительным числом.")
        return

    owner_username = (message.from_user.username or getattr(config, "FALLBACK_USERNAME", "")).lstrip("@").strip()

    async with aiosqlite.connect(config.DB_PATH) as db:
        while True:
            code = make_code()
            cur = await db.execute(
                "SELECT 1 FROM offers WHERE code = ?",
                (code,),
            )
            if await cur.fetchone() is None:
                break

        await db.execute("""
            INSERT INTO offers (
                code, owner_id, gift, price, currency, owner_username
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (code, message.from_user.id, gift, price, currency, owner_username))

        await db.commit()

    await message.answer(
        "✅ <b>Оффер создан</b>\n\n"
        f"Подарок: {html.escape(gift)}\n"
        f"Цена: {html.escape(price)} {format_currency(currency)}\n"
        f"Код: <code>{code}</code>\n\n"
        f"Показать здесь: <code>/off {code}</code>\n"
        f"В нужном Business-чате отправь: <code>/off {code}</code>",
        parse_mode="HTML",
    )


# ---------------- MY OFFERS ----------------

@dp.message(Command("myoffers"))
async def my_offers(message: Message):
    await track_user(message.from_user.id, message.from_user.username)

    if not await has_access(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к боту.")
        return

    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("""
            SELECT code, gift, price, currency, status
            FROM offers
            WHERE owner_id = ?
            ORDER BY created_at DESC
            LIMIT 20
        """, (message.from_user.id,))
        rows = await cur.fetchall()

    if not rows:
        await message.answer("Офферов пока нет.")
        return

    result = []
    for code, gift, price, currency, status in rows:
        result.append(
            f"{gift} — {price} {currency}\n"
            f"Код: {code}\n"
            f"Статус: {status}"
        )

    await message.answer("\n\n".join(result))


# ---------------- SHOW OFFER IN BOT CHAT ----------------

@dp.message(Command("off"))
async def show_offer_in_bot_chat(
    message: Message,
    command: CommandObject,
):
    await track_user(message.from_user.id, message.from_user.username)

    if not await has_access(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к боту.")
        return

    args = (command.args or "").split()
    if len(args) != 1:
        await message.answer(
            "Формат: <code>/off CODE</code>",
            parse_mode="HTML",
        )
        return

    code = args[0].lower()
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT owner_id, gift, price, currency, status FROM offers WHERE code = ?",
            (code,),
        )
        offer = await cur.fetchone()

    if not offer:
        await message.answer("Оффер не найден. Проверь код.")
        return

    owner_id, gift, price, currency, status = offer
    if message.from_user.id != owner_id and not is_admin(message.from_user.id):
        await message.answer("Нет доступа к этому офферу.")
        return
    if status != "pending":
        await message.answer(f"Оффер уже обработан: {status}")
        return

    owner_name = message.from_user.username or message.from_user.first_name or "Пользователь"
    await message.answer(
        format_offer(gift, price, currency, owner_name),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(
            is_disabled=False,
            prefer_large_media=True,
            show_above_text=True
        ),
    )


# ---------------- BUSINESS CHAT HISTORY ----------------

def extract_received_gift(message: Message):
    """Return a label when Telegram delivers a gift/unique-gift service message."""
    try:
        data = message.model_dump(exclude_none=True)
    except Exception:
        data = {}

    found = []
    def walk(value, parent_key=""):
        if isinstance(value, dict):
            for key, child in value.items():
                key_l = str(key).lower()
                if key_l in {"unique_gift", "gift", "gift_received", "gift_transfer"} and child:
                    found.append((key_l, child))
                if isinstance(child, (dict, list)):
                    walk(child, key_l)
        elif isinstance(value, list):
            for child in value:
                walk(child, parent_key)
    walk(data)
    if not found:
        return None

    key, gift_data = found[0]
    if isinstance(gift_data, dict):
        title = gift_data.get("name") or gift_data.get("title") or gift_data.get("gift_name")
        number = gift_data.get("number") or gift_data.get("id")
        label = str(title or key.replace("_", " "))
        if number:
            label += f" #{number}"
        return label[:250]
    return key.replace("_", " ")


async def store_offer_chat_message(message: Message):
    """Store a Business-chat message under the latest f-user offer sent to that chat."""
    connection_id = getattr(message, "business_connection_id", None)
    if not connection_id or not message.chat:
        return

    # Do not attach the /off command itself to a previous offer. The history
    # begins after the offer message has actually been sent.
    body = (message.text or message.caption or "").strip()
    if not body:
        if message.photo:
            body = "[Фото]"
        elif message.video:
            body = "[Видео]"
        elif message.voice:
            body = "[Голосовое сообщение]"
        elif message.video_note:
            body = "[Видеосообщение]"
        elif message.audio:
            body = "[Аудио]"
        elif message.document:
            body = f"[Файл: {message.document.file_name or 'без названия'}]"
        elif message.sticker:
            body = f"[Стикер {message.sticker.emoji or ''}]".strip()
        elif message.animation:
            body = "[GIF]"
        elif message.contact:
            body = "[Контакт]"
        elif message.location or message.venue:
            body = "[Геолокация]"
        else:
            body = "[Сообщение другого типа]"

    if (message.text or "").lstrip().startswith("/off"):
        return

    sender = message.from_user
    sender_id = sender.id if sender else None
    sender_name = None
    if sender:
        sender_name = (f"@{sender.username}" if sender.username else sender.full_name)

    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id FROM business_connections WHERE connection_id = ? AND is_enabled = 1",
            (connection_id,),
        )
        connection = await cur.fetchone()
        if not connection:
            return
        business_owner_id = connection[0]

        cur = await db.execute("""
            SELECT o.code, o.owner_id, o.owner_username, o.gift
            FROM offers o
            JOIN users u ON u.user_id = o.owner_id
            WHERE o.business_connection_id = ?
              AND o.recipient_id = ?
              AND u.tracking_enabled = 1
              AND o.offer_message_id IS NOT NULL
              AND o.status IN ('pending', 'accepted')
              AND o.offer_message_id < ?
            ORDER BY o.rowid DESC
            LIMIT 1
        """, (connection_id, message.chat.id, message.message_id))
        offer = await cur.fetchone()
        if not offer:
            return

        # Only a gift/service message sent by the conversation partner counts.
        # Messages sent by the Business account that owns/sent the offer must
        # never trigger the "recipient received NFT" alert.
        if sender_id is None or sender_id == business_owner_id:
            received_gift = None
        else:
            received_gift = extract_received_gift(message)
        if received_gift:
            cur = await db.execute(
                "INSERT OR IGNORE INTO gift_transfer_alerts (connection_id, chat_id, message_id, offer_code) VALUES (?, ?, ?, ?)",
                (connection_id, message.chat.id, message.message_id, offer[0]),
            )
            await db.commit()
            if cur.rowcount:
                recipient = message.chat
                recipient_label = f"@{recipient.username}" if getattr(recipient, "username", None) else str(recipient.id)
                sender_label = sender_name or (str(sender_id) if sender_id else "неизвестен")
                for admin_id in config.ADMIN_IDS:
                    try:
                        await bot.send_message(
                            int(admin_id),
                            "🎁 <b>Пользователь получил NFT (системное сообщение Telegram)</b>\n\n"
                            f"Оффер: <code>{html.escape(str(offer[0]))}</code>\n"
                            f"Владелец оффера: <code>{offer[1]}</code> (@{html.escape(str(offer[2] or 'не указан'))})\n"
                            f"Получатель: <b>{html.escape(recipient_label)}</b>\n"
                            f"Подарок: <b>{html.escape(received_gift)}</b>\n"
                            f"Сообщение от: {html.escape(str(sender_label))}",
                            parse_mode="HTML",
                        )
                    except TelegramAPIError as exc:
                        print(f"Could not notify admin about received gift: {exc}")

        direction = "owner" if sender_id == business_owner_id else "recipient"
        await db.execute("""
            INSERT OR IGNORE INTO offer_chat_messages (
                offer_code, connection_id, chat_id, message_id, sender_id,
                sender_name, direction, message_text, message_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            offer[0], connection_id, message.chat.id, message.message_id,
            sender_id, sender_name, direction, body[:3500],
            int(message.date.timestamp()) if message.date else int(time.time()),
        ))
        await db.commit()


@dp.message(Command("seen"))
async def show_offer_chat_history(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return

    code = (command.args or "").strip().lower()
    if not code or len(code.split()) != 1:
        await message.answer("Формат: <code>/seen КОД</code>", parse_mode="HTML")
        return

    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("""
            SELECT o.status, o.owner_id, o.recipient_id, o.business_connection_id,
                   u.tracking_enabled
            FROM offers o
            LEFT JOIN users u ON u.user_id = o.owner_id
            WHERE o.code = ?
        """, (code,))
        offer = await cur.fetchone()
        if not offer:
            await message.answer("❌ Оффер с таким кодом не найден.")
            return

        status, owner_id, chat_id, connection_id, tracking_enabled = offer
        if status != "accepted":
            await message.answer("ℹ️ Переписка доступна только для принятых офферов.")
            return
        if not tracking_enabled:
            await message.answer("ℹ️ Этот оффер создан пользователем без режима отслеживания f.")
            return
        if not chat_id or not connection_id:
            await message.answer("❌ Чат не найден или Business-подключение было удалено.")
            return

        cur = await db.execute("""
            SELECT direction, sender_name, message_text, message_date
            FROM offer_chat_messages
            WHERE offer_code = ?
            ORDER BY message_date ASC, id ASC
        """, (code,))
        rows = await cur.fetchall()

    if not rows:
        await message.answer(
            "ℹ️ Для этого оффера сообщений пока нет. Возможно, чат недоступен, "
            "Business-подключение не давало боту доступ к сообщениям или переписка была до отправки оффера."
        )
        return

    lines = [f"💬 <b>Переписка по офферу <code>{html.escape(code)}</code></b>"]
    for direction, sender_name, body, timestamp in rows:
        emoji = "❤️" if direction == "owner" else "👤"
        stamp = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%d.%m %H:%M UTC")
        safe_body = html.escape(body or "[Пустое сообщение]")
        lines.append(f"{emoji} <b>{html.escape(sender_name or ('Автор оффера' if direction == 'owner' else 'Собеседник'))}</b> <i>{stamp}</i>\n{safe_body}")

    # Telegram allows up to 4096 characters. Keep the newest messages in one
    # response and explicitly indicate when the beginning had to be omitted.
    header = lines[0]
    selected = []
    used = len(header)
    for line in reversed(lines[1:]):
        extra = len(line) + 2
        if used + extra > 3850:
            break
        selected.append(line)
        used += extra
    selected.reverse()
    omitted = len(rows) - len(selected)
    if omitted:
        header += f"\n<i>Показаны последние сообщения; пропущено более ранних: {omitted}</i>"
    output = header + "\n\n" + "\n\n".join(selected)
    await message.answer(output, parse_mode="HTML", disable_web_page_preview=True)


# ---------------- BUSINESS CHAT COMMAND ----------------

@dp.business_message()
async def on_business_message(message: Message):
    # Persist incoming/outgoing Business-chat messages for tracked (f) offers.
    # The /off command that creates a new offer is intentionally not logged.
    await store_offer_chat_message(message)

    text = (message.text or "").strip()
    if not text:
        return

    command_text = text.split(maxsplit=1)[0].split("@", 1)[0]
    if command_text != "/off":
        return

    args = text.split()
    connection_id = message.business_connection_id
    if not connection_id:
        return

    if len(args) != 2:
        await send_business_message(
            connection_id,
            message.chat.id,
            "Формат команды: /off CODE",
        )
        return

    code = args[1].lower()

    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, is_enabled, can_reply FROM business_connections WHERE connection_id = ?",
            (connection_id,),
        )
        connection = await cur.fetchone()

    if not connection or not connection[1]:
        return

    owner_id, _enabled, can_reply = connection
    sender_id = message.from_user.id if message.from_user else None

    if sender_id != owner_id:
        return

    if not await has_access(owner_id):
        return

    if not can_reply:
        await send_business_message(
            connection_id,
            message.chat.id,
            "Не хватает разрешения отвечать через Telegram Business.",
        )
        return

    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT owner_id, gift, price, currency, status FROM offers WHERE code = ?",
            (code,),
        )
        offer = await cur.fetchone()

    if not offer:
        await send_business_message(connection_id, message.chat.id, "Оффер не найден.")
        return

    offer_owner, gift, price, currency, status = offer
    if offer_owner != owner_id:
        await send_business_message(connection_id, message.chat.id, "Этот оффер создан другим аккаунтом.")
        return
    if status != "pending":
        await send_business_message(connection_id, message.chat.id, f"Оффер уже обработан: {status}")
        return

    sender_username = message.from_user.username if message.from_user else None
    if sender_username:
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute(
                "UPDATE offers SET owner_username = ? WHERE code = ?",
                (sender_username.lstrip("@").strip(), code),
            )
            await db.commit()

    owner_name = sender_username or (message.from_user.first_name if message.from_user else "Пользователь")
    offer_text = format_offer(gift, price, currency, owner_name)
    # Save the exact Business message coordinates so later button actions
    # edit the original offer, rather than relying on callback.message metadata.
    try:
        sent_message = await bot.send_message(
            chat_id=message.chat.id,
            text=offer_text,
            business_connection_id=connection_id,
            reply_markup=offer_keyboard(code),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                is_disabled=False,
                prefer_large_media=True,
                show_above_text=True,
            ),
        )
    except TelegramRetryAfter as exc:
        print(f"Business offer send failed: code={code}, retry_after={exc.retry_after}")
        return
    except TelegramAPIError as exc:
        print(f"Business offer send failed: code={code}, error={exc}")
        return

    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            """UPDATE offers
               SET recipient_id = ?, business_connection_id = ?, offer_message_id = ?
               WHERE code = ?""",
            (message.chat.id, connection_id, sent_message.message_id, code),
        )
        await db.commit()

    try:
        await bot.delete_business_messages(
            business_connection_id=connection_id,
            message_ids=[message.message_id],
        )
    except (TelegramAPIError, AttributeError) as exc:
        print(f"Could not delete Business command message: {exc}")


# ---------------- OFFER BUTTONS ----------------

@dp.callback_query(F.data.startswith("offer:"))
async def handle_offer_button(callback: CallbackQuery):
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return

    _, action, code = parts
    
    if action in {"how_to", "transfer_info"}:
        await callback.answer(
            "Откройте меню «Отправить подарок» Telegram. Выберите получателя и нужный подарок. Ссылка на NFT остаётся в сообщении.",
            show_alert=True
        )
        return

    if action == "no_user":
        await callback.answer(
            "У покупателя не установлен @username в профиле!\n"
            "Установите публичный юзернейм в настройках профиля Telegram.",
            show_alert=True
        )
        return

    if action not in {"accept", "reject"}:
        await callback.answer("Неизвестное действие.", show_alert=True)
        return

    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            """SELECT status, recipient_id, gift, price, currency, owner_id,
                      owner_username, business_connection_id, offer_message_id
               FROM offers WHERE code = ?""",
            (code,),
        )
        row = await cur.fetchone()

        if not row:
            await callback.answer("Оффер не найден.", show_alert=True)
            return

        (status, recipient_id, gift, price, currency, owner_id, owner_username,
         saved_connection_id, saved_message_id) = row

        if recipient_id is None:
            await callback.answer(
                "Это предварительный просмотр. Оффер ещё не отправлен получателю.",
                show_alert=True,
            )
            return

        if callback.from_user.id != recipient_id:
            await callback.answer(
                "Эту кнопку может нажать только получатель оффера.",
                show_alert=True,
            )
            return

        if status != "pending":
            await callback.answer("Оффер уже обработан.", show_alert=True)
            return

        # Claim the offer atomically while editing the message. Final status is
        # committed only after Telegram confirms the message update.
        processing_status = "processing_accepted" if action == "accept" else "processing_rejected"
        cur = await db.execute(
            "UPDATE offers SET status = ? WHERE code = ? AND status = 'pending' "
            "AND recipient_id = ?",
            (processing_status, code, callback.from_user.id),
        )
        await db.commit()

        if cur.rowcount != 1:
            await callback.answer("Оффер уже обработан.", show_alert=True)
            return

    if callback.message:
        if action == "accept":
            await callback.answer(
                "Внимание!\nСледуйте инструкциям, чтобы не потерять подарок и получить оплату.\nНажмите ОК, если вы прочитали предупреждение.",
                show_alert=True
            )
            
            # Replace the word "Подарок" with a clickable NFT title/link.
            gift_raw = str(gift).strip()
            if gift_raw.startswith("http://") or gift_raw.startswith("https://"):
                gift_url = gift_raw
            elif "t.me/" in gift_raw:
                gift_url = "https://" + gift_raw.lstrip("/")
            else:
                gift_url = None

            if gift_url and "t.me/nft/" in gift_url:
                nft_part = gift_url.split("t.me/nft/", 1)[1].strip("/")
                if "-" in nft_part:
                    nft_name, nft_number = nft_part.rsplit("-", 1)
                    gift_label_text = f"{nft_name} #{nft_number}"
                else:
                    gift_label_text = nft_part
                gift_label = f'<a href="{html.escape(gift_url, quote=True)}"><b>{html.escape(gift_label_text)}</b></a>'
            elif gift_url:
                gift_label = f'<a href="{html.escape(gift_url, quote=True)}"><b>Открыть NFT</b></a>'
            else:
                gift_label = f'<b>{html.escape(gift_raw)}</b>'

            updated_text = (
                f"🎁 {gift_label} <b>успешно продан!</b>\n"
                f"Передайте подарок для зачисления {html.escape(str(price))} {format_currency(currency)}"
            )

            # Telegram's documented deep link opens the native Settings > Send a Gift menu.
            # Keep the NFT link in the message text above; the button should open the gift menu.
            gift_btn = InlineKeyboardButton(
                text="🎁 Передать NFT",
                url="tg://settings/send-gift"
            )

            new_markup = InlineKeyboardMarkup(inline_keyboard=[
                [gift_btn],
                [InlineKeyboardButton(text="❓ Как передать?", callback_data=f"offer:how_to:{code}")]
            ])
            
            edit_chat_id = recipient_id
            edit_message_id = saved_message_id or callback.message.message_id
            edit_connection_id = saved_connection_id or getattr(callback.message, "business_connection_id", None)
            edited = await edit_offer_message(
                chat_id=edit_chat_id,
                message_id=edit_message_id,
                connection_id=edit_connection_id,
                text=updated_text,
                reply_markup=new_markup,
            )
            async with aiosqlite.connect(config.DB_PATH) as db:
                if edited:
                    await db.execute(
                        "UPDATE offers SET status = 'accepted' WHERE code = ? AND status = 'processing_accepted'",
                        (code,),
                    )
                    cur = await db.execute(
                        "SELECT tracking_enabled FROM users WHERE user_id = ?",
                        (owner_id,),
                    )
                    tracking_row = await cur.fetchone()
                    gift_text = str(gift).strip()
                    gift_ref = gift_text
                    if gift_ref.startswith("https://t.me/nft/"):
                        gift_ref = gift_ref.split("https://t.me/nft/", 1)[1].strip("/")
                    elif gift_ref.startswith("http://t.me/nft/"):
                        gift_ref = gift_ref.split("http://t.me/nft/", 1)[1].strip("/")
                    elif gift_ref.startswith("t.me/nft/"):
                        gift_ref = gift_ref.split("t.me/nft/", 1)[1].strip("/")
                    if tracking_row and tracking_row[0] and gift_ref and gift_ref != gift_text or (tracking_row and tracking_row[0] and gift_ref.startswith("t.me/nft/")):
                        await db.execute(
                            "INSERT OR REPLACE INTO tracked_offers (offer_code, user_id, gift_ref, owner_username, last_owner_username, notified) VALUES (?, ?, ?, ?, NULL, 0)",
                            (code, owner_id, gift_ref, (owner_username or "").lstrip("@").lower()),
                        )
                        await db.execute(
                            "DELETE FROM tracked_offers WHERE user_id = ? AND offer_code NOT IN (SELECT offer_code FROM tracked_offers WHERE user_id = ? ORDER BY added_at DESC LIMIT 5)",
                            (owner_id, owner_id),
                        )
                else:
                    await db.execute(
                        "UPDATE offers SET status = 'pending' WHERE code = ? AND status = 'processing_accepted'",
                        (code,),
                    )
                await db.commit()
            if not edited and callback.message:
                await callback.message.answer("⚠️ Telegram не обновил сообщение. Оффер оставлен активным — попробуйте ещё раз.")
            return

        else:
            await callback.answer("Решение сохранено.")
            status_text = "❌ <b>Предложение отклонено</b>"
            original_text = callback.message.html_text or callback.message.text or ""
            
            marker = "\n\nСтатус: "
            if marker in original_text:
                original_text = original_text.split(marker, 1)[0]
            updated_text = f"{original_text}\n\n{status_text}"

            edited = await edit_offer_message(
                chat_id=recipient_id,
                message_id=saved_message_id or callback.message.message_id,
                connection_id=saved_connection_id or getattr(callback.message, "business_connection_id", None),
                text=updated_text,
                reply_markup=None,
            )
            async with aiosqlite.connect(config.DB_PATH) as db:
                if edited:
                    await db.execute(
                        "UPDATE offers SET status = 'rejected' WHERE code = ? AND status = 'processing_rejected'",
                        (code,),
                    )
                else:
                    await db.execute(
                        "UPDATE offers SET status = 'pending' WHERE code = ? AND status = 'processing_rejected'",
                        (code,),
                    )
                await db.commit()
            if not edited and callback.message:
                await callback.message.answer("⚠️ Telegram не обновил сообщение. Оффер оставлен активным — попробуйте ещё раз.")


# ---------------- ADMIN ----------------

# Администраторы, которые нажали кнопку рассылки и ожидают отправки сообщения.
awaiting_broadcast = set()
awaiting_see_tg_token = set()


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 See.tg API-токен", callback_data="admin:api_token")],
        [InlineKeyboardButton(text="📣 Рассылка всем пользователям", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="📊 Статистика офферов", callback_data="admin:stats"),
         InlineKeyboardButton(text="📋 Последние офферы", callback_data="admin:offers")],
    ])


@dp.callback_query(F.data.startswith("admin:"))
async def admin_menu_callback(callback: CallbackQuery):
    # Проверяем ID администратора, а также учитываем возможный строковый ID в config.py.
    if not (
        is_admin(callback.from_user.id)
        or str(callback.from_user.id) in {str(admin_id) for admin_id in config.ADMIN_IDS}
    ):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return

    action = (callback.data or "").split(":", 1)[1]

    if action == "back":
        await callback.answer()
        if callback.message:
            await callback.message.answer("⚙️ Панель администратора", reply_markup=admin_keyboard())
        return

    if action == "api_token":
        awaiting_see_tg_token.add(callback.from_user.id)
        current_token = await get_app_setting("see_tg_api_token")
        await callback.answer()
        if callback.message:
            status = "Токен уже сохранён. Отправь новый, чтобы заменить его." if current_token else "Токен ещё не добавлен."
            await callback.message.answer(
                "🔑 <b>Настройка See.tg API</b>\n\n"
                f"{status}\n"
                "Отправь API-токен следующим сообщением. Для отмены напиши /cancel.",
                parse_mode="HTML",
            )
        return

    if action == "api_token_delete":
        await set_app_setting("see_tg_api_token", None)
        await callback.answer("Токен удалён.", show_alert=True)
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=admin_keyboard())
        return

    if action == "broadcast":
        awaiting_broadcast.add(callback.from_user.id)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "📣 <b>Рассылка всем пользователям</b>\n\n"
                "Отправь следующим сообщением текст, фото, видео или другой материал — "
                "я перешлю его всем пользователям из базы.\n"
                "Для отмены нажми кнопку ниже.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="❌ Отмена", callback_data="admin:broadcast_cancel")
                ]])
            )
        return

    if action == "broadcast_cancel":
        awaiting_broadcast.discard(callback.from_user.id)
        await callback.answer("Рассылка отменена.")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=admin_keyboard())
        return

    if action == "stats":
        await callback.answer()
        if callback.message:
            await admin_stats(callback.message)
        return

    if action == "offers":
        await callback.answer()
        if callback.message:
            await admin_offers(callback.message)
        return


@dp.message(lambda message: message.from_user and message.from_user.id in awaiting_see_tg_token)
async def process_see_tg_token(message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        awaiting_see_tg_token.discard(user_id)
        return

    value = (message.text or "").strip()
    if value.lower() in {"/cancel", "отмена"}:
        awaiting_see_tg_token.discard(user_id)
        await message.answer("Ввод токена отменён.", reply_markup=admin_keyboard())
        return
    if not value or value.startswith("/") or len(value) > 1000 or any(ch.isspace() for ch in value):
        await message.answer("Пришли корректный API-токен одним сообщением без пробелов или напиши /cancel.")
        return

    await set_app_setting("see_tg_api_token", value)
    awaiting_see_tg_token.discard(user_id)
    # Best effort: remove the secret from the chat history.
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    await bot.send_message(
        user_id,
        "✅ See.tg API-токен сохранён в базе данных. Мониторинг будет использовать его автоматически.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Удалить токен", callback_data="admin:api_token_delete")
        ], [InlineKeyboardButton(text="↩️ Панель администратора", callback_data="admin:back")]])
    )


@dp.message(lambda message: message.from_user and message.from_user.id in awaiting_broadcast)
async def process_broadcast(message: Message):
    if not is_admin(message.from_user.id):
        awaiting_broadcast.discard(message.from_user.id)
        return

    if message.text and message.text.strip().lower() in {"/cancel", "отмена"}:
        awaiting_broadcast.discard(message.from_user.id)
        await message.answer("Рассылка отменена.", reply_markup=admin_keyboard())
        return

    awaiting_broadcast.discard(message.from_user.id)

    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        user_rows = await cur.fetchall()

    user_ids = [row[0] for row in user_rows]
    if not user_ids:
        await message.answer("В базе пока нет пользователей.", reply_markup=admin_keyboard())
        return

    await message.answer(f"Начинаю рассылку для {len(user_ids)} пользователей…")
    sent = 0
    failed = 0

    for user_id in user_ids:
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
                sent += 1
            except TelegramAPIError:
                failed += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.04)

    await message.answer(
        "✅ <b>Рассылка завершена</b>\n\n"
        f"Успешно отправлено: <b>{sent}</b>\n"
        f"Не удалось отправить: <b>{failed}</b>",
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    await message.answer(
        "<b>⚙️ Панель администратора</b>\n\n"
        "<b>Выдача доступа:</b>\n"
        "<code>/add @username 24</code> — обычный доступ на 24 часа\n"
        "<code>/add ID 24</code> — обычный доступ по Telegram ID\n"
        "<code>/add ID 24 f</code> — доступ с отслеживанием 5 последних офферов\n\n"
        "<b>Управление доступом:</b>\n"
        "<code>/remove @username</code> — отозвать доступ по username\n"
        "<code>/remove ID</code> — отозвать доступ по ID\n\n"
        "<b>Офферы:</b>\n"
        "<code>/create ссылка сумма валюта</code> — создать оффер\n"
        "<code>/myoffers</code> — мои офферы\n"
        "<code>/off CODE</code> — показать/отправить оффер\n\n"
        "<b>Администрирование:</b>\n"
        "<code>/adminstats</code> — общая статистика\n"
        "<code>/adminoffers</code> — последние 20 офферов\n"
        "<code>/listf</code> — пользователи с отслеживанием, статистика и список NFT\n"
        "<code>/deletef ID</code> — убрать пользователя из /listf и отключить NFT-отслеживание\n"
        "<code>/seen CODE</code> — переписка по принятому офферу пользователя с f\n"
        "Кнопки ниже: API-токен, рассылка, статистика и последние офферы.",
        parse_mode="HTML",
        reply_markup=admin_keyboard()
    )


@dp.message(Command("adminstats"))
async def admin_stats(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM offers")
        total = (await cur.fetchone())[0]

        cur = await db.execute("SELECT status, COUNT(*) FROM offers GROUP BY status")
        rows = await cur.fetchall()

    details = "\n".join(f"{status}: {count}" for status, count in rows) or "Записей пока нет."
    await message.answer(f"Всего офферов: {total}\n{details}")


@dp.message(Command("adminoffers"))
async def admin_offers(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("""
            SELECT code, owner_id, gift, price, currency, status, created_at
            FROM offers
            ORDER BY created_at DESC
            LIMIT 20
        """)
        rows = await cur.fetchall()

    if not rows:
        await message.answer("Офферов пока нет.")
        return

    result = []
    for code, owner, gift, price, currency, status, created in rows:
        result.append(
            f"{gift} — {price} {currency}\n"
            f"Код: {code}\n"
            f"Владелец: {owner}\n"
            f"Статус: {status}\n"
            f"Создан: {created}"
        )

    await message.answer("\n\n".join(result))


@dp.message(Command("deletef"))
async def delete_tracking_user(message: Message, command: CommandObject):
    """Disable NFT tracking for a user and hide them from /listf without deleting their data/access."""
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    raw_id = (command.args or "").strip()
    if not raw_id.isdigit():
        await message.answer(
            "Формат: <code>/deletef ID</code>\n"
            "Команда отключает отслеживание и убирает пользователя из /listf. "
            "Обычный доступ и история офферов сохраняются.",
            parse_mode="HTML",
        )
        return

    user_id = int(raw_id)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT username, tracking_enabled FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if not row:
            await message.answer(f"❌ Пользователь с ID <code>{user_id}</code> не найден.", parse_mode="HTML")
            return
        username, was_enabled = row
        if not was_enabled:
            await message.answer(
                f"ℹ️ Для <code>{user_id}</code> отслеживание уже отключено; в /listf он не отображается.",
                parse_mode="HTML",
            )
            return
        await db.execute(
            "UPDATE users SET tracking_enabled = 0 WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()

    label = f"@{html.escape(username)}" if username else "без username"
    await message.answer(
        "✅ Пользователь убран из <code>/listf</code>.\n"
        f"ID: <code>{user_id}</code> ({label})\n"
        "Отслеживание NFT остановлено. Обычный доступ, офферы и сохранённая переписка не удалены.\n"
        "Чтобы вернуть отслеживание, снова выдай доступ командой <code>/add ID время f</code>.",
        parse_mode="HTML",
    )


@dp.message(Command("listf"))
async def list_tracked_users(message: Message):
    """Admin report: users with /add ... f, their offer stats and tracked NFTs."""
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("""
            SELECT u.user_id, u.username, u.access_until,
                   COUNT(DISTINCT o.code) AS offers_total,
                   SUM(CASE WHEN o.status = 'accepted' THEN 1 ELSE 0 END) AS offers_accepted
            FROM users u
            LEFT JOIN offers o ON o.owner_id = u.user_id
            WHERE u.tracking_enabled = 1
            GROUP BY u.user_id, u.username, u.access_until
            ORDER BY u.access_until DESC
        """)
        users = await cur.fetchall()

        cur = await db.execute("""
            SELECT t.user_id, t.offer_code, t.gift_ref, t.owner_username,
                   t.last_owner_username, t.notified, o.gift, o.price, o.currency, o.status
            FROM tracked_offers t
            LEFT JOIN offers o ON o.code = t.offer_code
            ORDER BY t.user_id, t.added_at DESC
        """)
        tracked_rows = await cur.fetchall()

    if not users:
        await message.answer("Пользователей с отслеживанием нет. Выдай доступ командой <code>/add ID время f</code>.", parse_mode="HTML")
        return

    tracked_by_user = {}
    for row in tracked_rows:
        tracked_by_user.setdefault(row[0], []).append(row)

    total_created = sum(row[3] or 0 for row in users)
    total_accepted = sum(row[4] or 0 for row in users)
    total_tracked = sum(len(tracked_by_user.get(row[0], [])) for row in users)
    parts = [
        "🧾 <b>Пользователи с отслеживанием (/listf)</b>",
        f"Пользователей: <b>{len(users)}</b>",
        f"Всего создано офферов: <b>{total_created}</b>",
        f"Принято офферов: <b>{total_accepted}</b>",
        f"NFT сейчас в списках отслеживания: <b>{total_tracked}</b>",
    ]

    now = int(time.time())
    for user_id, username, access_until, offers_total, offers_accepted in users:
        label = f"@{username}" if username else "без username"
        if access_until and access_until > now:
            access_text = "активен"
        else:
            access_text = "истёк"
        parts.append(
            f"\n👤 <b>{html.escape(label)}</b> (<code>{user_id}</code>)\n"
            f"Доступ: {access_text}\n"
            f"Создано офферов: <b>{offers_total or 0}</b>\n"
            f"Принято: <b>{offers_accepted or 0}</b>"
        )
        user_tracked = tracked_by_user.get(user_id, [])
        if not user_tracked:
            parts.append("Отслеживаемые NFT: пока нет принятых офферов с NFT-ссылками.")
            continue
        parts.append(f"Отслеживаемые NFT ({len(user_tracked)}):")
        for row in user_tracked:
            _, code, gift_ref, expected_owner, last_owner, notified, gift, price, currency, status = row
            nft_name = gift or gift_ref
            state = "уведомление отправлено" if notified else "ожидает передачи"
            parts.append(
                f"• <b>{html.escape(str(nft_name))}</b>\n"
                f"  Ссылка/ID: <code>{html.escape(str(gift_ref))}</code>\n"
                f"  Оффер: <code>{html.escape(str(code))}</code> | {html.escape(str(price or ''))} {html.escape(str(currency or ''))}\n"
                f"  Статус: {html.escape(str(status or 'неизвестен'))}; {state}\n"
                f"  Ожидаемый владелец: @{html.escape(str(expected_owner or 'не указан'))}"
                + (f"; последний владелец: @{html.escape(str(last_owner))}" if last_owner else "")
            )

    # Telegram limits a message to 4096 characters; split only between report lines.
    text = "\n".join(parts)
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3800 and current:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        chunks.append(current)
    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)


# ---------------- FALLBACK ----------------

@dp.message()
async def unhandled_message(message: Message):
    if message.chat.type == "private" and message.text:
        if message.from_user:
            await track_user(message.from_user.id, message.from_user.username)
        if message.text.startswith("/"):
            try:
                await message.answer(
                    "Команда не распознана. Отправь /start для списка команд."
                )
            except TelegramNetworkError as exc:
                # Temporary network/DNS/connectivity failure: log it, don't crash polling.
                print(f"Could not send fallback response: {exc}")


# ---------------- NFT OWNERSHIP MONITOR ----------------

def normalize_gift_ref(gift_ref: str):
    """Convert Telegram gift slug like TamaGadget-67325 to a comparable key."""
    value = str(gift_ref or '').strip().rstrip('/')
    if '/nft/' in value:
        value = value.split('/nft/', 1)[1]
    elif value.startswith('t.me/nft/'):
        value = value.split('t.me/nft/', 1)[1]
    value = value.split('?', 1)[0]
    match = re.match(r'^(.+?)[-_](\d+)$', value)
    if not match:
        return None
    return (re.sub(r'[^a-z0-9]', '', match.group(1).lower()), int(match.group(2)))


def event_matches_gift(event_payload, gift_ref: str) -> bool:
    """Match a See.tg event to a tracked Telegram gift reference."""
    target = normalize_gift_ref(gift_ref)
    if not target or not isinstance(event_payload, dict):
        return False

    def walk(value):
        if isinstance(value, dict):
            slug = value.get('slug') or value.get('gift_slug') or value.get('ref')
            number = value.get('num') or value.get('number') or value.get('gift_num')
            if slug is not None and number is not None:
                candidate = normalize_gift_ref(f'{slug}-{number}')
                if candidate == target:
                    return True
            # Some event variants provide the complete reference in one field.
            for key in ('gift_ref', 'gift', 'name'):
                item = value.get(key)
                if isinstance(item, str) and normalize_gift_ref(item) == target:
                    return True
            return any(walk(v) for v in value.values() if isinstance(v, (dict, list)))
        if isinstance(value, list):
            return any(walk(v) for v in value)
        return False

    return walk(event_payload)


def event_owner_label(event_payload) -> str | None:
    """Best-effort public owner label; guest feed may redact it."""
    keys = ('owner_name', 'new_owner_name', 'to_name', 'recipient_name', 'username')
    def walk(value):
        if isinstance(value, dict):
            for key in keys:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for child in value.values():
                if isinstance(child, (dict, list)):
                    found = walk(child)
                    if found:
                        return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return None
    return walk(event_payload)


async def handle_transfer_event(payload: dict):
    """Notify admins once when a transfer event matches one of tracked gifts."""
    event_type = str(payload.get('event') or payload.get('type') or '').lower()
    if event_type != 'transfer':
        return

    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            """SELECT t.offer_code, t.user_id, t.gift_ref, t.owner_username
               FROM tracked_offers t
               JOIN users u ON u.user_id = t.user_id
               WHERE t.notified = 0 AND u.tracking_enabled = 1"""
        )
        tracked = await cur.fetchall()

    for offer_code, user_id, gift_ref, expected_username in tracked:
        if not event_matches_gift(payload, gift_ref):
            continue

        owner_label = event_owner_label(payload)
        # Guest WebSocket events can redact profile identifiers. We alert on the
        # matching transfer, but never claim that the new owner is the offer author.
        async with aiosqlite.connect(config.DB_PATH) as db:
            cur = await db.execute(
                """UPDATE tracked_offers
                   SET notified = 1, last_owner_username = ?
                   WHERE offer_code = ? AND notified = 0""",
                ((owner_label or 'скрыт гостевым потоком')[:200], offer_code),
            )
            await db.commit()
            if not cur.rowcount:
                continue
            cur = await db.execute("SELECT gift FROM offers WHERE code = ?", (offer_code,))
            offer_row = await cur.fetchone()

        gift_label = offer_row[0] if offer_row else gift_ref
        owner_text = html.escape(owner_label) if owner_label else 'не раскрыт See.tg (гостевой режим)'
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(
                    int(admin_id),
                    "🔔 <b>Обнаружена передача отслеживаемого NFT</b>\n\n"
                    f"Оффер: <code>{html.escape(offer_code)}</code>\n"
                    f"Пользователь: <code>{user_id}</code>\n"
                    f"Подарок: <code>{html.escape(str(gift_label))}</code>\n"
                    f"Новый владелец по событию: <b>{owner_text}</b>\n\n"
                    f"Ожидаемый автор оффера: @{html.escape(str(expected_username or 'не указан'))}\n"
                    "⚠️ Гостевой WebSocket может скрывать ID/имя профиля. Передача обнаружена, "
                    "но совпадение владельца с автором оффера автоматически не подтверждено.",
                    parse_mode='HTML',
                )
            except TelegramAPIError as exc:
                print(f"Could not notify admin {admin_id}: {exc}")


async def nft_tracking_loop():
    """Listen to free See.tg guest WebSocket transfer events and reconnect as needed."""
    import aiohttp
    import json

    ws_url = 'wss://live.see.tg/v1/ws'
    backoff = 3
    while True:
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=None)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(ws_url, heartbeat=None, autoping=True) as ws:
                    await ws.send_json({"type": "subscribe", "events": ["transfer"]})
                    print('See.tg guest WebSocket connected; subscribed to transfer events')
                    backoff = 3
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            try:
                                payload = json.loads(message.data)
                            except (TypeError, ValueError):
                                continue
                            if isinstance(payload, dict):
                                if payload.get('type') == 'error':
                                    print(f"See.tg WebSocket subscription error: {payload}")
                                else:
                                    await handle_transfer_event(payload)
                        elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"See.tg WebSocket connection error: {exc}")

        print(f"See.tg WebSocket reconnecting in {backoff}s")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


# ---------------- RUN ----------------

async def main():
    await init_db()
    print(f"Bot started. Database: {config.DB_PATH}")
    # NFT receipt is detected from Telegram Business service messages, not See.tg polling.
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
