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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
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
logger = logging.getLogger("sf_waiter_ai_bot_v2")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bot.db"
MENU_PATH = BASE_DIR / "sample_menu.json"
SITE_URL = os.getenv("SF_SITE_URL", "https://shaurma-food.kz")

REGISTER_NAME, REGISTER_RESTAURANT, REGISTER_ROLE = range(3)

MAIN_MENU = [
    ["🍽 Меню", "🎁 Акции"],
    ["🎓 Академия меню", "🧪 Тесты"],
    ["💬 Продажи AI", "📈 Прогресс"],
    ["🔥 Экзамен дня", "🛠 Админ"],
]

PROMO_SLUGS = {"promo", "promotions", "stocks", "aktsii", "action"}

CATEGORY_NAME_MAP = {
    "promo": "Акции",
    "sets": "Сеты",
    "combo": "Комбо",
    "combos": "Комбо",
    "shaurma": "Шаурма",
    "burgers": "Бургеры",
    "pizza": "Пицца",
    "sushi_i_rolli": "Роллы",
    "rolls": "Роллы",
    "shashlyki": "Шашлыки",
    "zavtraki": "Завтраки",
    "soups": "Супы",
    "vostochnaya_kukhnya": "Восточная кухня",
    "salads": "Салаты",
    "stejki_i_goryachee": "Стейки и горячее",
    "kids_menu": "Детское меню",
    "poke_udon": "Поке & Удон",
    "zakuski_i_garniry": "Закуски и гарниры",
    "sauces": "Соусы",
    "desserty_i_bliny": "Десерты & Блины",
    "drinks": "Напитки",
    "sezonnoe_menyu": "Сезонное меню",
}

EMOJI_BY_NAME = {
    "Акции": "🎁",
    "Сеты": "🍱",
    "Комбо": "🔥",
    "Шаурма": "🌯",
    "Бургеры": "🍔",
    "Пицца": "🍕",
    "Роллы": "🍣",
    "Шашлыки": "🍢",
    "Завтраки": "🍳",
    "Супы": "🍜",
    "Восточная кухня": "🥘",
    "Салаты": "🥗",
    "Стейки и горячее": "🥩",
    "Детское меню": "👶",
    "Поке & Удон": "🍲",
    "Закуски и гарниры": "🍟",
    "Соусы": "🫙",
    "Десерты & Блины": "🧁",
    "Напитки": "🥤",
    "Сезонное меню": "🌟",
}

SALES_SCENARIOS = [
    {
        "id": "unknown_choice",
        "guest_message": "Я не знаю, что взять, посоветуйте.",
        "goal": "Сначала сузить выбор, затем предложить 1–2 позиции и допродажу.",
    },
    {
        "id": "light",
        "guest_message": "Хочу что-то лёгкое, но чтобы наесться.",
        "goal": "Предложить более лёгкие по восприятию позиции и допродать напиток/десерт.",
    },
    {
        "id": "difference",
        "guest_message": "А в чем разница между курицей и говядиной в шаурме?",
        "goal": "Объяснить разницу простым языком и помочь выбрать.",
    },
    {
        "id": "coffee",
        "guest_message": "Что можно взять к кофе?",
        "goal": "Предложить 2 десерта/сладкие позиции и помочь быстро выбрать.",
    },
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
            category TEXT NOT NULL,
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
            telegram_id, action_type, category, item_name, score, max_score,
            feedback, datetime.now().isoformat(),
        ),
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
        ORDER BY avg_ratio ASC
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


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def slug_to_name(slug: str) -> str:
    return CATEGORY_NAME_MAP.get(slug, slug.replace("_", " ").replace("-", " ").title())


def name_to_emoji(name: str) -> str:
    return EMOJI_BY_NAME.get(name, "🍽")


