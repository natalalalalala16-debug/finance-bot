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

Данные хранятся в SQLite (finance_bot.db) рядом со скриптом.
ВАЖНО: на Railway без подключённого Volume файл базы стирается при каждом передеплое —
см. README.md, раздел "Постоянное хранилище".
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta

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


# ---------- Основная логика (переиспользуется командами и кнопками) ----------


def log_expense(user_id: int, category: str, amount: float):
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
    return spent, limit


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
    text = (
        "Привет! Я слежу за твоим бюджетом, доходами, накоплениями и долгом.\n\n"
        "Снизу — кнопки для быстрых действий. Команды — через /help."
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Расходы:\n"
        "/add Категория Сумма — записать трату\n"
        "/mybudget — лимиты по категориям\n"
        "/setbudget Категория Сумма — изменить лимит\n\n"
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
        "/report day|week|month — сводка за период\n"
        "/reset — обнулить траты/доходы текущего месяца"
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
    spent, limit = log_expense(user_id, category, amount)
    if limit is None:
        await update.message.reply_text(
            f"Записал: {category} — {amount:.0f} ₽ (лимита для этой категории нет, "
            f"добавь через /setbudget {category} Сумма)",
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
        f"Записал: {category} — {amount:.0f} ₽\n"
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

    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)


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
        await update.message.reply_text("Напиши категорию и сумму, например: Продукты 650")
        return
    if text == "💰 Доход":
        context.user_data["mode"] = "income"
        await update.message.reply_text("Напиши источник и сумму, например: Зарплата 80000")
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
    parts = text.rsplit(" ", 1)
    if mode and len(parts) == 2:
        label, amount_text = parts
        amount = parse_amount(amount_text)
        if amount is not None:
            label = label.strip().capitalize()
            context.user_data["mode"] = None
            if mode == "expense":
                await reply_expense_logged(update, user_id, label, amount)
            elif mode == "income":
                await reply_income_logged(update, user_id, label, amount)
            return

    if mode == "savings":
        amount = parse_amount(text)
        if amount is not None:
            context.user_data["mode"] = None
            await reply_savings_logged(update, user_id, amount)
            return

    await update.message.reply_text(
        "Не понял. Используй кнопки внизу или посмотри /help.", reply_markup=MAIN_KEYBOARD
    )


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Не найден TELEGRAM_BOT_TOKEN. Установи переменную окружения перед запуском."
        )

    init_db()

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("mybudget", mybudget))
    application.add_handler(CommandHandler("setbudget", setbudget))
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
