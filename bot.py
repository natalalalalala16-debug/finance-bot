"""
Личный финансовый бот для Telegram.

Функции:
  /start            - начать работу, создать бюджет по умолчанию
  /mybudget         - показать текущий бюджет по категориям
  /setbudget К С    - установить лимит для категории (например: /setbudget Продукты 15000)
  /add К С          - записать трату (например: /add Продукты 850)
  /savings С        - добавить (или списать отрицательным числом) в накопления
  /status           - показать остатки по всем категориям и общий прогресс
  /report           - сводка за месяц
  /reset            - обнулить траты текущего месяца (бюджет и накопления сохраняются)
  /help             - список команд

Данные хранятся в SQLite (finance_bot.db), файл создаётся автоматически рядом со скриптом.
"""

import logging
import os
import sqlite3
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finance_bot.db")

# Бюджет по умолчанию — под старт можно сразу поправить командой /setbudget
DEFAULT_BUDGET = {
    "Кредит": 15000,
    "Жильё": 15000,
    "Продукты": 15000,
    "Транспорт": 4000,
    "Связь": 2000,
    "Личное": 7000,
    "Буфер": 5000,
}
DEFAULT_SAVINGS_GOAL_MONTHLY = 7000


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
        """CREATE TABLE IF NOT EXISTS savings (
            user_id INTEGER PRIMARY KEY,
            total REAL DEFAULT 0
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
        conn.execute(
            "INSERT OR IGNORE INTO savings (user_id, total) VALUES (?, 0)",
            (user_id,),
        )
        conn.commit()
    conn.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    text = (
        "Привет! Я буду следить за твоим бюджетом.\n\n"
        "Я уже настроил стартовый бюджет на основе твоих данных. "
        "Посмотреть его — /mybudget, изменить лимит — /setbudget.\n\n"
        "Основные команды:\n"
        "/add Категория Сумма — записать трату\n"
        "/status — остатки по категориям\n"
        "/savings Сумма — добавить в накопления\n"
        "/report — сводка за месяц\n"
        "/help — все команды"
    )
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Команды:\n"
        "/mybudget — текущий бюджет по категориям\n"
        "/setbudget Категория Сумма — установить лимит\n"
        "/add Категория Сумма — записать трату\n"
        "/savings Сумма — добавить в накопления (можно отрицательное число, если снял)\n"
        "/status — остатки по категориям и накопления\n"
        "/report — сводка за текущий месяц\n"
        "/reset — обнулить траты месяца (бюджет и накопления не трогает)"
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
            "Формат: /setbudget Категория Сумма\nНапример: /setbudget Продукты 18000"
        )
        return
    try:
        amount = float(args[-1].replace(",", "."))
    except ValueError:
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


async def add_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Формат: /add Категория Сумма\nНапример: /add Продукты 650"
        )
        return
    try:
        amount = float(args[-1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Сумма должна быть числом.")
        return
    category = " ".join(args[:-1]).strip().capitalize()
    conn = get_db()
    conn.execute(
        "INSERT INTO expenses (user_id, category, amount, created_at, month_key) VALUES (?, ?, ?, ?, ?)",
        (user_id, category, amount, datetime.now().isoformat(), current_month_key()),
    )
    conn.commit()

    row = conn.execute(
        "SELECT limit_amount FROM budgets WHERE user_id = ? AND category = ?",
        (user_id, category),
    ).fetchone()
    spent_row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS spent FROM expenses WHERE user_id = ? AND category = ? AND month_key = ?",
        (user_id, category, current_month_key()),
    ).fetchone()
    conn.close()

    spent = spent_row["spent"]
    if row is None:
        await update.message.reply_text(
            f"Записал: {category} — {amount:.0f} ₽ (эта категория без установленного лимита, "
            f"добавь его через /setbudget {category} Сумма)"
        )
        return

    limit = row["limit_amount"]
    remaining = limit - spent
    warn = ""
    if remaining < 0:
        warn = "\n⚠️ Лимит по категории превышен."
    elif remaining < limit * 0.15:
        warn = "\n⚠️ Остаток по категории меньше 15%."

    await update.message.reply_text(
        f"Записал: {category} — {amount:.0f} ₽\n"
        f"Потрачено в этом месяце: {spent:.0f} из {limit:.0f} ₽\n"
        f"Остаток: {remaining:.0f} ₽{warn}"
    )


async def savings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "Формат: /savings Сумма\nПлюс — если отложил(а), минус — если снял(а).\nНапример: /savings 7000 или /savings -2000"
        )
        return
    try:
        amount = float(args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Сумма должна быть числом.")
        return
    conn = get_db()
    conn.execute(
        "UPDATE savings SET total = total + ? WHERE user_id = ?", (amount, user_id)
    )
    conn.commit()
    total = conn.execute(
        "SELECT total FROM savings WHERE user_id = ?", (user_id,)
    ).fetchone()["total"]
    conn.close()
    action = "Отложено" if amount >= 0 else "Списано"
    await update.message.reply_text(
        f"{action}: {abs(amount):.0f} ₽\nВсего накоплено: {total:.0f} ₽"
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
        lines.append(f"{mark} {b['category']}: {spent:.0f} / {b['limit_amount']:.0f} ₽ (остаток {remaining:.0f})")

    savings_row = conn.execute(
        "SELECT total FROM savings WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()

    savings_total = savings_row["total"] if savings_row else 0
    text = (
        "Статус на сегодня:\n\n"
        + "\n".join(lines)
        + f"\n\nВсего потрачено: {total_spent:.0f} из {total_limit:.0f} ₽"
        + f"\nНакоплено: {savings_total:.0f} ₽"
    )
    await update.message.reply_text(text)


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    month = current_month_key()
    conn = get_db()
    rows = conn.execute(
        """SELECT category, SUM(amount) AS spent, COUNT(*) AS cnt
           FROM expenses WHERE user_id = ? AND month_key = ?
           GROUP BY category ORDER BY spent DESC""",
        (user_id, month),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("В этом месяце пока нет записанных трат.")
        return

    total = sum(r["spent"] for r in rows)
    lines = [f"{r['category']}: {r['spent']:.0f} ₽ ({r['cnt']} записей)" for r in rows]
    text = f"Сводка за {month}:\n\n" + "\n".join(lines) + f"\n\nВсего потрачено: {total:.0f} ₽"
    await update.message.reply_text(text)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    conn.execute(
        "DELETE FROM expenses WHERE user_id = ? AND month_key = ?",
        (user_id, current_month_key()),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text("Траты текущего месяца обнулены. Бюджет и накопления сохранены.")


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
    application.add_handler(CommandHandler("add", add_expense))
    application.add_handler(CommandHandler("savings", savings))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CommandHandler("reset", reset))

    logger.info("Бот запущен")
    application.run_polling()


if __name__ == "__main__":
    main()
