#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SF Waiter AI Trainer Bot
Telegram bot for waiter training: menu study, tests, AI sales simulations, reports.

Stack:
- python-telegram-bot v21+
- SQLite
- OpenAI API (optional, for AI mode)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Optional OpenAI client
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("sf_waiter_ai_bot")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bot.db"
MENU_PATH = BASE_DIR / "sample_menu.json"

REGISTER_NAME, REGISTER_RESTAURANT, REGISTER_ROLE = range(3)
AI_SCENARIO_REPLY = 100

MAIN_MENU = [
    ["📚 Изучить меню", "🧠 Пройти тест"],
    ["💬 Тренировка продаж", "📊 Мой прогресс"],
    ["🔥 Экзамен дня", "🛠 Админ"],
]


def get_env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def load_menu() -> Dict[str, Any]:
    with open(MENU_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            restaurant TEXT NOT NULL,
            role TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            action_type TEXT NOT NULL,
            category TEXT,
            item_name TEXT,
            score INTEGER DEFAULT 0,
            max_score INTEGER DEFAULT 0,
            feedback TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_exam (
            exam_date TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            question_json TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def upsert_employee(telegram_id: int, full_name: str, restaurant: str, role: str, is_admin: int = 0) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO employees (telegram_id, full_name, restaurant, role, is_admin, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            full_name=excluded.full_name,
            restaurant=excluded.restaurant,
            role=excluded.role
        """,
        (telegram_id, full_name, restaurant, role, is_admin, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_employee(telegram_id: int) -> Optional[sqlite3.Row]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM employees WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return row


def save_progress(
    telegram_id: int,
    action_type: str,
    category: Optional[str] = None,
    item_name: Optional[str] = None,
    score: int = 0,
    max_score: int = 0,
    feedback: Optional[str] = None,
) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO progress (telegram_id, action_type, category, item_name, score, max_score, feedback, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            telegram_id,
            action_type,
            category,
            item_name,
            score,
            max_score,
            feedback,
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def get_user_stats(telegram_id: int) -> Dict[str, Any]:
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            COUNT(*) as total_actions,
            COALESCE(SUM(score), 0) as total_score,
            COALESCE(SUM(max_score), 0) as total_max
        FROM progress
        WHERE telegram_id = ?
        """,
        (telegram_id,),
    )
    summary = cur.fetchone()

    cur.execute(
        """
        SELECT action_type, COUNT(*) as cnt
        FROM progress
        WHERE telegram_id = ?
        GROUP BY action_type
        ORDER BY cnt DESC
        """,
        (telegram_id,),
    )
    by_type = cur.fetchall()

    cur.execute(
        """
        SELECT category, AVG(CASE WHEN max_score > 0 THEN CAST(score AS FLOAT) / max_score ELSE NULL END) as avg_ratio
        FROM progress
        WHERE telegram_id = ? AND category IS NOT NULL
        GROUP BY category
        ORDER BY avg_ratio ASC
        """,
        (telegram_id,),
    )
    by_category = cur.fetchall()

    conn.close()
    return {
        "summary": dict(summary) if summary else {},
        "by_type": [dict(x) for x in by_type],
        "by_category": [dict(x) for x in by_category],
    }


def get_network_stats() -> List[sqlite3.Row]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT e.restaurant,
               COUNT(DISTINCT e.telegram_id) as employees,
               COUNT(p.id) as activities,
               COALESCE(AVG(CASE WHEN p.max_score > 0 THEN CAST(p.score AS FLOAT) / p.max_score END), 0) as avg_ratio
        FROM employees e
        LEFT JOIN progress p ON e.telegram_id = p.telegram_id
        GROUP BY e.restaurant
        ORDER BY avg_ratio DESC, activities DESC
        """
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def make_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)


def category_keyboard(menu_data: Dict[str, Any]) -> InlineKeyboardMarkup:
    buttons = []
    for category in menu_data["categories"]:
        buttons.append([InlineKeyboardButton(category["emoji"] + " " + category["name"], callback_data=f"cat:{category['slug']}")])
    return InlineKeyboardMarkup(buttons)


def item_keyboard(category: Dict[str, Any]) -> InlineKeyboardMarkup:
    buttons = []
    for item in category["items"]:
        buttons.append([InlineKeyboardButton(item["name"], callback_data=f"item:{category['slug']}:{item['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Назад к категориям", callback_data="back:categories")])
    return InlineKeyboardMarkup(buttons)


def item_card(item: Dict[str, Any], category_name: str) -> str:
    upsell = "\n".join([f"• {x}" for x in item.get("upsell", [])]) or "• —"
    features = "\n".join([f"• {x}" for x in item.get("features", [])]) or "• —"
    return (
        f"*{item['name']}*\n"
        f"Категория: {category_name}\n"
        f"Вес: {item.get('weight', '—')}\n"
        f"Цена: {item.get('price', '—')}\n\n"
        f"*Состав:*\n{item.get('composition', '—')}\n\n"
        f"*Как описать гостю:*\n{item.get('sell_pitch', '—')}\n\n"
        f"*Когда предлагать:*\n{features}\n\n"
        f"*Допродажа:*\n{upsell}"
    )


def generate_quiz_questions(menu_data: Dict[str, Any], count: int = 5) -> List[Dict[str, Any]]:
    questions: List[Dict[str, Any]] = []
    all_items = []
    for cat in menu_data["categories"]:
        for item in cat["items"]:
            all_items.append((cat, item))

    random.shuffle(all_items)
    selected = all_items[:count]

    for cat, item in selected:
        wrong_items = [x[1]["name"] for x in all_items if x[1]["name"] != item["name"]]
        random.shuffle(wrong_items)
        options = [item["name"]] + wrong_items[:3]
        random.shuffle(options)

        q = {
            "category": cat["name"],
            "question": f"Какое блюдо подходит под описание: {item['quiz_hint']}",
            "options": options,
            "answer": item["name"],
            "item_name": item["name"],
        }
        questions.append(q)
    return questions


def get_category_by_slug(menu_data: Dict[str, Any], slug: str) -> Optional[Dict[str, Any]]:
    for cat in menu_data["categories"]:
        if cat["slug"] == slug:
            return cat
    return None


def get_item_by_id(category: Dict[str, Any], item_id: str) -> Optional[Dict[str, Any]]:
    for item in category["items"]:
        if item["id"] == item_id:
            return item
    return None


def get_today_exam(menu_data: Dict[str, Any]) -> Dict[str, Any]:
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM daily_exam WHERE exam_date = ?", (today,))
    row = cur.fetchone()

    if row:
        conn.close()
        return json.loads(row["question_json"])

    questions = generate_quiz_questions(menu_data, count=5)
    exam = {"date": today, "questions": questions}
    cur.execute(
        "INSERT OR REPLACE INTO daily_exam (exam_date, category, question_json) VALUES (?, ?, ?)",
        (today, "mixed", json.dumps(exam, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    return exam


def build_sales_scenarios(menu_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "id": "light_meal",
            "guest_message": "Добрый вечер. Хочу что-то лёгкое, но чтобы наесться. Что посоветуете?",
            "goal": "Уточнить потребность, предложить 1–2 позиции и сделать мягкую допродажу.",
            "good_points": [
                "есть уточняющий вопрос или аккуратное предположение",
                "предложена релевантная позиция, а не всё подряд",
                "есть вкусное описание блюда",
                "есть допродажа напитка/соуса/закуски",
            ],
        },
        {
            "id": "difference_meat",
            "guest_message": "В чем разница между курицей и говядиной в шаурме?",
            "goal": "Объяснить вкус и ощущение блюда простым языком, без сухого перечисления.",
            "good_points": [
                "объяснил разницу по вкусу",
                "говорил простым языком",
                "помог выбрать, а не просто перечислил факты",
            ],
        },
        {
            "id": "coffee_dessert",
            "guest_message": "Что можно взять к кофе?",
            "goal": "Предложить 2 уместных варианта и помочь быстро выбрать.",
            "good_points": [
                "предложил 2 варианта",
                "кратко объяснил отличие",
                "не перегрузил гостя",
            ],
        },
    ]


def evaluate_locally(answer: str, scenario: Dict[str, Any]) -> Tuple[int, str]:
    text = answer.lower()
    score = 0
    reasons = []

    keywords = {
        "light_meal": ["совет", "рекоменд", "пицц", "салат", "напит", "соус", "могу предложить"],
        "difference_meat": ["куриц", "говяд", "вкус", "нежн", "насыщ", "если хотите"],
        "coffee_dessert": ["десерт", "блин", "чизкейк", "два варианта", "кофе", "подойдет"],
    }

    scenario_words = keywords.get(scenario["id"], [])
    matched = sum(1 for w in scenario_words if w in text)
    score += min(10, matched * 2)

    if len(answer.split()) > 12:
        score += 3
        reasons.append("Ответ не слишком короткий — это хорошо.")
    else:
        reasons.append("Ответ коротковат. Стоит добавить уверенную презентацию блюда.")

    if any(x in text for x in ["ещё", "дополнительно", "напит", "соус", "картофель"]):
        score += 3
        reasons.append("Есть попытка допродажи.")
    else:
        reasons.append("Нет допродажи — упущен шанс увеличить чек.")

    if "?" in answer:
        score += 2
        reasons.append("Есть попытка уточнить потребность гостя.")
    else:
        reasons.append("Не хватает уточняющего вопроса.")

    final_score = max(1, min(10, score))
    feedback = f"Оценка: *{final_score}/10*\n" + "\n".join([f"• {r}" for r in reasons])
    return final_score, feedback


def build_system_prompt(menu_data: Dict[str, Any], scenario: Dict[str, Any]) -> str:
    compact_menu = []
    for cat in menu_data["categories"]:
        for item in cat["items"]:
            compact_menu.append(
                {
                    "category": cat["name"],
                    "name": item["name"],
                    "sell_pitch": item.get("sell_pitch"),
                    "upsell": item.get("upsell", []),
                    "features": item.get("features", []),
                }
            )

    return f"""
Ты — строгий, но полезный тренер официантов сети SF Shaurma Food.
Твоя задача — оценить ответ официанта в ситуации общения с гостем.

Правила:
1. Оценивай по 10-балльной шкале.
2. Смотри на понимание запроса гостя, релевантность рекомендации, вкусную презентацию и наличие допродажи.
3. Пиши только по делу.
4. Ответ должен быть в формате:
Оценка: X/10
Что хорошо:
- ...
Что упущено:
- ...
Как ответить сильнее:
- ...

Сценарий:
Гость: {scenario['guest_message']}
Цель: {scenario['goal']}

Доступное меню:
{json.dumps(compact_menu, ensure_ascii=False)}
    """.strip()


def get_openai_client() -> Optional[Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return None
    return OpenAI(api_key=api_key)


async def ai_evaluate(menu_data: Dict[str, Any], scenario: Dict[str, Any], answer: str) -> Tuple[int, str]:
    client = get_openai_client()
    if client is None:
        return evaluate_locally(answer, scenario)

    try:
        prompt = build_system_prompt(menu_data, scenario)
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": answer},
            ],
            temperature=0.3,
        )
        content = response.choices[0].message.content.strip()
        score = 7
        for token in content.replace("*", "").split():
            if "/10" in token:
                try:
                    score = int(token.split("/")[0].split(":")[-1])
                    break
                except Exception:
                    pass
        return score, content
    except Exception as e:
        logger.exception("OpenAI evaluation error: %s", e)
        return evaluate_locally(answer, scenario)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    employee = get_employee(user.id)
    if employee:
        await update.message.reply_text(
            f"Привет, {employee['full_name']}!\nДобро пожаловать в AI-тренажёр официантов SF.",
            reply_markup=make_main_keyboard(),
        )
        return ConversationHandler.END

    await update.message.reply_text("Привет! Давай зарегистрируем тебя.\nНапиши имя и фамилию.")
    return REGISTER_NAME


async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["full_name"] = update.message.text.strip()
    await update.message.reply_text("Укажи ресторан/точку, где работаешь.")
    return REGISTER_RESTAURANT


async def register_restaurant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["restaurant"] = update.message.text.strip()
    await update.message.reply_text("Укажи роль: официант / администратор / директор.")
    return REGISTER_ROLE


async def register_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    role = update.message.text.strip().lower()
    is_admin = 1 if role in {"администратор", "директор", "manager", "admin"} else 0

    upsert_employee(
        telegram_id=update.effective_user.id,
        full_name=context.user_data["full_name"],
        restaurant=context.user_data["restaurant"],
        role=role,
        is_admin=is_admin,
    )

    await update.message.reply_text(
        "Регистрация завершена. Можешь начинать обучение.",
        reply_markup=make_main_keyboard(),
    )
    return ConversationHandler.END


async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Регистрация отменена.", reply_markup=make_main_keyboard())
    return ConversationHandler.END


async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    menu_data = context.bot_data["menu_data"]
    employee = get_employee(update.effective_user.id)

    if not employee:
        await update.message.reply_text("Сначала нажми /start и пройди регистрацию.")
        return

    if text == "📚 Изучить меню":
        await update.message.reply_text(
            "Выбери категорию меню:",
            reply_markup=category_keyboard(menu_data),
        )
        return

    if text == "🧠 Пройти тест":
        questions = generate_quiz_questions(menu_data, count=5)
        context.user_data["quiz_questions"] = questions
        context.user_data["quiz_index"] = 0
        context.user_data["quiz_score"] = 0
        await send_next_quiz_question(update, context)
        return

    if text == "💬 Тренировка продаж":
        scenarios = build_sales_scenarios(menu_data)
        scenario = random.choice(scenarios)
        context.user_data["sales_scenario"] = scenario
        await update.message.reply_text(
            f"*Сценарий тренировки*\n\n"
            f"Гость: _{scenario['guest_message']}_\n\n"
            f"Твоя задача: {scenario['goal']}\n\n"
            f"Напиши свой ответ как официант.",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data["awaiting_ai_reply"] = True
        return

    if text == "📊 Мой прогресс":
        stats = get_user_stats(update.effective_user.id)
        summary = stats["summary"]
        percent = 0
        if summary.get("total_max", 0):
            percent = round(summary["total_score"] / summary["total_max"] * 100)

        weak_categories = []
        for row in stats["by_category"][:3]:
            if row["category"] and row["avg_ratio"] is not None:
                weak_categories.append(f"• {row['category']} — {round(row['avg_ratio'] * 100)}%")

        actions = "\n".join([f"• {x['action_type']}: {x['cnt']}" for x in stats["by_type"]]) or "• Пока нет действий"
        weak = "\n".join(weak_categories) or "• Пока недостаточно данных"

        await update.message.reply_text(
            f"*Твой прогресс*\n\n"
            f"Всего активностей: {summary.get('total_actions', 0)}\n"
            f"Средний результат: {percent}%\n\n"
            f"*По форматам:*\n{actions}\n\n"
            f"*Зоны роста:*\n{weak}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if text == "🔥 Экзамен дня":
        exam = get_today_exam(menu_data)
        context.user_data["quiz_questions"] = exam["questions"]
        context.user_data["quiz_index"] = 0
        context.user_data["quiz_score"] = 0
        context.user_data["quiz_mode"] = "daily_exam"
        await send_next_quiz_question(update, context)
        return

    if text == "🛠 Админ":
        if not employee["is_admin"]:
            await update.message.reply_text("Этот раздел доступен только администратору или директору.")
            return
        rows = get_network_stats()
        if not rows:
            await update.message.reply_text("Пока нет данных по сети.")
            return

        lines = ["*Сводка по ресторанам*"]
        for row in rows:
            lines.append(
                f"• {row['restaurant']}: сотрудников {row['employees']}, активностей {row['activities']}, средний результат {round(row['avg_ratio'] * 100)}%"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    await update.message.reply_text("Выбери действие из меню.", reply_markup=make_main_keyboard())


async def send_next_quiz_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    idx = context.user_data.get("quiz_index", 0)
    questions = context.user_data.get("quiz_questions", [])

    if idx >= len(questions):
        score = context.user_data.get("quiz_score", 0)
        max_score = len(questions)
        mode = context.user_data.get("quiz_mode", "quiz")
        title = "Экзамен дня" if mode == "daily_exam" else "Тест завершён"

        save_progress(
            telegram_id=update.effective_user.id,
            action_type=mode,
            category="mixed",
            score=score,
            max_score=max_score,
            feedback=f"Результат {score}/{max_score}",
        )

        await update.message.reply_text(
            f"{title}.\nРезультат: {score}/{max_score}",
            reply_markup=make_main_keyboard(),
        )
        context.user_data.pop("quiz_mode", None)
        return

    q = questions[idx]
    buttons = [[InlineKeyboardButton(opt, callback_data=f"quiz:{opt}")] for opt in q["options"]]
    buttons.append([InlineKeyboardButton("❌ Завершить тест", callback_data="quiz:end")])

    text = (
        f"*Вопрос {idx + 1}/{len(questions)}*\n"
        f"{q['question']}\n"
        f"Категория: {q['category']}"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        await update.message.reply_text(
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    menu_data = context.bot_data["menu_data"]
    data = query.data

    if data == "back:categories":
        await query.edit_message_text("Выбери категорию меню:", reply_markup=category_keyboard(menu_data))
        return

    _, slug = data.split(":", 1)
    category = get_category_by_slug(menu_data, slug)
    if not category:
        await query.edit_message_text("Категория не найдена.")
        return

    await query.edit_message_text(
        f"Категория: *{category['emoji']} {category['name']}*\nВыбери блюдо:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=item_keyboard(category),
    )


async def item_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    menu_data = context.bot_data["menu_data"]

    _, slug, item_id = query.data.split(":")
    category = get_category_by_slug(menu_data, slug)
    if not category:
        await query.edit_message_text("Категория не найдена.")
        return

    item = get_item_by_id(category, item_id)
    if not item:
        await query.edit_message_text("Блюдо не найдено.")
        return

    save_progress(
        telegram_id=update.effective_user.id,
        action_type="menu_study",
        category=category["name"],
        item_name=item["name"],
        score=1,
        max_score=1,
        feedback="Изучена карточка блюда",
    )

    buttons = [
        [InlineKeyboardButton("🧠 Проверить себя по этой категории", callback_data=f"quizcat:{slug}")],
        [InlineKeyboardButton("⬅️ Назад к блюдам", callback_data=f"cat:{slug}")],
    ]

    await query.edit_message_text(
        item_card(item, category["name"]),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def quiz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "quiz:end":
        context.user_data["quiz_index"] = len(context.user_data.get("quiz_questions", []))
        await send_next_quiz_question(update, context)
        return

    _, answer = query.data.split(":", 1)
    idx = context.user_data.get("quiz_index", 0)
    questions = context.user_data.get("quiz_questions", [])
    current = questions[idx]

    is_correct = answer == current["answer"]
    if is_correct:
        context.user_data["quiz_score"] = context.user_data.get("quiz_score", 0) + 1

    text = (
        f"{'✅ Верно' if is_correct else '❌ Неверно'}\n\n"
        f"Правильный ответ: *{current['answer']}*\n"
        f"Подсказка: {current['question']}"
    )

    context.user_data["quiz_index"] = idx + 1
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    await send_next_quiz_question(update, context)


async def quiz_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    menu_data = context.bot_data["menu_data"]

    _, slug = query.data.split(":", 1)
    category = get_category_by_slug(menu_data, slug)
    if not category:
        await query.edit_message_text("Категория не найдена.")
        return

    questions = []
    items = category["items"][:]
    random.shuffle(items)
    for item in items[:5]:
        wrong = [x["name"] for x in category["items"] if x["name"] != item["name"]]
        random.shuffle(wrong)
        options = [item["name"]] + wrong[:3]
        random.shuffle(options)
        questions.append(
            {
                "category": category["name"],
                "question": f"Какое блюдо подходит под описание: {item['quiz_hint']}",
                "options": options,
                "answer": item["name"],
                "item_name": item["name"],
            }
        )

    context.user_data["quiz_questions"] = questions
    context.user_data["quiz_index"] = 0
    context.user_data["quiz_score"] = 0
    context.user_data["quiz_mode"] = "category_quiz"

    await query.edit_message_text(f"Запускаю тест по категории *{category['name']}*...", parse_mode=ParseMode.MARKDOWN)
    await send_next_quiz_question(update, context)


async def handle_ai_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_ai_reply"):
        return

    answer = update.message.text.strip()
    scenario = context.user_data.get("sales_scenario")
    menu_data = context.bot_data["menu_data"]

    if not scenario:
        context.user_data["awaiting_ai_reply"] = False
        await update.message.reply_text("Сценарий не найден. Запусти тренировку заново.")
        return

    score, feedback = await ai_evaluate(menu_data, scenario, answer)

    save_progress(
        telegram_id=update.effective_user.id,
        action_type="sales_training",
        category="sales",
        item_name=scenario["id"],
        score=score,
        max_score=10,
        feedback=feedback,
    )

    await update.message.reply_text(
        f"*Разбор ответа*\n\n{feedback}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_main_keyboard(),
    )
    context.user_data["awaiting_ai_reply"] = False
    context.user_data.pop("sales_scenario", None)


def build_app() -> Application:
    token = get_env("TELEGRAM_BOT_TOKEN")
    app = Application.builder().token(token).build()

    app.bot_data["menu_data"] = load_menu()
    init_db()

    registration_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            REGISTER_RESTAURANT: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_restaurant)],
            REGISTER_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_role)],
        },
        fallbacks=[CommandHandler("cancel", cancel_registration)],
    )

    app.add_handler(registration_handler)
    app.add_handler(CallbackQueryHandler(category_callback, pattern=r"^cat:|^back:categories$"))
    app.add_handler(CallbackQueryHandler(item_callback, pattern=r"^item:"))
    app.add_handler(CallbackQueryHandler(quiz_callback, pattern=r"^quiz:"))
    app.add_handler(CallbackQueryHandler(quiz_category_callback, pattern=r"^quizcat:"))

    # AI replies go first if scenario is active
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ai_reply), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_main_menu), group=1)

    return app


def main() -> None:
    app = build_app()
    logger.info("Bot started")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
