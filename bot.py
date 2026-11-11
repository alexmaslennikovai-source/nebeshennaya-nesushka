"""
Полный код Telegram-бота и сервера базы данных для челленджа «НеБешенная НеСушка» (2 ноября — 29 декабря).

Что делает этот единый файл bot.py:
1. Строгий ВАЙТЛИСТ (ALLOWED_USER_IDS): доступ к боту, API и Mini App имеют только 2 прописанных Telegram ID (вы и друг).
2. Создаёт и обслуживает базу данных SQLite (nesushka128.db) + поддерживает авто-восстановление данных с телефона
   (защита от сброса диска на бесплатном тарифе Render.com!).
3. Содержит эндпоинт /ping и фоновый авто-пинг (Keep-Alive), чтобы бесплатный сервер Render НЕ засыпал!
4. Запускает встроенный веб-сервер (aiohttp), к которому напрямую обращается Mini App (index.html):
   - При запросе от участника сервер отдаёт ему его профиль, а по другу отдаёт ТОЛЬКО тренировки (+1 балл).
   - Личные килограммы друга НИКОГДА не передаются по сети!
   - После 29 декабря 00:00 сервер рассчитывает и отдаёт процентную дельту веса (максимум 100 баллов, без перевыполнения).
5. Отправляет мотивирующие слова после внесения каждой тренировки.
6. Автоматически напоминает в пятницу и воскресенье об обязательном минимуме (1 тренировка в неделю) и о взвешивании 27–29 декабря.

Установка библиотек:
pip install aiogram aiohttp apscheduler
"""

import asyncio
import json
import logging
import os
import random
import sqlite3
from datetime import datetime
from typing import Callable, Dict, Any, Awaitable

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, BaseMiddleware, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# =====================================================================
# НАСТРОЙКИ БОТА, СЕРВЕРА И ВАЙТЛИСТ УЧАСТНИКОВ
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9295051811:AAGjmaRxiBM_jLFXi99Fb1adpK-_nhM8T5Y")

# Ссылка на ваш сервер Render (или GitHub Pages).
# Когда получите ссылку в Render (например, https://nesushka-128.onrender.com), вставьте её сюда или в переменную среды MINI_APP_URL
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://alexmaslennikovai-source.github.io/nebeshennaya-nesushka")

# Порт встроенного веб-сервера (Render автоматически передаёт переменную PORT)
PORT = int(os.getenv("PORT", 8080))

# ВАЙТЛИСТ: Доступ открыт только для этих двух Telegram ID (вы и друг)
ALLOWED_USER_IDS = [
    5835424498,  # Участник 1
    7647922685,  # Участник 2
]

DB_PATH = os.getenv("DB_PATH", "nesushka128.db")
FINALE_DATES = ("2026-12-27", "2026-12-28", "2026-12-29")
FINALE_UNLOCK_DT = datetime(2026, 12, 29, 0, 0, 0)

WORKOUT_TYPE_LABELS = {
    "home": "🏠 Дома",
    "gym": "🏋️‍♀️ Тренажерка",
    "dance": "💃 Танцы",
    "other": "⚡ Другое",
}

