"""
Личный финансовый бот для Telegram (v2).

Команды:
  /start                    - начать работу, показать меню кнопок
  /help                     - список команд

Расходы:
  /add Категория Сумма      - записать трату (например: /add Продукты 650)
  /mybudget                 - показать лимиты по категориям
  /setbudget Категория Сумма- задать/изменить лимит категории

Доходы:
  /income Источник Сумма    - записать поступление (например: /income Зарплата 80000)

Накопления:
  /savings Сумма            - добавить (или списать отрицательным) в накопления
  /setsavings Сумма         - задать цель по накоплениям на месяц

Долг:
  /debt                     - показать текущие долги и остатки
  /paydebt Сумма [Название] - записать платёж в счёт долга

Сводки:
  /status                   - остатки по категориям, накопления, долг
  /report [day|week|month]  - сводка за период (по умолчанию месяц), удобно для копирования
  /reset                    - обнулить траты и доходы текущего месяца (бюджет/накопления/долг не трогает)

Кнопки (снизу экрана в Telegram) дублируют основные действия без набора команд.

AI (GigaChat), если задан GIGACHAT_AUTH_KEY:
  - Понимает свободный текст без команд/кнопок ("кофе 500" → сам поймёт трату и категорию)
  - Отвечает на произвольные вопросы про финансы прямо в чате
  - Добавляет короткий комментарий и совет к каждой сводке (/report)

Данные хранятся в SQLite (finance_bot.db) рядом со скриптом.
ВАЖНО: на Railway без подключённого Volume файл базы стирается при каждом передеплое —
см. README.md, раздел "Постоянное хранилище".
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timedelta

import requests
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finance_bot.db")

# Бюджет по умолчанию — под старт, дальше правится командой /setbudget
DEFAULT_BUDGET = {
    "Продукты": 12000,
    "Телефон и интернет": 1500,
    "Подписки": 2000,
    "Транспорт": 3000,
    "Развлечения": 4000,
    "Вредные привычки": 4000,
    "Одежда": 1500,
    "Здоровье": 1500,
    "Красота": 3000,
}
DEFAULT_SAVINGS_TARGET = 20000
DEFAULT_DEBTS = {
    "Кредитка Т-Банк": 45221,
}

EXPENSE_CATEGORY_HINTS = list(DEFAULT_BUDGET.keys()) + ["Другое"]

GIGACHAT_AUTH_KEY = os.environ.get("GIGACHAT_AUTH_KEY")  # base64(client_id:client_secret) из кабинета
GIGACHAT_SCOPE = os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_API_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
GIGACHAT_MODEL = "GigaChat"

# ---------- Быстрая запись через iOS Shortcuts / Back Tap ----------
# Секретный ключ и твой личный Telegram ID нужны, чтобы посторонний не мог
# слать боту записи через этот адрес. См. README, раздел "Быстрый ввод (Back Tap)".
SHORTCUT_SECRET = os.environ.get("SHORTCUT_SECRET")
OWNER_USER_ID = os.environ.get("OWNER_USER_ID")
SHORTCUT_PORT = int(os.environ.get("PORT", 8080))

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["💸 Расход", "💰 Доход"],
        ["📊 Статус", "🏦 Накопления"],
        ["💳 Долг", "📋 Сводка"],
    ],
    resize_keyboard=True,
)
REPORT_PERIOD_KEYBOARD = ReplyKeyboardMarkup(
    [["День", "Неделя", "Месяц"], ["Отмена"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS budgets (
            user_id INTEGER,
            category TEXT,
            limit_amount REAL,
            PRIMARY KEY (user_id, category)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            category TEXT,
            amount REAL,
            created_at TEXT,
            month_key TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS incomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            source TEXT,
            amount REAL,
            created_at TEXT,
            month_key TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS savings (
            user_id INTEGER PRIMARY KEY,
            total REAL DEFAULT 0,
            monthly_target REAL DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            total_amount REAL,
            remaining_amount REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS category_aliases (
            user_id INTEGER,
            alias TEXT,
            canonical TEXT,
            PRIMARY KEY (user_id, alias)
        )"""
    )
    conn.commit()
    conn.close()


def current_month_key():
    return datetime.now().strftime("%Y-%m")


def ensure_user(user_id: int):
    conn = get_db()
    cur = conn.execute("SELECT 1 FROM budgets WHERE user_id = ?", (user_id,))
    if cur.fetchone() is None:
        for cat, limit in DEFAULT_BUDGET.items():
            conn.execute(
                "INSERT INTO budgets (user_id, category, limit_amount) VALUES (?, ?, ?)",
                (user_id, cat, limit),
            )
    cur = conn.execute("SELECT 1 FROM savings WHERE user_id = ?", (user_id,))
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO savings (user_id, total, monthly_target) VALUES (?, 0, ?)",
            (user_id, DEFAULT_SAVINGS_TARGET),
        )
    cur = conn.execute("SELECT 1 FROM debts WHERE user_id = ?", (user_id,))
    if cur.fetchone() is None:
        for name, amount in DEFAULT_DEBTS.items():
            conn.execute(
                "INSERT INTO debts (user_id, name, total_amount, remaining_amount) VALUES (?, ?, ?, ?)",
                (user_id, name, amount, amount),
            )
    conn.commit()
    conn.close()


