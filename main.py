#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
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

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("sf_waiter_ai_bot_static")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bot.db"
MENU_PATH = BASE_DIR / "sample_menu.json"

REGISTER_NAME, REGISTER_RESTAURANT, REGISTER_ROLE = range(3)

MAIN_MENU = [
    ["📚 Меню", "🎁 Акции и комбо"],
    ["🎓 Академия продаж", "🧠 Тест по меню"],
    ["💬 Тренировка продаж", "🔥 Экзамен дня"],
    ["📊 Мой прогресс", "🛠 Админ"],
]


def get_env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            restaurant TEXT NOT NULL,
            role TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
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
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_exam (
            exam_date TEXT PRIMARY KEY,
            question_json TEXT NOT NULL
        )
    """)
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
            role=excluded.role,
            is_admin=excluded.is_admin
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


def save_progress(telegram_id: int, action_type: str, category: Optional[str] = None, item_name: Optional[str] = None,
                  score: int = 0, max_score: int = 0, feedback: Optional[str] = None) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO progress (telegram_id, action_type, category, item_name, score, max_score, feedback, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (telegram_id, action_type, category, item_name, score, max_score, feedback, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_user_stats(telegram_id: int) -> Dict[str, Any]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) as total_actions,
               COALESCE(SUM(score), 0) as total_score,
               COALESCE(SUM(max_score), 0) as total_max
        FROM progress
        WHERE telegram_id = ?
    """, (telegram_id,))
    summary = cur.fetchone()

    cur.execute("""
        SELECT category,
               AVG(CASE WHEN max_score > 0 THEN CAST(score AS FLOAT) / max_score END) as avg_ratio,
               COUNT(*) as cnt
        FROM progress
        WHERE telegram_id = ? AND category IS NOT NULL
        GROUP BY category
        ORDER BY avg_ratio ASC, cnt DESC
    """, (telegram_id,))
    categories = cur.fetchall()

    conn.close()
    return {"summary": dict(summary), "categories": [dict(x) for x in categories]}


def get_network_stats() -> List[sqlite3.Row]:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT e.restaurant,
               COUNT(DISTINCT e.telegram_id) as employees,
               COUNT(p.id) as activities,
               COALESCE(AVG(CASE WHEN p.max_score > 0 THEN CAST(p.score AS FLOAT) / p.max_score END), 0) as avg_ratio
        FROM employees e
        LEFT JOIN progress p ON e.telegram_id = p.telegram_id
        GROUP BY e.restaurant
        ORDER BY avg_ratio DESC, activities DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def make_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)