MOTIVATION_MESSAGES = [
    "🔥 Отличная работа! Ещё +1 балл в копилку 128! Дисциплина всегда побеждает лень.",
    "💪 Так держать! Каждая тренировка приближает тебя к победе в «НеБешенной НеСушке»!",
    "⚡ Мощно! Друг увидит твою отметку в календаре и поймёт, что ты настроен(а) серьёзно!",
    "🏆 Ты сегодня сильнее, чем вчера! Обязательный минимум закрывается без единого шанса для пропусков.",
    "🚀 Великолепный темп! 29 декабря таймер откроет итоги, и твои усилия окупятся на все 100%!",
    "✨ Твоя сила воли восхищает! Восстанавливай силы и продолжай движение к 128 баллам!"
]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# =====================================================================
# 1. АВТОМАТИЧЕСКОЕ СОЗДАНИЕ И РАБОТА С БАЗОЙ ДАННЫХ SQLITE
# =====================================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            user_id INTEGER PRIMARY KEY,
            name TEXT DEFAULT 'Участник',
            avatar TEXT DEFAULT '🔥',
            start_weight REAL DEFAULT 68.0,
            target_weight REAL DEFAULT 62.0,
            registered INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS workouts (
            user_id INTEGER,
            workout_date TEXT,
            workout_type TEXT,
            duration INTEGER,
            comment TEXT DEFAULT '',
            PRIMARY KEY (user_id, workout_date)
        )
    """)
    # Авто-миграция для уже созданных таблиц: добавляем колонку comment, если её ещё нет
    try:
        cur.execute("ALTER TABLE workouts ADD COLUMN comment TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS weight_logs (
            user_id INTEGER,
            log_date TEXT,
            weight REAL,
            PRIMARY KEY (user_id, log_date)
        )
    """)
    conn.commit()
    conn.close()


def db_upsert_participant(user_id: int, name: str, avatar: str, start_weight: float, target_weight: float):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO participants (user_id, name, avatar, start_weight, target_weight, registered, updated_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            name = excluded.name,
            avatar = excluded.avatar,
            start_weight = excluded.start_weight,
            target_weight = excluded.target_weight,
            registered = 1,
            updated_at = excluded.updated_at
    """, (user_id, name, avatar, start_weight, target_weight, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def db_save_workout(user_id: int, date_str: str, w_type: str, duration: int, comment: str = ""):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO workouts (user_id, workout_date, workout_type, duration, comment)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, date_str, w_type, duration, comment))
    conn.commit()
    conn.close()


def db_delete_workout(user_id: int, date_str: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM workouts WHERE user_id = ? AND workout_date = ?", (user_id, date_str))
    conn.commit()
    conn.close()


def db_save_weight(user_id: int, date_str: str, weight: float):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO weight_logs (user_id, log_date, weight)
        VALUES (?, ?, ?)
    """, (user_id, date_str, weight))
    conn.commit()
    conn.close()


def db_restore_backup_from_client(user_id: int, backup_data: Dict[str, Any]):
    """
    Синхронизирует и объединяет данные из Telegram CloudStorage / LocalStorage телефона с БД SQLite на сервере.
    Никогда не затирает уже существующие записи, а дополняет их!
    """
    if not backup_data or not backup_data.get("registered"):
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO participants (user_id, name, avatar, start_weight, target_weight, registered, updated_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            name = excluded.name,
            avatar = excluded.avatar,
            start_weight = excluded.start_weight,
            target_weight = excluded.target_weight,
            registered = 1,
            updated_at = excluded.updated_at
    """, (
        user_id,
        str(backup_data.get("name", "Участник")),
        str(backup_data.get("avatar", "🔥")),
        float(backup_data.get("startWeight", 68.0)),
        float(backup_data.get("targetWeight", 62.0)),
        datetime.now().isoformat()
    ))

    workouts = backup_data.get("workouts", {})
    if isinstance(workouts, dict):
        for date_str, w_info in workouts.items():
            if isinstance(w_info, dict):
                cur.execute("""
                    INSERT OR REPLACE INTO workouts (user_id, workout_date, workout_type, duration, comment)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    user_id,
                    str(date_str),
                    str(w_info.get("type", "home")),
                    int(w_info.get("duration", 45)),
                    str(w_info.get("comment", ""))
                ))

    weight_logs = backup_data.get("weightLogs", {})
    if isinstance(weight_logs, dict):
        for date_str, w_val in weight_logs.items():
            try:
                cur.execute("""
                    INSERT OR REPLACE INTO weight_logs (user_id, log_date, weight)
                    VALUES (?, ?, ?)
                """, (user_id, str(date_str), float(w_val)))
            except (ValueError, TypeError):
                pass

    conn.commit()
    conn.close()