def parse_amount(text: str):
    try:
        return float(text.replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


# Строка вида "Категория Сумма", где Сумма может быть простым числом,
# арифметическим выражением (73*2) или готовым итогом после "=" (449+249=698).
_BULK_LINE_PATTERN = re.compile(r"^(?P<label>.+?)\s+(?P<expr>[0-9\s+\-*/,.=]+)$")


def _safe_eval_amount(expr: str):
    expr = expr.strip()
    if "=" in expr:
        before, after = expr.rsplit("=", 1)
        expr = after.strip() or before.strip()
    expr = expr.replace(",", ".").replace(" ", "")
    if not expr or not re.fullmatch(r"[0-9.+\-*/]+", expr):
        return None
    try:
        value = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 — вход уже провалидирован regex'ом выше
        value = float(value)
        return value if value > 0 else None
    except Exception:
        return None


def parse_bulk_lines(text: str):
    """Разбирает многострочный текст на пары (категория/источник, сумма).
    Строки, которые не удалось распознать, пропускаются."""
    results = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _BULK_LINE_PATTERN.match(line)
        if not match:
            continue
        amount = _safe_eval_amount(match.group("expr"))
        if amount is None:
            continue
        label = match.group("label").strip().capitalize()
        results.append((label, amount))
    return results


# ---------- GigaChat (AI) ----------

# GigaChat использует сертификат Минцифры, который обычно не установлен в системе —
# поэтому проверка SSL отключена (verify=False). Для продакшена с повышенными
# требованиями к безопасности можно установить сертификат и включить проверку обратно.
import urllib3  # noqa: E402

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_gigachat_token_cache = {"token": None, "expires_at": 0}


def get_gigachat_token():
    now = time.time()
    if _gigachat_token_cache["token"] and _gigachat_token_cache["expires_at"] - 30 > now:
        return _gigachat_token_cache["token"]
    if not GIGACHAT_AUTH_KEY:
        return None
    try:
        resp = requests.post(
            GIGACHAT_OAUTH_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
            },
            data={"scope": GIGACHAT_SCOPE},
            verify=False,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data["access_token"]
        expires_at = data["expires_at"]
        if expires_at > 10**12:  # миллисекунды -> секунды
            expires_at = expires_at / 1000
        _gigachat_token_cache["token"] = token
        _gigachat_token_cache["expires_at"] = expires_at
        return token
    except Exception as e:
        logger.error(f"GigaChat auth error: {e}")
        return None


def call_ai(prompt: str):
    """Синхронный вызов GigaChat. Возвращает текст ответа или None при ошибке/отсутствии ключа."""
    token = get_gigachat_token()
    if not token:
        return None
    try:
        resp = requests.post(
            GIGACHAT_API_URL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json={
                "model": GIGACHAT_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
            },
            verify=False,
            timeout=25,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"GigaChat error: {e}")
        return None


def get_financial_snapshot_text(user_id: int) -> str:
    """Компактная текстовая сводка по пользователю — используется как контекст для AI."""
    conn = get_db()
    budgets = conn.execute(
        "SELECT category, limit_amount FROM budgets WHERE user_id = ?", (user_id,)
    ).fetchall()
    month = current_month_key()
    lines = []
    total_limit = 0.0
    total_spent = 0.0
    for b in budgets:
        spent = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS spent FROM expenses WHERE user_id = ? AND category = ? AND month_key = ?",
            (user_id, b["category"], month),
        ).fetchone()["spent"]
        total_limit += b["limit_amount"]
        total_spent += spent
        lines.append(f"- {b['category']}: потрачено {spent:.0f} из {b['limit_amount']:.0f} ₽")
    income_total = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM incomes WHERE user_id = ? AND month_key = ?",
        (user_id, month),
    ).fetchone()["total"]
    savings_row = conn.execute(
        "SELECT total, monthly_target FROM savings WHERE user_id = ?", (user_id,)
    ).fetchone()
    debts = conn.execute(
        "SELECT name, remaining_amount FROM debts WHERE user_id = ? AND remaining_amount > 0",
        (user_id,),
    ).fetchall()
    conn.close()
    debt_text = (
        ", ".join(f"{d['name']} — осталось {d['remaining_amount']:.0f} ₽" for d in debts)
        or "долгов нет"
    )
    return (
        "Категории расходов в этом месяце:\n"
        + "\n".join(lines)
        + f"\n\nВсего потрачено: {total_spent:.0f} из {total_limit:.0f} ₽"
        + f"\nДоходов за месяц: {income_total:.0f} ₽"
        + f"\nНакоплено: {savings_row['total']:.0f} ₽ (цель на месяц: {savings_row['monthly_target']:.0f} ₽)"
        + f"\nДолги: {debt_text}"
    )