def load_menu() -> Dict[str, Any]:
    with open(MENU_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_category_by_slug(menu_data: Dict[str, Any], slug: str) -> Optional[Dict[str, Any]]:
    for c in menu_data.get("categories", []):
        if c["slug"] == slug:
            return c
    return None


def get_item_by_id(category: Dict[str, Any], item_id: str) -> Optional[Dict[str, Any]]:
    for i in category.get("items", []):
        if i["id"] == item_id:
            return i
    return None


def is_promo_category(category: Dict[str, Any]) -> bool:
    return category["slug"] in {"promo", "sets", "combo", "combos", "kombo"} or "акц" in category["name"].lower() or "комбо" in category["name"].lower() or "сет" in category["name"].lower()


def categories_keyboard(menu_data: Dict[str, Any], promo_only: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    for c in menu_data.get("categories", []):
        if promo_only and not is_promo_category(c):
            continue
        if not promo_only and is_promo_category(c):
            continue
        buttons.append([InlineKeyboardButton(f"{c['emoji']} {c['name']}", callback_data=f"cat:{c['slug']}")])
    if not buttons:
        buttons = [[InlineKeyboardButton("Нет данных", callback_data="noop")]]
    return InlineKeyboardMarkup(buttons)


def items_keyboard(category: Dict[str, Any], mode: str = "view") -> InlineKeyboardMarkup:
    buttons = []
    for item in category.get("items", [])[:80]:
        buttons.append([InlineKeyboardButton(item["name"], callback_data=f"item:{mode}:{category['slug']}:{item['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"back:{mode}")])
    return InlineKeyboardMarkup(buttons)


def academy_keyboard(menu_data: Dict[str, Any]) -> InlineKeyboardMarkup:
    buttons = []
    for c in menu_data.get("categories", []):
        buttons.append([InlineKeyboardButton(f"{c['emoji']} {c['name']}", callback_data=f"academy:{c['slug']}")])
    return InlineKeyboardMarkup(buttons or [[InlineKeyboardButton("Нет данных", callback_data="noop")]])


def item_actions_keyboard(category_slug: str, item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📘 Разобрать глубже", callback_data=f"deep:{category_slug}:{item_id}")],
        [InlineKeyboardButton("🎯 Как продавать", callback_data=f"sell:{category_slug}:{item_id}")],
        [InlineKeyboardButton("🧠 Мини-вопрос", callback_data=f"microquiz:{category_slug}:{item_id}")],
        [InlineKeyboardButton("⬅️ К позиции", callback_data=f"cat:{category_slug}")],
    ])


def normalize_pitch(name: str, category_name: str, item: Dict[str, Any]) -> str:
    if item.get("sell_pitch"):
        return item["sell_pitch"]
    if category_name == "Шаурма":
        return f"{name} — это сытный и понятный вариант, который удобно рекомендовать, когда гость хочет быстро выбрать."
    if "сет" in name.lower() or category_name in {"Сеты", "Роллы", "Пицца"}:
        return f"{name} удобно продавать как готовое решение для компании, пары или заказа на несколько человек."
    if category_name in {"Десерты & Блины", "Горячие напитки"}:
        return f"{name} хорошо работает как мягкая допродажа к кофе или чаю."
    return f"{name} — понятная позиция, которую удобно рекомендовать по запросу гостя."


def normalize_upsell(category_name: str, item: Dict[str, Any]) -> List[str]:
    if item.get("upsell"):
        return item["upsell"]
    if category_name == "Шаурма":
        return ["Напиток", "Соус", "Картофель"]
    if category_name in {"Бургеры", "Закуски и гарниры"}:
        return ["Картофель", "Соус", "Напиток"]
    if category_name in {"Пицца", "Роллы", "Сеты"}:
        return ["Напиток 1 л", "Соус", "Десерт"]
    if category_name in {"Десерты & Блины"}:
        return ["Кофе", "Чай"]
    return ["Напиток"]


def build_item_card(item: Dict[str, Any], category_name: str, deep: bool = False) -> str:
    pitch = normalize_pitch(item["name"], category_name, item)
    upsell = normalize_upsell(category_name, item)
    composition = item.get("composition", "Состав уточняется по внутреннему стандарту меню.")
    if not deep:
        return (
            f"*{item['name']}*\n"
            f"{category_name}\n"
            f"Вес: {item.get('weight', '—')}   Цена: {item.get('price', '—')}\n\n"
            f"*Как коротко подать гостю*\n{pitch}\n\n"
            f"*Что допродать*\n" + "\n".join(f"• {x}" for x in upsell)
        )
    return (
        f"*{item['name']}*\n"
        f"{category_name}\n"
        f"Вес: {item.get('weight', '—')}   Цена: {item.get('price', '—')}\n\n"
        f"*1. Суть блюда*\n{pitch}\n\n"
        f"*2. Состав*\n{composition}\n\n"
        f"*3. Когда предлагать*\n"
        f"• когда гость не хочет долго выбирать\n"
        f"• когда запрос совпадает с категорией блюда\n"
        f"• когда можно сделать мягкую допродажу\n\n"
        f"*4. Что допродать*\n" + "\n".join(f"• {x}" for x in upsell) + "\n\n"
        f"*5. Готовая фраза*\n"
        f"\"Рекомендую {item['name']}. {pitch} Также могу предложить {upsell[0].lower()}.\""
    )


def academy_lesson(category: Dict[str, Any]) -> str:
    items = category.get("items", [])
    top = "\n".join(f"• {x['name']} — {x.get('price', '—')}" for x in items[:8]) or "• Нет позиций"
    category_name = category["name"]

    if category_name == "Шаурма":
        logic = "Главное в продаже шаурмы: быстро понять, курица/говядина/ассорти, классика или интереснее, обычная или более сытная."
        focus = "Допродажа: напиток, соус, картофель."
    elif category_name in {"Акции", "Комбо", "Сеты"} or is_promo_category(category):
        logic = "Эти позиции продаются через выгоду, объём и готовое решение для компании."
        focus = "Главное объяснить выгоду, состав набора и на сколько человек хватит."
    elif category_name == "Бургеры":
        logic = "В бургерах нужно быстро объяснять разницу между базовым, сырным и более сытным вариантом."
        focus = "Допродажа: фри, соус, напиток."
    elif category_name == "Пицца":
        logic = "Пиццу удобно продавать на компанию или как выбор между классикой, премиумом и более ярким вкусом."
        focus = "Допродажа: напиток 1 литр, соус, десерт."
    elif category_name == "Роллы":
        logic = "По роллам важно помочь гостю выбрать между понятной классикой, горячими/запеченными и сетами."
        focus = "Допродажа: напиток, сет, десерт."
    else:
        logic = "Главная задача — не пересказывать меню, а помогать гостю быстро выбрать 1–2 релевантные позиции."
        focus = "Всегда думай, что уместно допродать к основной позиции."

    return (
        f"*🎓 Академия: {category_name}*\n\n"
        f"*Логика категории*\n{logic}\n\n"
        f"*Ключевые позиции*\n{top}\n\n"
        f"*Фокус для официанта/кассира*\n{focus}\n\n"
        f"*Что должен уметь сотрудник*\n"
        f"• назвать 3–5 ключевых позиций\n"
        f"• объяснить разницу между похожими блюдами\n"
        f"• сделать одну уместную допродажу\n"
        f"• не перегружать гостя деталями"
    )


def generate_questions(menu_data: Dict[str, Any], count: int = 10, promo_bias: bool = False) -> List[Dict[str, Any]]:
    pairs = []
    for cat in menu_data.get("categories", []):
        if promo_bias and not is_promo_category(cat):
            continue
        for item in cat.get("items", []):
            pairs.append((cat, item))
    if not pairs:
        for cat in menu_data.get("categories", []):
            for item in cat.get("items", []):
                pairs.append((cat, item))
    random.shuffle(pairs)
    selected = pairs[:count]
    result = []
    for cat, item in selected:
        pool = [x[1]["name"] for x in pairs if x[1]["name"] != item["name"]]
        random.shuffle(pool)
        options = [item["name"]] + pool[:3]
        random.shuffle(options)
        hint = item.get("quiz_hint") or f"Позиция из категории {cat['name']}"
        result.append({
            "category": cat["name"],
            "item_name": item["name"],
            "question": f"Какое блюдо подходит под описание: {hint}",
            "options": options,
            "answer": item["name"],
        })
    return result


def get_today_exam(menu_data: Dict[str, Any]) -> Dict[str, Any]:
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM daily_exam WHERE exam_date = ?", (today,))
    row = cur.fetchone()
    if row:
        conn.close()
        return json.loads(row["question_json"])
    # mixed exam with menu + promo
    questions = generate_questions(menu_data, count=12, promo_bias=False)
    exam = {"date": today, "questions": questions}
    cur.execute("INSERT OR REPLACE INTO daily_exam (exam_date, question_json) VALUES (?, ?)", (today, json.dumps(exam, ensure_ascii=False)))
    conn.commit()
    conn.close()
    return exam


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return None
    return OpenAI(api_key=api_key)


def local_sales_feedback(answer: str) -> Tuple[int, str]:
    text = answer.lower()
    score = 4
    good, bad = [], []

    if "?" in answer:
        score += 2
        good.append("Есть уточняющий вопрос.")
    else:
        bad.append("Не хватает уточнения запроса гостя.")

    if any(x in text for x in ["рекоменд", "могу предложить", "совет"]):
        score += 2
        good.append("Есть уверенная рекомендация.")
    else:
        bad.append("Ответ звучит не как рекомендация, а слишком нейтрально.")

    if any(x in text for x in ["напит", "соус", "картоф", "десерт", "кофе"]):
        score += 2
        good.append("Есть допродажа.")
    else:
        bad.append("Нет дополнительной продажи.")

    if len(answer.split()) >= 12:
        score += 2
        good.append("Ответ достаточно развернутый.")
    else:
        bad.append("Ответ слишком короткий.")

    score = max(1, min(10, score))
    return score, (
        f"Оценка: *{score}/10*\n\n"
        f"*Что хорошо:*\n" + ("\n".join(f"• {x}" for x in good) if good else "• Пока мало сильных элементов.") + "\n\n"
        f"*Что упущено:*\n" + ("\n".join(f"• {x}" for x in bad) if bad else "• Критичных упущений нет.") + "\n\n"
        f"*Как усилить ответ:*\n• Сначала уточни запрос, затем предложи 1–2 позиции и мягко добавь допродажу."
    )


async def ai_evaluate(menu_data: Dict[str, Any], scenario: Dict[str, Any], answer: str) -> Tuple[int, str]:
    client = get_openai_client()
    if client is None:
        return local_sales_feedback(answer)

    compact = []
    for c in menu_data.get("categories", []):
        for i in c.get("items", [])[:10]:
            compact.append({
                "category": c["name"],
                "name": i["name"],
                "pitch": normalize_pitch(i["name"], c["name"], i),
                "upsell": normalize_upsell(c["name"], i),
            })

    system = (
        "Ты — тренер официантов и кассиров сети SF Shaurma Food. "
        "Оцени ответ сотрудника по 10-балльной шкале. "
        "Смотри на понимание запроса, релевантность рекомендации, презентацию блюда и допродажу. "
        "Формат строго:\n"
        "Оценка: X/10\n\nЧто хорошо:\n- ...\n\nЧто упущено:\n- ...\n\nКак усилить:\n- ..."
    )
    payload = json.dumps({"scenario": scenario, "answer": answer, "menu": compact[:60]}, ensure_ascii=False)
    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": payload}],
            temperature=0.2,
        )
        content = response.choices[0].message.content.strip()
        m = re.search(r"(\d{1,2})/10", content)
        score = int(m.group(1)) if m else 7
        return score, content
    except Exception:
        return local_sales_feedback(answer)


SALES_SCENARIOS = [
    {"id": "choice", "guest_message": "Я не знаю, что взять, посоветуйте.", "goal": "Уточнить запрос и предложить 1–2 позиции."},
    {"id": "light", "guest_message": "Хочу что-то лёгкое, но чтобы наесться.", "goal": "Предложить более лёгкие по восприятию позиции."},
    {"id": "difference", "guest_message": "В чем разница между курицей и говядиной?", "goal": "Просто объяснить вкус и помочь выбрать."},
    {"id": "upsell", "guest_message": "Мне только шаурму.", "goal": "Сделать мягкую и уместную допродажу."},
    {"id": "coffee", "guest_message": "Что взять к кофе?", "goal": "Предложить 2 сладкие позиции и помочь выбрать."},
]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    employee = get_employee(update.effective_user.id)
    if employee:
        await update.message.reply_text(
            f"Привет, {employee['full_name']}!\nЭто учебный бот SF для официантов и кассиров.",
            reply_markup=make_main_keyboard(),
        )
        return ConversationHandler.END
    await update.message.reply_text("Привет! Давай зарегистрируемся.\nНапиши имя и фамилию.")
    return REGISTER_NAME


async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["full_name"] = update.message.text.strip()
    await update.message.reply_text("Укажи ресторан / точку.")
    return REGISTER_RESTAURANT


async def register_restaurant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["restaurant"] = update.message.text.strip()
    await update.message.reply_text("Укажи роль: официант / кассир / администратор / директор.")
    return REGISTER_ROLE


async def register_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    role = update.message.text.strip().lower()
    is_admin = 1 if role in {"администратор", "директор", "admin", "manager"} else 0
    upsert_employee(update.effective_user.id, context.user_data["full_name"], context.user_data["restaurant"], role, is_admin)
    await update.message.reply_text("Готово. Бот настроен.", reply_markup=make_main_keyboard())
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено.", reply_markup=make_main_keyboard())
    return ConversationHandler.END


async def send_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    idx = context.user_data.get("quiz_index", 0)
    questions = context.user_data.get("quiz_questions", [])
    if idx >= len(questions):
        score = context.user_data.get("quiz_score", 0)
        max_score = len(questions)
        mode = context.user_data.get("quiz_mode", "quiz")
        save_progress(update.effective_user.id, mode, "mixed", None, score, max_score, f"Результат {score}/{max_score}")
        text = f"{'Экзамен дня' if mode == 'daily_exam' else 'Тест'} завершён.\nРезультат: {score}/{max_score}"
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text, reply_markup=make_main_keyboard())
        return

    q = questions[idx]
    kb = [[InlineKeyboardButton(opt, callback_data=f"quiz:{opt}")] for opt in q["options"]]
    kb.append([InlineKeyboardButton("❌ Завершить", callback_data="quiz:end")])
    text = f"*Вопрос {idx+1}/{len(questions)}*\n{q['question']}\nКатегория: {q['category']}"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))