def load_menu() -> Dict[str, Any]:
    if not MENU_PATH.exists():
        return {"brand": "SF Shaurma Food", "categories": []}
    with open(MENU_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_menu(data: Dict[str, Any]) -> None:
    with open(MENU_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_category_by_slug(menu_data: Dict[str, Any], slug: str) -> Optional[Dict[str, Any]]:
    for category in menu_data.get("categories", []):
        if category["slug"] == slug:
            return category
    return None


def get_item_by_id(category: Dict[str, Any], item_id: str) -> Optional[Dict[str, Any]]:
    for item in category.get("items", []):
        if item["id"] == item_id:
            return item
    return None


def site_sync_summary(menu_data: Dict[str, Any]) -> str:
    categories = len(menu_data.get("categories", []))
    items = sum(len(c.get("items", [])) for c in menu_data.get("categories", []))
    return f"Категорий: {categories}\nПозиций: {items}\nИсточник: {menu_data.get('source_note', 'локальная база')}"


def parse_weight_price(text: str) -> Tuple[str, str]:
    text = normalize_space(text)
    weight_match = re.search(r"(\d+[.,]?\d*)\s*(г|g|кг|kg|л|ml|мл)", text, flags=re.I)
    price_match = re.search(r"(от\s*)?(\d[\d\s]*)\s*₸", text)
    weight = weight_match.group(0) if weight_match else "—"
    price = price_match.group(0) if price_match else "—"
    return weight, price


def build_learning_fields(item: Dict[str, Any], category_name: str) -> Dict[str, Any]:
    name = item["name"]
    composition = item.get("composition", "Состав уточняется по карточке блюда на сайте.")
    if category_name == "Шаурма":
        guest_fit = "Подходит гостям, которые хотят быстро, сытно и понятно."
        objections = [
            "Если гость сомневается между курицей и говядиной — объясни разницу по вкусу, а не составом.",
            "Если гость боится, что будет слишком тяжело — предложи меньший формат или напиток без газа.",
        ]
        pitch = f"{name} — это сытный и понятный вариант, который легко рекомендовать, когда гость не хочет долго выбирать."
        upsell = ["Напиток", "Соус", "Картофель"]
    elif category_name in {"Пицца", "Роллы", "Сеты"}:
        guest_fit = "Хорошо подходит для компании, пары или гостя, который хочет разделить блюдо."
        objections = [
            "Если гость не уверен по объему — помоги понять, на сколько человек хватит.",
            "Если гость хочет безопасный выбор — предложи самые понятные вкусы.",
        ]
        pitch = f"{name} удобно продавать как готовое решение без сложного выбора."
        upsell = ["Напиток 1 л", "Десерт", "Доп. соус"]
    elif category_name in {"Десерты & Блины", "Завтраки"}:
        guest_fit = "Подходит, когда гость хочет мягкий, понятный или сладкий вкус."
        objections = [
            "Если гость уже взял кофе — помоги быстро подобрать сладкую пару.",
            "Не перегружай длинным описанием — лучше дать 1–2 понятных варианта.",
        ]
        pitch = f"{name} удобно предлагать как дополнение к кофе или чаю."
        upsell = ["Кофе", "Чай"]
    else:
        guest_fit = "Подходит гостям, которые хотят понятный вкус и быстрый выбор."
        objections = [
            "Сначала уточни предпочтение: мясо/полегче/острее/для компании.",
            "Не перечисляй всё подряд — лучше предложить 1–2 релевантные позиции.",
        ]
        pitch = item.get("sell_pitch") or f"{name} — понятная позиция, которую удобно рекомендовать по запросу гостя."
        upsell = item.get("upsell", ["Напиток"])

    return {
        "guest_fit": guest_fit,
        "pitch": pitch,
        "upsell": upsell,
        "objections": objections,
        "composition": composition,
        "quiz_hint": item.get("quiz_hint") or f"Позиция из категории {category_name}.",
    }


def build_item_card(item: Dict[str, Any], category_name: str, deep: bool = False) -> str:
    fields = build_learning_fields(item, category_name)
    if not deep:
        return (
            f"*{item['name']}*\n"
            f"{name_to_emoji(category_name)} {category_name}\n"
            f"Вес: {item.get('weight', '—')}   Цена: {item.get('price', '—')}\n\n"
            f"*Коротко для гостя:*\n{fields['pitch']}\n\n"
            f"*Что предложить дополнительно:*\n" +
            "\n".join(f"• {x}" for x in fields["upsell"])
        )

    return (
        f"*{item['name']}*\n"
        f"{name_to_emoji(category_name)} {category_name}\n"
        f"Вес: {item.get('weight', '—')}   Цена: {item.get('price', '—')}\n\n"
        f"*1. Что это за позиция*\n{fields['pitch']}\n\n"
        f"*2. Состав / суть блюда*\n{fields['composition']}\n\n"
        f"*3. Кому лучше предлагать*\n{fields['guest_fit']}\n\n"
        f"*4. Что допродать*\n" + "\n".join(f"• {x}" for x in fields["upsell"]) + "\n\n"
        f"*5. Частые ситуации / возражения*\n" + "\n".join(f"• {x}" for x in fields["objections"]) + "\n\n"
        f"*6. Мини-скрипт*\n"
        f"\"Рекомендую {item['name']}. {fields['pitch']} Также могу предложить {fields['upsell'][0].lower()}.\""
    )


def categories_keyboard(menu_data: Dict[str, Any], include_promo: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    for category in menu_data.get("categories", []):
        if not include_promo and category["slug"] in PROMO_SLUGS:
            continue
        if include_promo and category["slug"] not in PROMO_SLUGS:
            continue
        buttons.append([
            InlineKeyboardButton(
                f"{category['emoji']} {category['name']}",
                callback_data=f"cat:{category['slug']}"
            )
        ])
    return InlineKeyboardMarkup(buttons or [[InlineKeyboardButton("Нет данных", callback_data="noop")]])


def items_keyboard(category: Dict[str, Any], mode: str = "browse") -> InlineKeyboardMarkup:
    buttons = []
    for item in category.get("items", [])[:80]:
        buttons.append([InlineKeyboardButton(item["name"], callback_data=f"item:{mode}:{category['slug']}:{item['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Назад к категориям", callback_data=f"back_categories:{mode}")])
    return InlineKeyboardMarkup(buttons)


def item_actions_keyboard(category_slug: str, item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📘 Глубоко разобрать", callback_data=f"deep:{category_slug}:{item_id}")],
        [InlineKeyboardButton("🎯 Как это продавать", callback_data=f"sell:{category_slug}:{item_id}")],
        [InlineKeyboardButton("🧠 Мини-вопрос", callback_data=f"microquiz:{category_slug}:{item_id}")],
        [InlineKeyboardButton("⬅️ К списку блюд", callback_data=f"cat:{category_slug}")],
    ])


def academy_keyboard(menu_data: Dict[str, Any]) -> InlineKeyboardMarkup:
    buttons = []
    for category in menu_data.get("categories", []):
        if category["slug"] in PROMO_SLUGS:
            continue
        buttons.append([InlineKeyboardButton(f"{category['emoji']} {category['name']}", callback_data=f"academy:{category['slug']}")])
    return InlineKeyboardMarkup(buttons or [[InlineKeyboardButton("Нет данных", callback_data="noop")]])


def academy_lesson_text(category: Dict[str, Any]) -> str:
    items = category.get("items", [])
    top_examples = "\n".join(f"• {x['name']} — {x.get('price', '—')}" for x in items[:6]) or "• Пока нет позиций"
    if category["name"] == "Шаурма":
        logic = (
            "Эта категория продаётся через простоту выбора, сытость и понятный вкус.\n"
            "Главная задача официанта — быстро понять: курица или говядина, обычная или поинтереснее, стандарт или побольше."
        )
    elif category["name"] == "Пицца":
        logic = (
            "Пиццу удобно продавать как решение для компании или пары.\n"
            "Нужно понимать: классика, премиум, поострее или полегче."
        )
    elif category["name"] == "Роллы":
        logic = (
            "В роллах важна навигация: холодные, горячие, запечённые, понятные классические или наборы на компанию."
        )
    else:
        logic = (
            "Задача официанта — не перечитывать меню, а быстро переводить запрос гостя в 1–2 понятные рекомендации."
        )

    return (
        f"*🎓 Академия: {category['name']}*\n\n"
        f"*Как думать про категорию*\n{logic}\n\n"
        f"*Ключевые позиции*\n{top_examples}\n\n"
        f"*Что должен уметь официант*\n"
        f"• объяснить разницу между 2–3 основными позициями\n"
        f"• рекомендовать по запросу гостя\n"
        f"• сделать мягкую допродажу\n"
        f"• не перегружать лишними подробностями"
    )


def generate_quiz_questions(menu_data: Dict[str, Any], count: int = 7) -> List[Dict[str, Any]]:
    all_pairs = []
    for cat in menu_data.get("categories", []):
        if cat["slug"] in PROMO_SLUGS:
            continue
        for item in cat.get("items", []):
            all_pairs.append((cat, item))
    random.shuffle(all_pairs)
    selected = all_pairs[:count]
    questions = []
    for cat, item in selected:
        pool = [x[1]["name"] for x in all_pairs if x[1]["name"] != item["name"]]
        random.shuffle(pool)
        options = [item["name"]] + pool[:3]
        random.shuffle(options)
        questions.append({
            "category": cat["name"],
            "item_name": item["name"],
            "question": f"Какое блюдо подходит под описание: {item.get('quiz_hint', f'Позиция из категории {cat['name']}')}",
            "options": options,
            "answer": item["name"],
        })
    return questions


def get_today_exam(menu_data: Dict[str, Any]) -> Dict[str, Any]:
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM daily_exam WHERE exam_date = ?", (today,))
    row = cur.fetchone()
    if row:
        conn.close()
        return json.loads(row["question_json"])

    exam = {"date": today, "questions": generate_quiz_questions(menu_data, 10)}
    cur.execute(
        "INSERT OR REPLACE INTO daily_exam (exam_date, category, question_json) VALUES (?, ?, ?)",
        (today, "mixed", json.dumps(exam, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    return exam


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        return None
    return OpenAI(api_key=api_key)


def evaluate_locally(answer: str, scenario: Dict[str, Any]) -> Tuple[int, str]:
    text = answer.lower()
    score = 4
    notes_good = []
    notes_bad = []

    if "?" in answer:
        score += 2
        notes_good.append("Есть попытка уточнить потребность гостя.")
    else:
        notes_bad.append("Не хватает уточняющего вопроса.")

    if any(x in text for x in ["рекоменд", "совет", "могу предложить", "подойдет"]):
        score += 2
        notes_good.append("Есть уверенная рекомендация.")
    else:
        notes_bad.append("Ответ слабоват как рекомендация.")

    if any(x in text for x in ["напит", "соус", "десерт", "картоф", "кофе"]):
        score += 2
        notes_good.append("Есть допродажа.")
    else:
        notes_bad.append("Нет допродажи.")

    if len(answer.split()) >= 12:
        score += 2
        notes_good.append("Ответ достаточно развернутый.")
    else:
        notes_bad.append("Ответ короткий — не хватает презентации блюда.")

    score = max(1, min(10, score))
    feedback = (
        f"Оценка: *{score}/10*\n\n"
        f"*Что хорошо:*\n" + ("\n".join(f"• {x}" for x in notes_good) if notes_good else "• Пока мало сильных элементов.") + "\n\n"
        f"*Что упущено:*\n" + ("\n".join(f"• {x}" for x in notes_bad) if notes_bad else "• Критичных пробелов нет.") + "\n\n"
        f"*Как ответить сильнее:*\n• Сначала уточни запрос, затем предложи 1–2 позиции и мягко допродай напиток или дополнение."
    )
    return score, feedback


async def ai_evaluate(menu_data: Dict[str, Any], scenario: Dict[str, Any], answer: str) -> Tuple[int, str]:
    client = get_openai_client()
    if client is None:
        return evaluate_locally(answer, scenario)

    compact_menu = []
    for cat in menu_data.get("categories", []):
        for item in cat.get("items", [])[:20]:
            compact_menu.append({
                "category": cat["name"],
                "name": item["name"],
                "pitch": build_learning_fields(item, cat["name"])["pitch"],
                "upsell": build_learning_fields(item, cat["name"])["upsell"],
            })
    system = (
        "Ты — тренер официантов сети SF Shaurma Food. "
        "Оцени ответ официанта по 10-балльной шкале. "
        "Смотри на понимание запроса, релевантность рекомендации, вкусную презентацию и допродажу. "
        "Формат ответа строго такой:\n"
        "Оценка: X/10\n\nЧто хорошо:\n- ...\n\nЧто упущено:\n- ...\n\nКак ответить сильнее:\n- ..."
    )
    user_prompt = json.dumps({
        "scenario": scenario,
        "answer": answer,
        "menu_excerpt": compact_menu[:80],
    }, ensure_ascii=False)
    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user_prompt}],
            temperature=0.2,
        )
        content = response.choices[0].message.content.strip()
        score_match = re.search(r"(\d{1,2})/10", content)
        score = int(score_match.group(1)) if score_match else 7
        return score, content
    except Exception as e:
        logger.exception("OpenAI evaluate failed: %s", e)
        return evaluate_locally(answer, scenario)


def fetch_url(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SFWaiterBot/1.0)",
        "Accept-Language": "ru,en;q=0.9",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_links(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("/"):
            href = base_url.rstrip("/") + href
        if href.startswith(base_url):
            links.append(href.split("?")[0])
    return sorted(set(links))


def make_item_id(name: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9а-яА-Я]+", "_", name.lower()).strip("_")
    return text[:80] or "item"


def scrape_site_menu(site_url: str) -> Dict[str, Any]:
    # Strategy:
    # 1) open sitemap if available
    # 2) collect /menu/<category> and /menu/<category>/<item> links
    # 3) parse category pages for product cards
    sitemap_url = site_url.rstrip("/") + "/sitemap"
    try:
        html = fetch_url(sitemap_url)
    except Exception:
        html = fetch_url(site_url)

    links = extract_links(html, site_url)
    menu_links = [x for x in links if "/menu/" in x]
    category_map: Dict[str, Dict[str, Any]] = {}

    # infer categories from links
    for link in menu_links:
        parts = link.rstrip("/").split("/")
        try:
            idx = parts.index("menu")
        except ValueError:
            continue
        if len(parts) <= idx + 1:
            continue
        category_slug = parts[idx + 1]
        category_name = slug_to_name(category_slug)
        if category_slug not in category_map:
            category_map[category_slug] = {
                "slug": category_slug,
                "name": category_name,
                "emoji": name_to_emoji(category_name),
                "items": []
            }

    # parse category pages
    for category_slug, category in list(category_map.items()):
        url = f"{site_url.rstrip('/')}/menu/{category_slug}"
        try:
            cat_html = fetch_url(url)
        except Exception:
            continue

        soup = BeautifulSoup(cat_html, "html.parser")
        text = soup.get_text("\n", strip=True)
        lines = [normalize_space(x) for x in text.splitlines() if normalize_space(x)]
        # rough parser: find lines that look like product title + nearby weight/price
        seen = set()
        for i, line in enumerate(lines):
            if len(line) < 2 or len(line) > 120:
                continue
            if any(line.lower() == c["name"].lower() for c in category_map.values()):
                continue

            next_block = " ".join(lines[i:i+4])
            weight, price = parse_weight_price(next_block)

            # Heuristic: item line near weight/price or followed by them
            looks_like_item = (
                price != "—" or weight != "—" or
                any(tok in next_block.lower() for tok in ["₸", "г", "g", "new", "новинка", "from"])
            )
            bad = re.search(r"^(меню|назад|доставка|корзина|вход|регистрация|контакты|политика)", line.lower())
            if looks_like_item and not bad and line.lower() not in seen:
                seen.add(line.lower())
                category["items"].append({
                    "id": make_item_id(line),
                    "name": line,
                    "weight": weight,
                    "price": price,
                    "composition": "Описание и точный состав уточняются по карточке блюда на сайте.",
                    "sell_pitch": "",
                    "upsell": [],
                    "features": [],
                    "quiz_hint": f"Позиция из категории {category['name']}.",
                })

        # Deduplicate and trim obvious junk
        cleaned = []
        bad_words = ["telegram", "instagram", "facebook", "copyright", "заказать", "добавить", "оформить", "в корзину"]
        for item in category["items"]:
            low = item["name"].lower()
            if any(x in low for x in bad_words):
                continue
            if len(item["name"]) < 3:
                continue
            cleaned.append(item)
        # preserve order, unique names
        unique = {}
        for item in cleaned:
            unique.setdefault(item["name"], item)
        category["items"] = list(unique.values())[:120]

    categories = [x for x in category_map.values() if x["items"]]
    categories.sort(key=lambda x: x["name"])
    data = {
        "brand": "SF Shaurma Food",
        "source_note": f"Синхронизировано с сайта {site_url} {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "categories": categories,
    }
    return data


async def sync_menu_from_site(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    menu_data = await asyncio.to_thread(scrape_site_menu, SITE_URL)
    if menu_data.get("categories"):
        save_menu(menu_data)
        context.bot_data["menu_data"] = menu_data
        return menu_data
    raise RuntimeError("Не удалось собрать меню с сайта.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    employee = get_employee(update.effective_user.id)
    if employee:
        await update.message.reply_text(
            f"Привет, {employee['full_name']}!\nЭто SF Academy Bot.\nВыбирай нужный раздел ниже.",
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
    await update.message.reply_text("Укажи роль: официант / администратор / директор.")
    return REGISTER_ROLE


async def register_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    role = update.message.text.strip().lower()
    is_admin = 1 if role in {"администратор", "директор", "admin", "manager"} else 0
    upsert_employee(
        update.effective_user.id,
        context.user_data["full_name"],
        context.user_data["restaurant"],
        role,
        is_admin
    )
    await update.message.reply_text("Готово. Ты внутри SF Academy.", reply_markup=make_main_keyboard())
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Ок, отменено.", reply_markup=make_main_keyboard())
    return ConversationHandler.END


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("awaiting_ai_answer"):
        await handle_ai_answer(update, context)
        return

    menu_data = context.bot_data["menu_data"]
    text = update.message.text.strip()
    employee = get_employee(update.effective_user.id)
    if not employee:
        await update.message.reply_text("Сначала нажми /start.")
        return

    if text == "🍽 Меню":
        await update.message.reply_text(
            "*Меню*\nВыбери категорию, чтобы открыть позиции.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=categories_keyboard(menu_data, include_promo=False),
        )
        return

    if text == "🎁 Акции":
        await update.message.reply_text(
            "*Акции и промо*\nЗдесь собраны спецпредложения и акционные позиции.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=categories_keyboard(menu_data, include_promo=True),
        )
        return

    if text == "🎓 Академия меню":
        await update.message.reply_text(
            "*Академия меню*\nВыбери категорию для глубокого изучения логики продаж.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=academy_keyboard(menu_data),
        )
        return

    if text == "🧪 Тесты":
        context.user_data["quiz_questions"] = generate_quiz_questions(menu_data, 7)
        context.user_data["quiz_index"] = 0
        context.user_data["quiz_score"] = 0
        context.user_data["quiz_mode"] = "quiz"
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

    if text == "💬 Продажи AI":
        scenario = random.choice(SALES_SCENARIOS)
        context.user_data["sales_scenario"] = scenario
        context.user_data["awaiting_ai_answer"] = True
        await update.message.reply_text(
            f"*Сценарий гостя*\n\n"
            f"Гость: _{scenario['guest_message']}_\n\n"
            f"*Задача:* {scenario['goal']}\n\n"
            f"Напиши свой ответ как официант.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if text == "📈 Прогресс":
        stats = get_user_stats(update.effective_user.id)
        s = stats["summary"]
        percent = round((s["total_score"] / s["total_max"]) * 100) if s["total_max"] else 0
        weak = "\n".join(
            f"• {x['category']} — {round((x['avg_ratio'] or 0)*100)}% ({x['cnt']} актив.)"
            for x in stats["categories"][:5]
        ) or "• Пока мало данных"
        await update.message.reply_text(
            f"*Твой прогресс*\n\n"
            f"Активностей: {s['total_actions']}\n"
            f"Средний результат: {percent}%\n\n"
            f"*Где нужно усилиться*\n{weak}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if text == "🛠 Админ":
        if not employee["is_admin"]:
            await update.message.reply_text("Раздел доступен администратору или директору.")
            return
        rows = get_network_stats()
        lines = ["*Админ-панель*\n", "*Сеть по ресторанам:*"]
        for row in rows:
            lines.append(
                f"• {row['restaurant']}: сотрудников {row['employees']}, "
                f"активностей {row['activities']}, средний результат {round(row['avg_ratio']*100)}%"
            )
        lines.append("\nКоманды:\n/sync_site — обновить меню с сайта\n/menu_stats — показать статистику меню")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    await update.message.reply_text("Выбери нужный раздел на клавиатуре.", reply_markup=make_main_keyboard())


async def send_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    idx = context.user_data.get("quiz_index", 0)
    questions = context.user_data.get("quiz_questions", [])
    if idx >= len(questions):
        score = context.user_data.get("quiz_score", 0)
        max_score = len(questions)
        mode = context.user_data.get("quiz_mode", "quiz")
        save_progress(
            update.effective_user.id, mode, "mixed", None, score, max_score,
            f"Завершено: {score}/{max_score}"
        )
        title = "Экзамен дня" if mode == "daily_exam" else "Тест"
        if update.callback_query:
            await update.callback_query.edit_message_text(f"{title} завершён.\nРезультат: {score}/{max_score}")
        else:
            await update.message.reply_text(f"{title} завершён.\nРезультат: {score}/{max_score}")
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
    await update.message.reply_text(f"*Разбор ответа*\n\n{feedback}", parse_mode=ParseMode.MARKDOWN, reply_markup=make_main_keyboard())


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    menu_data = context.bot_data["menu_data"]
    data = query.data

    if data == "noop":
        return

    if data.startswith("back_categories:"):
        mode = data.split(":", 1)[1]
        if mode == "browse":
            await query.edit_message_text("Выбери категорию меню:", reply_markup=categories_keyboard(menu_data, include_promo=False))
        else:
            await query.edit_message_text("Выбери категорию для глубокого изучения:", reply_markup=academy_keyboard(menu_data))
        return

    if data.startswith("cat:"):
        slug = data.split(":", 1)[1]
        category = get_category_by_slug(menu_data, slug)
        if not category:
            await query.edit_message_text("Категория не найдена.")
            return
        await query.edit_message_text(
            f"*{category['emoji']} {category['name']}*\nВыбери позицию.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=items_keyboard(category, mode="browse"),
        )
        return

    if data.startswith("academy:"):
        slug = data.split(":", 1)[1]
        category = get_category_by_slug(menu_data, slug)
        if not category:
            await query.edit_message_text("Категория не найдена.")
            return
        save_progress(update.effective_user.id, "academy_open", category["name"], None, 1, 1, "Открыт урок категории")
        await query.edit_message_text(
            academy_lesson_text(category),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📚 Открыть позиции категории", callback_data=f"academy_items:{slug}")],
                [InlineKeyboardButton("🧠 Тест по категории", callback_data=f"academy_quiz:{slug}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="back_categories:academy")],
            ]),
        )
        return

    if data.startswith("academy_items:"):
        slug = data.split(":", 1)[1]
        category = get_category_by_slug(menu_data, slug)
        await query.edit_message_text(
            f"*Позиции категории {category['name']}*\nВыбери блюдо для детального разбора.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=items_keyboard(category, mode="academy"),
        )
        return

    if data.startswith("item:"):
        _, mode, slug, item_id = data.split(":", 3)
        category = get_category_by_slug(menu_data, slug)
        item = get_item_by_id(category, item_id) if category else None
        if not item:
            await query.edit_message_text("Позиция не найдена.")
            return
        save_progress(update.effective_user.id, "menu_view", category["name"], item["name"], 1, 1, "Открыта карточка блюда")
        await query.edit_message_text(
            build_item_card(item, category["name"], deep=False),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=item_actions_keyboard(slug, item_id),
        )
        return

    if data.startswith("deep:"):
        _, slug, item_id = data.split(":", 2)
        category = get_category_by_slug(menu_data, slug)
        item = get_item_by_id(category, item_id) if category else None
        if not item:
            await query.edit_message_text("Позиция не найдена.")
            return
        save_progress(update.effective_user.id, "deep_learning", category["name"], item["name"], 1, 1, "Глубокое изучение блюда")
        await query.edit_message_text(
            build_item_card(item, category["name"], deep=True),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=item_actions_keyboard(slug, item_id),
        )
        return

    if data.startswith("sell:"):
        _, slug, item_id = data.split(":", 2)
        category = get_category_by_slug(menu_data, slug)
        item = get_item_by_id(category, item_id) if category else None
        fields = build_learning_fields(item, category["name"])
        text = (
            f"*Как продавать: {item['name']}*\n\n"
            f"*Когда предлагать*\n{fields['guest_fit']}\n\n"
            f"*Как сказать гостю*\n"
            f"• Коротко: {fields['pitch']}\n"
            f"• Мягкая допродажа: могу также предложить {', '.join(fields['upsell']).lower()}.\n\n"
            f"*Чего не делать*\n"
            f"• не перечислять всё меню\n"
            f"• не читать сухо состав\n"
            f"• не давить на гостя"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=item_actions_keyboard(slug, item_id))
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
        kb.append([InlineKeyboardButton("⬅️ Назад к блюду", callback_data=f"deep:{slug}:{item_id}")])
        await query.edit_message_text(
            f"*Мини-вопрос*\nКакое блюдо подходит под описание:\n_{item.get('quiz_hint', 'Позиция из этой категории')}_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("microans:"):
        answer = data.split(":", 1)[1]
        right = context.user_data.get("microquiz_answer")
        category_name = context.user_data.get("microquiz_category")
        item_name = context.user_data.get("microquiz_item")
        ok = answer == right
        save_progress(update.effective_user.id, "microquiz", category_name, item_name, 1 if ok else 0, 1, "Мини-вопрос")
        await query.edit_message_text(
            f"{'✅ Верно' if ok else '❌ Неверно'}\nПравильный ответ: *{right}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if data.startswith("academy_quiz:"):
        slug = data.split(":", 1)[1]
        category = get_category_by_slug(menu_data, slug)
        questions = []
        items = category.get("items", [])[:]
        random.shuffle(items)
        for item in items[:7]:
            pool = [x["name"] for x in items if x["name"] != item["name"]]
            random.shuffle(pool)
            opts = [item["name"]] + pool[:3]
            random.shuffle(opts)
            questions.append({
                "category": category["name"],
                "item_name": item["name"],
                "question": f"Какое блюдо подходит под описание: {item.get('quiz_hint', f'Позиция из категории {category['name']}')}",
                "options": opts,
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
        await query.edit_message_text(
            f"{'✅ Верно' if ok else '❌ Неверно'}\nПравильный ответ: *{current['answer']}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        await send_next_question(update, context)
        return


async def cmd_sync_site(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    employee = get_employee(update.effective_user.id)
    if not employee or not employee["is_admin"]:
        await update.message.reply_text("Команда доступна администратору или директору.")
        return
    await update.message.reply_text("Запускаю синхронизацию с сайтом. Это может занять до минуты.")
    try:
        menu_data = await sync_menu_from_site(context)
        await update.message.reply_text("✅ Меню обновлено.\n" + site_sync_summary(menu_data))
    except Exception as e:
        await update.message.reply_text(f"Не удалось обновить меню с сайта.\nПричина: {e}")


async def cmd_menu_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    menu_data = context.bot_data["menu_data"]
    lines = ["*Статистика меню*\n", site_sync_summary(menu_data), ""]
    for cat in menu_data.get("categories", [])[:25]:
        lines.append(f"• {cat['name']}: {len(cat.get('items', []))} поз.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


def build_app() -> Application:
    app = Application.builder().token(get_env("TELEGRAM_BOT_TOKEN")).build()
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
    app.add_handler(CommandHandler("sync_site", cmd_sync_site))
    app.add_handler(CommandHandler("menu_stats", cmd_menu_stats))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def main() -> None:
    app = build_app()
    logger.info("Bot started")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