async def classify_free_text(text: str):
    """Просит Gemini понять, трата это, доход или вопрос. Возвращает dict или None."""
    categories = ", ".join(EXPENSE_CATEGORY_HINTS)
    prompt = (
        "Ты — парсер сообщений финансового бота. Определи тип сообщения пользователя "
        "и верни ТОЛЬКО JSON без пояснений и без markdown-обрамления, в формате:\n"
        '{"type": "expense" | "income" | "question", "label": "строка или null", "amount": число или null}\n\n'
        f"Категории расходов на выбор для label, если type=expense: {categories}.\n"
        "Если type=income, label — источник поступления в свободной форме (Зарплата, Кешбек, Подарок и т.п.).\n"
        "Если сообщение не похоже на трату или доход (вопрос, просьба совета, что угодно ещё) — "
        'верни {"type": "question", "label": null, "amount": null}.\n\n'
        f'Сообщение пользователя: "{text}"'
    )
    raw = await asyncio.to_thread(call_ai, prompt)
    if not raw:
        return None
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return None


async def answer_finance_question(user_id: int, question: str) -> str:
    snapshot = get_financial_snapshot_text(user_id)
    prompt = (
        "Ты — дружелюбный личный финансовый помощник в Telegram-боте. Ты не можешь сам менять "
        "настройки бота или объединять категории — если пользователь просит что-то подобное "
        "(например, «занеси сигареты в категорию вредные привычки»), объясни, что для этого есть "
        "команда /alias СтараяКатегория НоваяКатегория, и покажи короткий пример под его сообщение.\n\n"
        "Вот текущие данные пользователя:\n\n"
        f"{snapshot}\n\n"
        f"Сообщение пользователя: {question}\n\n"
        "Ответь кратко (2-5 предложений), по делу, на русском, без markdown-разметки со звёздочками."
    )
    answer = await asyncio.to_thread(call_ai, prompt)
    if not answer:
        return (
            "Не получилось получить ответ от ИИ — проверь, задан ли GIGACHAT_AUTH_KEY. "
            "Пока могу показать /status или /report."
        )
    return "🤖 " + answer


async def get_report_ai_comment(report_text: str):
    prompt = (
        "Вот финансовая сводка пользователя за период:\n\n"
        f"{report_text}\n\n"
        "Напиши короткий (2-3 предложения) дружелюбный комментарий с одним конкретным советом, "
        "на русском, без markdown-звёздочек."
    )
    return await asyncio.to_thread(call_ai, prompt)


# ---------- Основная логика (переиспользуется командами и кнопками) ----------


def resolve_category(user_id: int, category: str) -> str:
    """Если для категории задан алиас (/alias), возвращает каноническое имя."""
    conn = get_db()
    row = conn.execute(
        "SELECT canonical FROM category_aliases WHERE user_id = ? AND alias = ?",
        (user_id, category.strip().lower()),
    ).fetchone()
    conn.close()
    return row["canonical"] if row else category


def log_expense(user_id: int, category: str, amount: float):
    category = resolve_category(user_id, category)
    conn = get_db()
    conn.execute(
        "INSERT INTO expenses (user_id, category, amount, created_at, month_key) VALUES (?, ?, ?, ?, ?)",
        (user_id, category, amount, datetime.now().isoformat(), current_month_key()),
    )
    conn.commit()
    limit_row = conn.execute(
        "SELECT limit_amount FROM budgets WHERE user_id = ? AND category = ?",
        (user_id, category),
    ).fetchone()
    spent_row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS spent FROM expenses WHERE user_id = ? AND category = ? AND month_key = ?",
        (user_id, category, current_month_key()),
    ).fetchone()
    conn.close()
    spent = spent_row["spent"]
    limit = limit_row["limit_amount"] if limit_row else None
    return category, spent, limit


def log_income(user_id: int, source: str, amount: float):
    conn = get_db()
    conn.execute(
        "INSERT INTO incomes (user_id, source, amount, created_at, month_key) VALUES (?, ?, ?, ?, ?)",
        (user_id, source, amount, datetime.now().isoformat(), current_month_key()),
    )
    conn.commit()
    total_row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM incomes WHERE user_id = ? AND month_key = ?",
        (user_id, current_month_key()),
    ).fetchone()
    conn.close()
    return total_row["total"]


def adjust_savings(user_id: int, amount: float):
    conn = get_db()
    conn.execute(
        "UPDATE savings SET total = total + ? WHERE user_id = ?", (amount, user_id)
    )
    conn.commit()
    total = conn.execute(
        "SELECT total FROM savings WHERE user_id = ?", (user_id,)
    ).fetchone()["total"]
    conn.close()
    return total