async def handle_ai_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    answer = update.message.text.strip()
    scenario = context.user_data.get("sales_scenario")
    menu_data = context.bot_data["menu_data"]
    score, feedback = await ai_evaluate(menu_data, scenario, answer)
    save_progress(update.effective_user.id, "sales_training", "sales", scenario["id"], score, 10, feedback)
    context.user_data["awaiting_ai_answer"] = False
    context.user_data.pop("sales_scenario", None)
    await update.message.reply_text(f"*Разбор ответа*\n\n{feedback}", parse_mode=ParseMode.MARKDOWN, reply_markup=make_main_keyboard())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("awaiting_ai_answer"):
        await handle_ai_answer(update, context)
        return

    menu_data = context.bot_data["menu_data"]
    employee = get_employee(update.effective_user.id)
    if not employee:
        await update.message.reply_text("Сначала нажми /start.")
        return

    text = update.message.text.strip()

    if text == "📚 Меню":
        await update.message.reply_text("*Меню*\nВыбери категорию.", parse_mode=ParseMode.MARKDOWN, reply_markup=categories_keyboard(menu_data, promo_only=False))
        return

    if text == "🎁 Акции и комбо":
        await update.message.reply_text("*Акции / комбо / сеты*\nИзучи выгодные предложения и наборы.", parse_mode=ParseMode.MARKDOWN, reply_markup=categories_keyboard(menu_data, promo_only=True))
        return

    if text == "🎓 Академия продаж":
        await update.message.reply_text("*Академия продаж*\nВыбери категорию для глубокого изучения.", parse_mode=ParseMode.MARKDOWN, reply_markup=academy_keyboard(menu_data))
        return

    if text == "🧠 Тест по меню":
        context.user_data["quiz_questions"] = generate_questions(menu_data, count=10, promo_bias=False)
        context.user_data["quiz_index"] = 0
        context.user_data["quiz_score"] = 0
        context.user_data["quiz_mode"] = "menu_quiz"
        await send_next_question(update, context)
        return

    if text == "🔥 Экзамен дня":
        exam = get_today_exam(menu_data)
        context.user_data["quiz_questions"] = exam["questions"]
        context.user_data["quiz_index"] = 0
        context.user_data["quiz_score"] = 0
        context.user_data["quiz_mode"] = "daily_exam"
        await send_next_question(update, context)
        return

    if text == "💬 Тренировка продаж":
        scenario = random.choice(SALES_SCENARIOS)
        context.user_data["sales_scenario"] = scenario
        context.user_data["awaiting_ai_answer"] = True
        await update.message.reply_text(
            f"*Сценарий*\n\nГость: _{scenario['guest_message']}_\n\n*Задача:* {scenario['goal']}\n\nНапиши свой ответ как сотрудник.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if text == "📊 Мой прогресс":
        stats = get_user_stats(update.effective_user.id)
        s = stats["summary"]
        percent = round((s["total_score"] / s["total_max"]) * 100) if s["total_max"] else 0
        weak = "\n".join(f"• {x['category']} — {round((x['avg_ratio'] or 0)*100)}% ({x['cnt']} актив.)" for x in stats["categories"][:5]) or "• Пока мало данных"
        await update.message.reply_text(
            f"*Твой прогресс*\n\n"
            f"Активностей: {s['total_actions']}\n"
            f"Средний результат: {percent}%\n\n"
            f"*Зоны роста*\n{weak}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if text == "🛠 Админ":
        if not employee["is_admin"]:
            await update.message.reply_text("Раздел доступен администратору или директору.")
            return
        rows = get_network_stats()
        lines = ["*Админ-панель*\n", "*По ресторанам:*"]
        for row in rows:
            lines.append(f"• {row['restaurant']}: сотрудников {row['employees']}, активностей {row['activities']}, средний результат {round(row['avg_ratio']*100)}%")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    await update.message.reply_text("Выбери раздел на клавиатуре.", reply_markup=make_main_keyboard())


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    menu_data = context.bot_data["menu_data"]
    data = query.data

    if data == "noop":
        return

    if data.startswith("back:"):
        mode = data.split(":", 1)[1]
        if mode == "view":
            await query.edit_message_text("Выбери категорию:", reply_markup=categories_keyboard(menu_data, promo_only=False))
        else:
            await query.edit_message_text("Выбери категорию для изучения:", reply_markup=academy_keyboard(menu_data))
        return

    if data.startswith("cat:"):
        slug = data.split(":", 1)[1]
        category = get_category_by_slug(menu_data, slug)
        if not category:
            await query.edit_message_text("Категория не найдена.")
            return
        await query.edit_message_text(f"*{category['emoji']} {category['name']}*\nВыбери позицию.", parse_mode=ParseMode.MARKDOWN, reply_markup=items_keyboard(category, mode="view"))
        return

    if data.startswith("academy:"):
        slug = data.split(":", 1)[1]
        category = get_category_by_slug(menu_data, slug)
        if not category:
            await query.edit_message_text("Категория не найдена.")
            return
        save_progress(update.effective_user.id, "academy_open", category["name"], None, 1, 1, "Открыт урок категории")
        await query.edit_message_text(
            academy_lesson(category),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📚 Открыть позиции категории", callback_data=f"academy_items:{slug}")],
                [InlineKeyboardButton("🧠 Тест по категории", callback_data=f"academy_quiz:{slug}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="back:academy")],
            ]),
        )
        return

    if data.startswith("academy_items:"):
        slug = data.split(":", 1)[1]
        category = get_category_by_slug(menu_data, slug)
        await query.edit_message_text(f"*Позиции категории {category['name']}*\nВыбери блюдо для разбора.", parse_mode=ParseMode.MARKDOWN, reply_markup=items_keyboard(category, mode="academy"))
        return

    if data.startswith("item:"):
        _, mode, slug, item_id = data.split(":", 3)
        category = get_category_by_slug(menu_data, slug)
        item = get_item_by_id(category, item_id) if category else None
        if not item:
            await query.edit_message_text("Позиция не найдена.")
            return
        save_progress(update.effective_user.id, "menu_view", category["name"], item["name"], 1, 1, "Открыта карточка блюда")
        await query.edit_message_text(build_item_card(item, category["name"], deep=False), parse_mode=ParseMode.MARKDOWN, reply_markup=item_actions_keyboard(slug, item_id))
        return

    if data.startswith("deep:"):
        _, slug, item_id = data.split(":", 2)
        category = get_category_by_slug(menu_data, slug)
        item = get_item_by_id(category, item_id) if category else None
        if not item:
            await query.edit_message_text("Позиция не найдена.")
            return
        save_progress(update.effective_user.id, "deep_learning", category["name"], item["name"], 1, 1, "Глубокое изучение блюда")
        await query.edit_message_text(build_item_card(item, category["name"], deep=True), parse_mode=ParseMode.MARKDOWN, reply_markup=item_actions_keyboard(slug, item_id))
        return

    if data.startswith("sell:"):
        _, slug, item_id = data.split(":", 2)
        category = get_category_by_slug(menu_data, slug)
        item = get_item_by_id(category, item_id) if category else None
        pitch = normalize_pitch(item["name"], category["name"], item)
        upsell = normalize_upsell(category["name"], item)
        await query.edit_message_text(
            f"*Как продавать: {item['name']}*\n\n"
            f"*Что говорить*\n• {pitch}\n\n"
            f"*Что допродать*\n" + "\n".join(f"• {x}" for x in upsell) + "\n\n"
            f"*Чего избегать*\n• не перечислять всё меню\n• не читать сухой состав\n• не давить на гостя",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=item_actions_keyboard(slug, item_id),
        )
        return

    if data.startswith("microquiz:"):
        _, slug, item_id = data.split(":", 2)
        category = get_category_by_slug(menu_data, slug)
        item = get_item_by_id(category, item_id) if category else None
        others = [x["name"] for x in category["items"] if x["id"] != item_id][:3]
        options = [item["name"]] + others[:3]
        random.shuffle(options)
        context.user_data["microquiz_answer"] = item["name"]
        context.user_data["microquiz_category"] = category["name"]
        context.user_data["microquiz_item"] = item["name"]
        kb = [[InlineKeyboardButton(opt, callback_data=f"microans:{opt}")] for opt in options]
        kb.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"deep:{slug}:{item_id}")])
        hint = item.get("quiz_hint") or f"Позиция из категории {category['name']}"
        await query.edit_message_text(f"*Мини-вопрос*\nКакое блюдо подходит под описание:\n_{hint}_", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("microans:"):
        answer = data.split(":", 1)[1]
        right = context.user_data.get("microquiz_answer")
        cat = context.user_data.get("microquiz_category")
        item_name = context.user_data.get("microquiz_item")
        ok = answer == right
        save_progress(update.effective_user.id, "microquiz", cat, item_name, 1 if ok else 0, 1, "Мини-вопрос")
        await query.edit_message_text(f"{'✅ Верно' if ok else '❌ Неверно'}\nПравильный ответ: *{right}*", parse_mode=ParseMode.MARKDOWN)
        return

    if data.startswith("academy_quiz:"):
        slug = data.split(":", 1)[1]
        category = get_category_by_slug(menu_data, slug)
        pairs = [(category, i) for i in category.get("items", [])]
        random.shuffle(pairs)
        questions = []
        for cat, item in pairs[:8]:
            pool = [x[1]["name"] for x in pairs if x[1]["name"] != item["name"]]
            random.shuffle(pool)
            options = [item["name"]] + pool[:3]
            random.shuffle(options)
            hint = item.get("quiz_hint") or f"Позиция из категории {cat['name']}"
            questions.append({
                "category": cat["name"],
                "item_name": item["name"],
                "question": f"Какое блюдо подходит под описание: {hint}",
                "options": options,
                "answer": item["name"],
            })
        context.user_data["quiz_questions"] = questions
        context.user_data["quiz_index"] = 0
        context.user_data["quiz_score"] = 0
        context.user_data["quiz_mode"] = "academy_quiz"
        await send_next_question(update, context)
        return

    if data.startswith("quiz:"):
        if data == "quiz:end":
            context.user_data["quiz_index"] = len(context.user_data.get("quiz_questions", []))
            await send_next_question(update, context)
            return
        answer = data.split(":", 1)[1]
        idx = context.user_data.get("quiz_index", 0)
        questions = context.user_data.get("quiz_questions", [])
        current = questions[idx]
        ok = answer == current["answer"]
        if ok:
            context.user_data["quiz_score"] = context.user_data.get("quiz_score", 0) + 1
        context.user_data["quiz_index"] = idx + 1
        await query.edit_message_text(f"{'✅ Верно' if ok else '❌ Неверно'}\nПравильный ответ: *{current['answer']}*", parse_mode=ParseMode.MARKDOWN)
        await send_next_question(update, context)
        return


def build_app() -> Application:
    token = get_env("TELEGRAM_BOT_TOKEN")
    app = Application.builder().token(token).build()
    init_db()
    app.bot_data["menu_data"] = load_menu()

    registration = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            REGISTER_RESTAURANT: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_restaurant)],
            REGISTER_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_role)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(registration)
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def main() -> None:
    app = build_app()
    logger.info("Bot started")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