def db_get_player_raw(user_id: int) -> Dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT name, avatar, start_weight, target_weight, registered
        FROM participants WHERE user_id = ?
    """, (user_id,))
    row = cur.fetchone()

    cur.execute("SELECT workout_date, workout_type, duration, COALESCE(comment, '') FROM workouts WHERE user_id = ?", (user_id,))
    workouts = {r[0]: {"type": r[1], "duration": r[2], "comment": r[3]} for r in cur.fetchall()}

    cur.execute("SELECT log_date, weight FROM weight_logs WHERE user_id = ?", (user_id,))
    weight_logs = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()

    if not row:
        return {
            "registered": False,
            "name": "Участник",
            "avatar": "🔥",
            "startWeight": 68.0,
            "targetWeight": 62.0,
            "workouts": workouts,
            "weightLogs": weight_logs
        }

    return {
        "registered": bool(row[4]),
        "name": row[0],
        "avatar": row[1],
        "startWeight": row[2],
        "targetWeight": row[3],
        "workouts": workouts,
        "weightLogs": weight_logs
    }


def calculate_capped_weight_score(player_data: Dict[str, Any]) -> float:
    """Расчёт баллов за вес (по минимальному весу 27-29 декабря). Строго не более 100 баллов!"""
    start_w = float(player_data.get("startWeight", 0))
    target_w = float(player_data.get("targetWeight", 0))
    goal_delta = start_w - target_w
    if goal_delta <= 0:
        return 0.0

    logs = player_data.get("weightLogs", {})
    finale_weights = [float(logs[d]) for d in FINALE_DATES if d in logs and float(logs[d]) > 0]
    if finale_weights:
        min_w = min(finale_weights)
    elif logs:
        min_w = min(float(v) for v in logs.values() if float(v) > 0)
    else:
        min_w = start_w

    actual_lost = start_w - min_w
    if actual_lost <= 0:
        return 0.0

    raw_pct = (actual_lost / goal_delta) * 100.0
    return round(min(100.0, max(0.0, raw_pct)), 1)


# =====================================================================
# 2. HTTP API СЕРВЕР (AIOHTTP) + АНТИ-СОН (/ping) ДЛЯ RENDER
# =====================================================================
@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Accept"
    return response


async def handle_ping(request: web.Request):
    """Эндпоинт для бесплатного cron-job.org / UptimeRobot, чтобы Render никогда не уходил в сон."""
    return web.json_response({"status": "alive", "time": datetime.now().isoformat()})


async def handle_serve_miniapp(request: web.Request):
    """Отдаёт index.html напрямую с того же сервера, где крутится бот."""
    if os.path.exists("index.html"):
        return web.FileResponse("index.html")
    return web.Response(text="Mini App Backend is running! Place index.html in the same folder.", status=200)


def build_safe_state_response(user_id: int) -> Dict[str, Any]:
    friend_id = ALLOWED_USER_IDS[1] if user_id == ALLOWED_USER_IDS[0] else ALLOWED_USER_IDS[0]
    me_raw = db_get_player_raw(user_id)
    friend_raw = db_get_player_raw(friend_id)
    is_finale = datetime.now() >= FINALE_UNLOCK_DT

    # Приватность: другу НЕ передаются ни startWeight, ни targetWeight, ни weightLogs!
    friend_safe = {
        "registered": friend_raw["registered"],
        "name": friend_raw["name"] if friend_raw["registered"] else "Друг",
        "avatar": friend_raw["avatar"],
        "workouts": friend_raw["workouts"],
        "finaleWeightScore": calculate_capped_weight_score(friend_raw) if is_finale else None
    }
    return {
        "ok": True,
        "me": me_raw,
        "friend": friend_safe
    }


async def handle_api_get_state(request: web.Request):
    try:
        user_id = int(request.query.get("user_id", 0))
    except ValueError:
        user_id = 0

    if user_id not in ALLOWED_USER_IDS:
        return web.json_response({"ok": False, "error": "Not in whitelist"}, status=403)

    return web.json_response(build_safe_state_response(user_id))


async def handle_api_sync_backup(request: web.Request):
    """
    Принимает локальную копию данных с телефона участника, восстанавливает её в БД (если Render перезапускался)
    и сразу возвращает актуальное состояние обоих игроков.
    """
    data = await request.json()
    user_id = int(data.get("user_id", 0))
    if user_id not in ALLOWED_USER_IDS:
        return web.json_response({"ok": False, "error": "Not in whitelist"}, status=403)

    local_me = data.get("local_me")
    if isinstance(local_me, dict):
        db_restore_backup_from_client(user_id, local_me)

    return web.json_response(build_safe_state_response(user_id))


async def handle_api_register(request: web.Request):
    data = await request.json()
    user_id = int(data.get("user_id", 0))
    if user_id not in ALLOWED_USER_IDS:
        return web.json_response({"ok": False, "error": "Not in whitelist"}, status=403)

    db_upsert_participant(
        user_id=user_id,
        name=str(data.get("name", "Участник")),
        avatar=str(data.get("avatar", "🔥")),
        start_weight=float(data.get("startWeight", 68.0)),
        target_weight=float(data.get("targetWeight", 62.0))
    )
    return web.json_response({"ok": True})


async def handle_api_workout(request: web.Request):
    data = await request.json()
    user_id = int(data.get("user_id", 0))
    if user_id not in ALLOWED_USER_IDS:
        return web.json_response({"ok": False, "error": "Not in whitelist"}, status=403)

    date_str = str(data.get("date", "2026-11-02"))
    action = str(data.get("action", "save"))

    if action == "delete":
        db_delete_workout(user_id, date_str)
    else:
        w_type = str(data.get("type", "home"))
        duration = int(data.get("duration", 45))
        comment = str(data.get("comment", "")).strip()
        db_save_workout(user_id, date_str, w_type, duration, comment)

        quote = random.choice(MOTIVATION_MESSAGES)
        type_label = WORKOUT_TYPE_LABELS.get(w_type, "Тренировка")
        comment_line = f"📝 Что делал(а): <i>{comment}</i>\n" if comment else ""
        try:
            await bot.send_message(
                user_id,
                f"✅ <b>Тренировка сохранена (+1 балл к 128)!</b>\n"
                f"📅 Дата: <b>{date_str}</b> | {type_label} ({duration} мин)\n"
                f"{comment_line}\n"
                f"{quote}",
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )
        except Exception as e:
            logging.warning(f"Не удалось отправить мотивацию в чат {user_id}: {e}")

    return web.json_response({"ok": True})


async def handle_api_weight(request: web.Request):
    data = await request.json()
    user_id = int(data.get("user_id", 0))
    if user_id not in ALLOWED_USER_IDS:
        return web.json_response({"ok": False, "error": "Not in whitelist"}, status=403)

    date_str = str(data.get("date", "2026-11-02"))
    weight = float(data.get("weight", 68.0))
    db_save_weight(user_id, date_str, weight)
    return web.json_response({"ok": True})


# =====================================================================
# 3. MIDDLEWARE ВАЙТЛИСТА ДЛЯ TELEGRAM-БОТА
# =====================================================================
class WhitelistMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if user and user.id not in ALLOWED_USER_IDS:
            deny_text = (
                "⛔ <b>Доступ закрыт!</b>\n\n"
                "Это приватный бот челленджа <b>«НеБешенная НеСушка»</b>.\n"
                "Доступ к боту и Mini App разрешён только участникам из вайтлиста.\n\n"
                f"🔑 Ваш Telegram ID: <code>{user.id}</code>"
            )
            if isinstance(event, types.Message):
                await event.answer(deny_text, parse_mode="HTML")
            elif isinstance(event, types.CallbackQuery):
                await event.answer("⛔ Доступ закрыт (вы не в вайтлисте)", show_alert=True)
            return
        return await handler(event, data)


dp.message.outer_middleware(WhitelistMiddleware())
dp.callback_query.outer_middleware(WhitelistMiddleware())


# =====================================================================
# 4. КОМАНДЫ И КНОПКИ TELEGRAM-БОТА
# =====================================================================
def get_webapp_url_with_api() -> str:
    """Автоматически передаёт адрес API сервера в параметре URL, чтобы Mini App всегда знал, где база данных."""
    base_url = (os.getenv("RENDER_EXTERNAL_URL") or MINI_APP_URL).rstrip("/")
    sep = "&" if "?" in MINI_APP_URL else "?"
    if "ваш-" not in base_url:
        return f"{MINI_APP_URL}{sep}api={base_url}"
    return MINI_APP_URL


def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔥 Открыть «НеБешенная НеСушка» (Mini App)",
                web_app=WebAppInfo(url=get_webapp_url_with_api())
            )
        ],
        [
            InlineKeyboardButton(
                text="💪 Заряд мотивации",
                callback_data="motivate_me"
            ),
            InlineKeyboardButton(
                text="📊 Правила 128 баллов",
                callback_data="rules_info"
            )
        ]
    ])


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    welcome_text = (
        f"Привет, <b>{message.from_user.first_name}</b>! 🏆\n\n"
        "Добро пожаловать в соревнование с другом <b>«НеБешенная НеСушка» (2 ноября — 29 декабря)</b>!\n\n"
        "🎯 <b>Суть челленджа:</b> набрать наиболее близкое значение к <b>128 баллам</b>:\n"
        "• <b>+1 балл</b> за каждую тренировку (план — 28 тренировок)\n"
        "• <b>Максимум 100 баллов</b> — процент выполнения твоей личной цели по сбросу веса (перевыполнение свыше 100 баллов не учитывается)\n\n"
        "🔒 <b>Строгая приватность и таймер Финала:</b>\n"
        "Твой стартовый (на 2 ноября), текущий и желаемый вес <b>не видит никто, даже друг</b>! "
        "Вы можете следить только за баллами за тренировки и датами посещений друг друга. "
        "Режим Финала (по минимальному весу в период <b>27–29 декабря</b>) откроется строго по таймеру <b>29 декабря</b>!\n\n"
        "👇 Нажми кнопку ниже, чтобы открыть календарь тренировок:"
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.callback_query(F.data == "rules_info")
async def show_rules(callback: types.CallbackQuery):
    rules_text = (
        "📋 <b>Памятка участника «НеБешенная НеСушка»:</b>\n\n"
        "1️⃣ <b>Период:</b> со 2 ноября по 27–29 декабря (8 недель).\n"
        "2️⃣ <b>Обязательный минимум:</b> минимум <b>1 тренировка в неделю</b>. "
        "Я буду присылать напоминания каждую пятницу и воскресенье!\n"
        "3️⃣ <b>Виды тренировок в календаре:</b>\n"
        "   🏠 Дома | 🏋️‍♀️ Тренажерка | 💃 Танцы | ⚡ Другое\n"
        "   Указывай длительность каждой тренировки — после сохранения ты получишь заряд мотивации, а в конце сформируется красивое Summary!\n"
        "4️⃣ <b>Финальный замер веса (27–29 декабря):</b>\n"
        "   В эти три дня заноси вес утром — система автоматически возьмёт <b>минимальное значение</b> "
        "за 27–29 декабря и рассчитает твою процентную дельту (но <b>не более 100 баллов</b>, перевыполнение не учитывается). "
        "Итоги по весу раскроются строго по таймеру <b>29 декабря</b>!"
    )
    await callback.message.answer(rules_text, parse_mode="HTML", reply_markup=get_main_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "motivate_me")
async def send_motivation_callback(callback: types.CallbackQuery):
    quote = random.choice(MOTIVATION_MESSAGES)
    await callback.message.answer(
        f"💥 <b>Мотивация «НеБешенная НеСушка»:</b>\n\n{quote}",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )
    await callback.answer("Заряд мотивации отправлен! 🔥")


@dp.message(Command("remind_test"))
async def cmd_remind_test(message: types.Message):
    await send_weekly_minimum_reminder()
    await message.answer("✅ Тестовое напоминание отправлено!")


# =====================================================================
# 5. АВТОМАТИЧЕСКИЕ НАПОМИНАНИЯ + KEEP-ALIVE ПИНГ ДЛЯ RENDER
# =====================================================================
async def send_weekly_minimum_reminder():
    """Напоминание об обязательном минимуме: 1 тренировка раз в неделю."""
    for user_id in ALLOWED_USER_IDS:
        try:
            text = (
                "🔔 <b>Напоминание «НеБешенная НеСушка»!</b>\n\n"
                "Не забудь про наш обязательный минимум — <b>хотя бы 1 тренировка в неделю</b> 💪\n\n"
                "Открой календарь в Mini App, чтобы отметить день, вид и продолжительность тренировки, "
                "а также проверить, сколько баллов за тренировки уже набрал друг!"
            )
            await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=get_main_keyboard())
        except Exception as e:
            logging.error(f"Не удалось отправить напоминание пользователю {user_id}: {e}")


async def send_finale_weighin_reminder():
    """Специальное уведомление 27, 28 и 29 декабря о финальном взвешивании."""
    now = datetime.now()
    if now.month == 12 and now.day in (27, 28, 29):
        for user_id in ALLOWED_USER_IDS:
            try:
                text = (
                    f"⭐ <b>Финальное окно взвешивания ({now.day} декабря) — «НеБешенная НеСушка»!</b>\n\n"
                    "Сегодня один из трёх решающих дней (27–29 декабря)!\n"
                    "Запиши свой утренний вес в Mini App (🔒 приватно). В итоговый зачёт пойдёт "
                    "<b>минимальный вес</b> за эти 3 дня (максимум 100 баллов за вес, перевыполнение не учитывается)!"
                )
                await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=get_main_keyboard())
            except Exception as e:
                logging.error(f"Ошибка отправки финального уведомления {user_id}: {e}")


async def self_keep_alive_ping():
    """Пингует внешний URL сервера каждые 10 минут, чтобы Render не уходил в спящий режим."""
    target_url = os.getenv("RENDER_EXTERNAL_URL") or MINI_APP_URL
    if not target_url or "ваш-" in target_url:
        return
    ping_url = target_url.rstrip("/") + "/ping"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(ping_url, timeout=10) as resp:
                logging.info(f"Keep-alive ping to {ping_url}: status {resp.status}")
    except Exception as e:
        logging.debug(f"Keep-alive ping skipped: {e}")


# =====================================================================
# 6. ЗАПУСК БОТА И ВЕБ-СЕРВЕРА БАЗЫ ДАННЫХ В ОДНОМ ПРОЦЕССЕ
# =====================================================================
async def start_web_server():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", handle_serve_miniapp)
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/api/state", handle_api_get_state)
    app.router.add_post("/api/sync_backup", handle_api_sync_backup)
    app.router.add_post("/api/register", handle_api_register)
    app.router.add_post("/api/workout", handle_api_workout)
    app.router.add_post("/api/weight", handle_api_weight)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Сервер базы данных SQLite и Mini App запущен на порту {PORT}")


async def main():
    init_db()
    await start_web_server()

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    # Каждые 10 минут — авто-пинг для предотвращения сна на Render
    scheduler.add_job(self_keep_alive_ping, trigger="interval", minutes=10)
    # Каждую пятницу в 18:00 — напоминание о тренировке на неделе
    scheduler.add_job(send_weekly_minimum_reminder, trigger="cron", day_of_week="fri", hour=18, minute=0)
    # Каждое воскресенье в 12:00 — контрольное напоминание перед закрытием недели
    scheduler.add_job(send_weekly_minimum_reminder, trigger="cron", day_of_week="sun", hour=12, minute=0)
    # 27, 28 и 29 декабря в 09:30 утра — напоминание о финальном взвешивании
    scheduler.add_job(send_finale_weighin_reminder, trigger="cron", month=12, day="27,28,29", hour=9, minute=30)
    scheduler.start()

    logging.info("Бот «НеБешенная НеСушка» запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