def pay_debt(user_id: int, amount: float, name: str = None):
    conn = get_db()
    if name:
        row = conn.execute(
            "SELECT id, remaining_amount FROM debts WHERE user_id = ? AND name = ?",
            (user_id, name),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, remaining_amount FROM debts WHERE user_id = ? AND remaining_amount > 0 ORDER BY id LIMIT 1",
            (user_id,),
        ).fetchone()
    if row is None:
        conn.close()
        return None
    new_remaining = max(0.0, row["remaining_amount"] - amount)
    conn.execute(
        "UPDATE debts SET remaining_amount = ? WHERE id = ?", (new_remaining, row["id"])
    )
    conn.commit()
    conn.close()
    return new_remaining


def get_period_range(period: str):
    now = datetime.now()
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = "сегодня"
    elif period == "week":
        start = now - timedelta(days=7)
        label = "последние 7 дней"
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        label = f"{now.strftime('%Y-%m')}"
    return start.isoformat(), now.isoformat(), label


# ---------------------------- Хендлеры команд ----------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    ai_line = (
        "\n\nМожешь просто писать мне текстом, например «кофе 500» или «получила зарплату 80000» — "
        "я сам пойму, что это, и запишу. Можно и вопросы про финансы задавать прямо так."
        if GIGACHAT_AUTH_KEY
        else ""
    )
    text = (
        "Привет! Я слежу за твоим бюджетом, доходами, накоплениями и долгом.\n\n"
        "Снизу — кнопки для быстрых действий. Команды — через /help." + ai_line
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Расходы:\n"
        "/add Категория Сумма — записать трату\n"
        "/mybudget — лимиты по категориям\n"
        "/setbudget Категория Сумма — изменить лимит\n"
        "/alias Старая Новая — считать одну категорию как другую (например: /alias Сигареты Вредные привычки)\n\n"
        "Доходы:\n"
        "/income Источник Сумма — записать поступление\n\n"
        "Накопления:\n"
        "/savings Сумма — отложить (минус — если сняла)\n"
        "/setsavings Сумма — задать цель на месяц\n\n"
        "Долг:\n"
        "/debt — текущие долги\n"
        "/paydebt Сумма [Название] — записать платёж\n\n"
        "Сводки:\n"
        "/status — всё сразу: категории, накопления, долг\n"
        "/report day|week|month — сводка за период (с AI-комментарием, если подключён Gemini)\n"
        "/reset — обнулить траты/доходы текущего месяца\n\n"
        + (
            "AI: просто пиши текстом без команд — пойму трату/доход или отвечу на вопрос про финансы."
            if GIGACHAT_AUTH_KEY
            else "AI выключен (нет GIGACHAT_AUTH_KEY) — см. README, как включить бесплатно."
        )
    )
    await update.message.reply_text(text)


async def mybudget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    conn = get_db()
    rows = conn.execute(
        "SELECT category, limit_amount FROM budgets WHERE user_id = ? ORDER BY limit_amount DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    total = sum(r["limit_amount"] for r in rows)
    lines = [f"{r['category']}: {r['limit_amount']:.0f} ₽" for r in rows]
    text = "Твой бюджет на месяц:\n" + "\n".join(lines) + f"\n\nИтого: {total:.0f} ₽"
    await update.message.reply_text(text)


async def setbudget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Формат: /setbudget Категория Сумма\nНапример: /setbudget Продукты 13000"
        )
        return
    amount = parse_amount(args[-1])
    if amount is None:
        await update.message.reply_text("Сумма должна быть числом.")
        return
    category = " ".join(args[:-1]).strip().capitalize()
    conn = get_db()
    conn.execute(
        """INSERT INTO budgets (user_id, category, limit_amount) VALUES (?, ?, ?)
           ON CONFLICT(user_id, category) DO UPDATE SET limit_amount = excluded.limit_amount""",
        (user_id, category, amount),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Лимит для «{category}» установлен: {amount:.0f} ₽")


async def alias_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if not args:
        await update.message.reply_text(
            "Формат: /alias СтараяКатегория НоваяКатегория\n"
            "Например: /alias Сигареты Вредные привычки\n\n"
            "После этого все траты (старые и новые) с категорией «Сигареты» "
            "будут учитываться как «Вредные привычки»."
        )
        return

    text = " ".join(args)
    if "->" in text:
        alias_part, canonical_part = text.split("->", 1)
    else:
        # Ищем среди уже существующих категорий бюджета ту, на которую заканчивается
        # введённый текст — так распознаются и многословные названия («Вредные привычки»).
        conn = get_db()
        existing = [
            r["category"]
            for r in conn.execute(
                "SELECT category FROM budgets WHERE user_id = ?", (user_id,)
            ).fetchall()
        ]
        conn.close()
        match = None
        text_lower = text.lower()
        for cat in sorted(existing, key=len, reverse=True):
            if text_lower.endswith(cat.lower()) and len(text_lower) > len(cat):
                match = cat
                break
        if match:
            alias_part = text[: -len(match)].strip()
            canonical_part = match
        elif len(args) == 2:
            alias_part, canonical_part = args[0], args[1]
        else:
            await update.message.reply_text(
                "Не понял, где заканчивается старая категория и начинается новая.\n"
                "Используй так: /alias Старая категория -> Новая категория"
            )
            return

    alias = alias_part.strip().lower()
    canonical = canonical_part.strip().capitalize()
    if not alias or not canonical:
        await update.message.reply_text("Обе категории должны быть непустыми.")
        return

    conn = get_db()
    conn.execute(
        """INSERT INTO category_aliases (user_id, alias, canonical) VALUES (?, ?, ?)
           ON CONFLICT(user_id, alias) DO UPDATE SET canonical = excluded.canonical""",
        (user_id, alias, canonical),
    )
    # Переносим уже существующие траты под старым названием в каноническую категорию
    updated = conn.execute(
        "UPDATE expenses SET category = ? WHERE user_id = ? AND LOWER(category) = ?",
        (canonical, user_id, alias),
    ).rowcount
    conn.commit()
    conn.close()

    extra = f" Уже занесённые траты ({updated} шт.) тоже перенесены." if updated else ""
    await update.message.reply_text(
        f"Готово: «{alias_part.strip().capitalize()}» теперь считается как «{canonical}».{extra}",
        reply_markup=MAIN_KEYBOARD,
    )


async def add_expense_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Формат: /add Категория Сумма\nНапример: /add Продукты 650"
        )
        return
    amount = parse_amount(args[-1])
    if amount is None:
        await update.message.reply_text("Сумма должна быть числом.")
        return
    category = " ".join(args[:-1]).strip().capitalize()
    await reply_expense_logged(update, user_id, category, amount)


async def reply_expense_logged(update: Update, user_id: int, category: str, amount: float):
    resolved_category, spent, limit = log_expense(user_id, category, amount)
    alias_note = f" (записано как «{resolved_category}»)" if resolved_category != category else ""
    if limit is None:
        await update.message.reply_text(
            f"Записал: {resolved_category} — {amount:.0f} ₽{alias_note} (лимита для этой категории нет, "
            f"добавь через /setbudget {resolved_category} Сумма)",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    remaining = limit - spent
    warn = ""
    if remaining < 0:
        warn = "\n⚠️ Лимит по категории превышен."
    elif remaining < limit * 0.15:
        warn = "\n⚠️ Остаток по категории меньше 15%."
    await update.message.reply_text(
        f"Записал: {resolved_category} — {amount:.0f} ₽{alias_note}\n"
        f"Потрачено в этом месяце: {spent:.0f} из {limit:.0f} ₽\n"
        f"Остаток: {remaining:.0f} ₽{warn}",
        reply_markup=MAIN_KEYBOARD,
    )


async def income_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Формат: /income Источник Сумма\nНапример: /income Зарплата 80000"
        )
        return
    amount = parse_amount(args[-1])
    if amount is None:
        await update.message.reply_text("Сумма должна быть числом.")
        return
    source = " ".join(args[:-1]).strip().capitalize()
    await reply_income_logged(update, user_id, source, amount)


async def reply_income_logged(update: Update, user_id: int, source: str, amount: float):
    total = log_income(user_id, source, amount)
    await update.message.reply_text(
        f"Записал доход: {source} — {amount:.0f} ₽\n"
        f"Доходов в этом месяце: {total:.0f} ₽",
        reply_markup=MAIN_KEYBOARD,
    )


async def reply_bulk_logged(update: Update, user_id: int, mode: str, entries: list):
    """Записывает сразу несколько трат/доходов (по одному на строку) и шлёт одну сводку."""
    lines = []
    total = 0.0
    for label, amount in entries:
        total += amount
        if mode == "expense":
            resolved_category, spent, limit = log_expense(user_id, label, amount)
            note = ""
            if limit is not None:
                remaining = limit - spent
                if remaining < 0:
                    note = " ⚠️ лимит превышен"
                elif remaining < limit * 0.15:
                    note = " ⚠️ остаток <15%"
            display = resolved_category if resolved_category != label else label
            lines.append(f"• {display}: {amount:.0f} ₽{note}")
        else:
            log_income(user_id, label, amount)
            lines.append(f"• {label}: {amount:.0f} ₽")

    verb = "Записал расходы" if mode == "expense" else "Записал доходы"
    tail = "\n\nПодробности по категориям — /status" if mode == "expense" else ""
    await update.message.reply_text(
        f"{verb} ({len(entries)} шт.):\n" + "\n".join(lines) + f"\n\nИтого: {total:.0f} ₽" + tail,
        reply_markup=MAIN_KEYBOARD,
    )


async def savings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "Формат: /savings Сумма\nПлюс — если отложила, минус — если сняла."
        )
        return
    amount = parse_amount(args[0])
    if amount is None:
        await update.message.reply_text("Сумма должна быть числом.")
        return
    await reply_savings_logged(update, user_id, amount)


async def reply_savings_logged(update: Update, user_id: int, amount: float):
    total = adjust_savings(user_id, amount)
    action = "Отложено" if amount >= 0 else "Списано"
    await update.message.reply_text(
        f"{action}: {abs(amount):.0f} ₽\nВсего накоплено: {total:.0f} ₽",
        reply_markup=MAIN_KEYBOARD,
    )


async def setsavings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Формат: /setsavings Сумма (цель на месяц)")
        return
    amount = parse_amount(args[0])
    if amount is None:
        await update.message.reply_text("Сумма должна быть числом.")
        return
    conn = get_db()
    conn.execute(
        "UPDATE savings SET monthly_target = ? WHERE user_id = ?", (amount, user_id)
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"Цель по накоплениям на месяц: {amount:.0f} ₽")


async def debt_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    conn = get_db()
    rows = conn.execute(
        "SELECT name, total_amount, remaining_amount FROM debts WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Долгов не записано.", reply_markup=MAIN_KEYBOARD)
        return
    lines = []
    for r in rows:
        paid = r["total_amount"] - r["remaining_amount"]
        pct = (paid / r["total_amount"] * 100) if r["total_amount"] else 0
        status_icon = "✅" if r["remaining_amount"] <= 0 else "🔴"
        lines.append(
            f"{status_icon} {r['name']}: осталось {r['remaining_amount']:.0f} из {r['total_amount']:.0f} ₽ (погашено {pct:.0f}%)"
        )
    text = "Твои долги:\n\n" + "\n".join(lines) + "\n\nЗаписать платёж: /paydebt Сумма"
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def paydebt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            "Формат: /paydebt Сумма [Название]\nНапример: /paydebt 20000 или /paydebt 20000 Кредитка Т-Банк"
        )
        return
    amount = parse_amount(args[0])
    if amount is None:
        await update.message.reply_text("Сумма должна быть числом.")
        return
    name = " ".join(args[1:]).strip() or None
    remaining = pay_debt(user_id, amount, name)
    if remaining is None:
        await update.message.reply_text("Такой долг не найден. Проверь /debt.")
        return
    done = "\n🎉 Долг полностью закрыт, поздравляю!" if remaining <= 0 else ""
    await update.message.reply_text(
        f"Платёж записан: {amount:.0f} ₽\nОсталось: {remaining:.0f} ₽{done}",
        reply_markup=MAIN_KEYBOARD,
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    conn = get_db()
    budgets = conn.execute(
        "SELECT category, limit_amount FROM budgets WHERE user_id = ?", (user_id,)
    ).fetchall()
    month = current_month_key()

    lines = []
    total_limit = 0.0
    total_spent = 0.0
    for b in budgets:
        spent_row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS spent FROM expenses WHERE user_id = ? AND category = ? AND month_key = ?",
            (user_id, b["category"], month),
        ).fetchone()
        spent = spent_row["spent"]
        remaining = b["limit_amount"] - spent
        total_limit += b["limit_amount"]
        total_spent += spent
        mark = "🔴" if remaining < 0 else ("🟡" if remaining < b["limit_amount"] * 0.15 else "🟢")
        lines.append(f"{mark} {b['category']}: {spent:.0f} / {b['limit_amount']:.0f} ₽")

    income_row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM incomes WHERE user_id = ? AND month_key = ?",
        (user_id, month),
    ).fetchone()
    savings_row = conn.execute(
        "SELECT total, monthly_target FROM savings WHERE user_id = ?", (user_id,)
    ).fetchone()
    debt_rows = conn.execute(
        "SELECT name, remaining_amount FROM debts WHERE user_id = ? AND remaining_amount > 0",
        (user_id,),
    ).fetchall()
    conn.close()

    debt_line = (
        "\n".join(f"  {d['name']}: {d['remaining_amount']:.0f} ₽" for d in debt_rows)
        if debt_rows
        else "  нет активных долгов 🎉"
    )

    text = (
        "Статус на сегодня:\n\n"
        + "\n".join(lines)
        + f"\n\nВсего потрачено: {total_spent:.0f} / {total_limit:.0f} ₽"
        + f"\nДоходов в этом месяце: {income_row['total']:.0f} ₽"
        + f"\n\nНакоплено: {savings_row['total']:.0f} ₽ (цель на месяц: {savings_row['monthly_target']:.0f} ₽)"
        + f"\n\nДолг:\n{debt_line}"
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def send_report(update: Update, context: ContextTypes.DEFAULT_TYPE, period: str):
    user_id = update.effective_user.id
    ensure_user(user_id)
    start_iso, end_iso, label = get_period_range(period)
    conn = get_db()
    expense_rows = conn.execute(
        """SELECT category, SUM(amount) AS total, COUNT(*) AS cnt FROM expenses
           WHERE user_id = ? AND created_at BETWEEN ? AND ? GROUP BY category ORDER BY total DESC""",
        (user_id, start_iso, end_iso),
    ).fetchall()
    income_rows = conn.execute(
        """SELECT source, SUM(amount) AS total FROM incomes
           WHERE user_id = ? AND created_at BETWEEN ? AND ? GROUP BY source ORDER BY total DESC""",
        (user_id, start_iso, end_iso),
    ).fetchall()
    savings_row = conn.execute(
        "SELECT total FROM savings WHERE user_id = ?", (user_id,)
    ).fetchone()
    debt_rows = conn.execute(
        "SELECT name, remaining_amount FROM debts WHERE user_id = ? AND remaining_amount > 0",
        (user_id,),
    ).fetchall()
    conn.close()

    total_expenses = sum(r["total"] for r in expense_rows) if expense_rows else 0
    total_income = sum(r["total"] for r in income_rows) if income_rows else 0

    lines = [f"📋 Сводка за {label}", ""]
    lines.append("Доходы:")
    if income_rows:
        lines += [f"- {r['source']}: {r['total']:.0f} ₽" for r in income_rows]
    else:
        lines.append("- нет записей")
    lines.append(f"Итого доходов: {total_income:.0f} ₽")
    lines.append("")
    lines.append("Расходы:")
    if expense_rows:
        lines += [f"- {r['category']}: {r['total']:.0f} ₽ ({r['cnt']} записей)" for r in expense_rows]
    else:
        lines.append("- нет записей")
    lines.append(f"Итого расходов: {total_expenses:.0f} ₽")
    lines.append("")
    lines.append(f"Баланс за период: {total_income - total_expenses:.0f} ₽")
    lines.append(f"Накоплено всего: {savings_row['total']:.0f} ₽")
    if debt_rows:
        lines.append("Остаток долга: " + ", ".join(f"{d['name']} — {d['remaining_amount']:.0f} ₽" for d in debt_rows))
    else:
        lines.append("Долгов нет 🎉")
    lines.append("")
    lines.append("(этот текст можно скопировать и отправить для разбора)")

    report_text = "\n".join(lines)
    comment = await get_report_ai_comment(report_text)
    if comment:
        report_text += f"\n\n🤖 {comment}"

    await update.message.reply_text(report_text, reply_markup=MAIN_KEYBOARD)


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    period = args[0].lower() if args else "month"
    period_map = {"day": "day", "week": "week", "month": "month", "д": "day", "н": "week", "м": "month"}
    period = period_map.get(period, "month")
    await send_report(update, context, period)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    conn.execute(
        "DELETE FROM expenses WHERE user_id = ? AND month_key = ?",
        (user_id, current_month_key()),
    )
    conn.execute(
        "DELETE FROM incomes WHERE user_id = ? AND month_key = ?",
        (user_id, current_month_key()),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        "Траты и доходы текущего месяца обнулены. Бюджет, накопления и долг сохранены.",
        reply_markup=MAIN_KEYBOARD,
    )


# ---------------------------- Кнопки / свободный текст ----------------------------


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    text = update.message.text.strip()

    if text == "💸 Расход":
        context.user_data["mode"] = "expense"
        await update.message.reply_text(
            "Напиши категорию и сумму, например: Продукты 650\n"
            "Можно сразу списком, по одной трате на строке:\n"
            "Продукты 650\nТранспорт 73*2=146\nПодписки 449+249=698"
        )
        return
    if text == "💰 Доход":
        context.user_data["mode"] = "income"
        await update.message.reply_text(
            "Напиши источник и сумму, например: Зарплата 80000\n"
            "Можно списком, по одному поступлению на строке."
        )
        return
    if text == "🏦 Накопления":
        context.user_data["mode"] = "savings"
        await update.message.reply_text(
            "Напиши сумму: положительную, если отложила, отрицательную — если сняла. Например: 5000 или -2000"
        )
        return
    if text == "📊 Статус":
        await status(update, context)
        return
    if text == "💳 Долг":
        await debt_status(update, context)
        return
    if text == "📋 Сводка":
        context.user_data["awaiting_report_period"] = True
        await update.message.reply_text("За какой период?", reply_markup=REPORT_PERIOD_KEYBOARD)
        return

    if context.user_data.get("awaiting_report_period"):
        context.user_data["awaiting_report_period"] = False
        period_map = {"День": "day", "Неделя": "week", "Месяц": "month"}
        if text == "Отмена":
            await update.message.reply_text("Ок, отменил.", reply_markup=MAIN_KEYBOARD)
            return
        if text in period_map:
            await send_report(update, context, period_map[text])
            return
        await update.message.reply_text("Не понял период, используй кнопки.", reply_markup=MAIN_KEYBOARD)
        return

    mode = context.user_data.get("mode")
    if mode in ("expense", "income"):
        entries = parse_bulk_lines(text)
        if entries:
            context.user_data["mode"] = None
            await reply_bulk_logged(update, user_id, mode, entries)
            return

    if mode == "savings":
        amount = parse_amount(text)
        if amount is not None:
            context.user_data["mode"] = None
            await reply_savings_logged(update, user_id, amount)
            return

    # Никакой активной кнопки/режима нет — пробуем понять сообщение через AI
    # (свободный ввод трат/доходов и вопросы про финансы).
    if mode is None and GIGACHAT_AUTH_KEY:
        parsed = await classify_free_text(text)
        if parsed:
            msg_type = parsed.get("type")
            label = (parsed.get("label") or "Другое").strip().capitalize()
            amount = parsed.get("amount")
            if msg_type == "expense" and amount:
                await update.message.reply_text(f"🤖 Похоже, это трата. Записываю как «{label}».")
                await reply_expense_logged(update, user_id, label, float(amount))
                return
            if msg_type == "income" and amount:
                await update.message.reply_text(f"🤖 Похоже, это доход. Записываю как «{label}».")
                await reply_income_logged(update, user_id, label, float(amount))
                return
        # Либо явный вопрос, либо не удалось чётко распознать трату/доход (например,
        # сообщение — это просьба или инструкция, а не сумма) — пробуем ответить как на вопрос,
        # чтобы не отвечать голым "не понял" на любое сообщение, которое AI не смог разложить по полочкам.
        answer = await answer_finance_question(user_id, text)
        await update.message.reply_text(answer, reply_markup=MAIN_KEYBOARD)
        return

    await update.message.reply_text(
        "Не понял. Используй кнопки внизу или посмотри /help.", reply_markup=MAIN_KEYBOARD
    )


# ---------- HTTP-эндпоинт для iOS Shortcuts / Back Tap ----------


def send_telegram_message_sync(chat_id, text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Ошибка отправки подтверждения в Telegram: {e}")


class ShortcutRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 — глушим стандартный лог http.server
        logger.info("shortcut endpoint: " + (format % args))

    def _respond(self, status: int, body: str):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/shortcut":
            self._respond(404, "not found")
            return

        params = parse_qs(parsed.query)
        key = (params.get("key") or [""])[0]
        text = (params.get("text") or [""])[0]
        entry_type = (params.get("type") or ["expense"])[0]

        if not SHORTCUT_SECRET or not OWNER_USER_ID:
            self._respond(500, "SHORTCUT_SECRET или OWNER_USER_ID не заданы на сервере")
            return
        if key != SHORTCUT_SECRET:
            self._respond(403, "неверный ключ")
            return
        if not text:
            self._respond(400, "пустой text")
            return

        try:
            user_id = int(OWNER_USER_ID)
        except ValueError:
            self._respond(500, "OWNER_USER_ID должен быть числом")
            return

        ensure_user(user_id)
        entries = parse_bulk_lines(text)
        if not entries:
            self._respond(400, f"не удалось распознать: {text}")
            return

        confirm_lines = []
        total = 0.0
        for label, amount in entries:
            total += amount
            if entry_type == "income":
                log_income(user_id, label, amount)
            else:
                resolved, spent, limit = log_expense(user_id, label, amount)
                label = resolved
            confirm_lines.append(f"• {label}: {amount:.0f} ₽")

        verb = "📲 Записано с Back Tap — доходы" if entry_type == "income" else "📲 Записано с Back Tap — расходы"
        confirm_text = f"{verb}:\n" + "\n".join(confirm_lines) + f"\n\nИтого: {total:.0f} ₽"
        send_telegram_message_sync(user_id, confirm_text)

        self._respond(200, "OK: " + "; ".join(confirm_lines))


def start_shortcut_server():
    server = ThreadingHTTPServer(("0.0.0.0", SHORTCUT_PORT), ShortcutRequestHandler)
    logger.info(f"Shortcut HTTP-сервер слушает порт {SHORTCUT_PORT}")
    server.serve_forever()


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не найден TELEGRAM_BOT_TOKEN. Установи переменную окружения перед запуском."
        )

    init_db()

    threading.Thread(target=start_shortcut_server, daemon=True).start()

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("mybudget", mybudget))
    application.add_handler(CommandHandler("setbudget", setbudget))
    application.add_handler(CommandHandler("alias", alias_cmd))
    application.add_handler(CommandHandler("add", add_expense_cmd))
    application.add_handler(CommandHandler("income", income_cmd))
    application.add_handler(CommandHandler("savings", savings_cmd))
    application.add_handler(CommandHandler("setsavings", setsavings_cmd))
    application.add_handler(CommandHandler("debt", debt_status))
    application.add_handler(CommandHandler("paydebt", paydebt_cmd))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("report", report_cmd))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен")
    application.run_polling()


if __name__ == "__main__":
    main()
