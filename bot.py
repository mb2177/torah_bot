import html
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from torah_ru_loader import get_parsha_ru

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODELS_FAST", "gpt-4.1-mini")

SCHEDULE_TZ = os.getenv("SCHEDULE_TZ", "Asia/Dubai")
SCHEDULE_DAY_OF_WEEK = os.getenv("SCHEDULE_DAY_OF_WEEK", "sun")
SCHEDULE_HOUR = int(os.getenv("SCHEDULE_HOUR", "12"))
SCHEDULE_MINUTE = int(os.getenv("SCHEDULE_MINUTE", "20"))

ISRAEL = os.getenv("ISRAEL", "false").lower() == "true"
TORAH_LANG = os.getenv("TORAH_LANG", "en").lower()
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()

USERS_FILE = Path("users.json")
CACHE_FILE = Path("cache.json")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


def load_users() -> List[int]:
    if not USERS_FILE.exists():
        return []
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_users(users: List[int]) -> None:
    USERS_FILE.write_text(
        json.dumps(sorted(set(users)), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def register_user(user_id: int) -> None:
    users = load_users()
    if user_id not in users:
        users.append(user_id)
        save_users(users)


def is_allowed(user_id: int) -> bool:
    if not ADMIN_USER_ID:
        return True
    return str(user_id) == ADMIN_USER_ID


def load_cache() -> Dict[str, Any]:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: Dict[str, Any]) -> None:
    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clean_html_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(clean_html_text(x) for x in value if x)
    if value is None:
        return ""

    text = str(value)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, limit: int = 3800) -> List[str]:
    text = text.strip()

    if len(text) <= limit:
        return [text]

    chunks = []

    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        split_at = text.rfind("\n\n", 0, limit)

        if split_at == -1:
            split_at = text.rfind("\n", 0, limit)

        if split_at == -1:
            split_at = limit

        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()

    return chunks


async def send_long_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    reply_markup=None,
) -> None:
    chunks = chunk_text(text)

    for i, chunk in enumerate(chunks):
        await context.bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup if i == len(chunks) - 1 else None,
            disable_web_page_preview=True,
        )


async def get_current_parsha() -> Dict[str, Any]:
    params = {
        "cfg": "json",
        "geo": "none",
        "M": "on",
    }

    if ISRAEL:
        params["i"] = "on"

    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://www.hebcal.com/shabbat",
            params=params,
            timeout=30,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

    parsha_item = None

    for item in data.get("items", []):
        if item.get("category") == "parashat":
            parsha_item = item
            break

    if not parsha_item:
        raise RuntimeError("Hebcal did not return a parsha item.")

    title = parsha_item.get("title", "").replace("Parashat ", "").strip()
    leyning = parsha_item.get("leyning", {}) or {}

    return {
        "title": title,
        "hebrew": parsha_item.get("hebrew", ""),
        "torah_ref": leyning.get("torah", ""),
        "date": data.get("date", ""),
        "raw": parsha_item,
    }


async def fetch_sefaria_text(tref: str, lang: str = "en") -> str:
    if not tref:
        raise RuntimeError("Missing Sefaria reference.")

    url = f"https://www.sefaria.org/api/texts/{quote(tref)}"

    params = {
        "lang": lang,
        "commentary": "0",
        "context": "0",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=45) as resp:
            resp.raise_for_status()
            data = await resp.json()

    text = clean_html_text(data.get("he" if lang == "he" else "text", ""))

    if not text:
        if lang != "en":
            return await fetch_sefaria_text(tref, "en")
        raise RuntimeError(f"Sefaria returned empty text for {tref}")

    return text


async def get_parsha_package() -> Dict[str, str]:
    """
    1. Hebcal determines this week's parsha.
    2. Bot tries to load the Russian parsha text from torah_ru_parshiot.json.
    3. If unavailable, fallback to Sefaria.
    Supports double portions like Achrei Mot-Kedoshim.
    """
    cache = load_cache()
    parsha = await get_current_parsha()

    title = parsha["title"]
    cache_key = f"RU|{title}|{parsha.get('torah_ref', '')}|{ISRAEL}"

    if cache.get("key") == cache_key:
        return cache["data"]

    titles = [t.strip() for t in title.split("-") if t.strip()]
    found_parts = []

    for t in titles:
        item = get_parsha_ru(t)

        if not item:
            torah_ref = parsha.get("torah_ref") or f"Parashat {title}"
            full_text = await fetch_sefaria_text(torah_ref, TORAH_LANG)

            data = {
                "title": title,
                "title_ru": title,
                "hebrew": parsha.get("hebrew", ""),
                "torah_ref": torah_ref,
                "full_text": full_text,
            }

            save_cache({"key": cache_key, "data": data})
            return data

        found_parts.append(item)

    full_text = "\n\n".join(
        f"📖 {part['title_ru']}\n{part['reference_ru']}\n\n{part['text_ru']}"
        for part in found_parts
    )

    refs = " + ".join(part["reference_ru"] for part in found_parts)
    ru_titles = " + ".join(part["title_ru"] for part in found_parts)

    data = {
        "title": title,
        "title_ru": ru_titles,
        "hebrew": parsha.get("hebrew", ""),
        "torah_ref": refs,
        "full_text": full_text,
    }

    save_cache({"key": cache_key, "data": data})
    return data


async def fetch_rashi_commentary(torah_ref: str) -> str:
    tref = f"Rashi on {torah_ref}"

    try:
        return await fetch_sefaria_text(tref, "en")
    except Exception:
        return ""


SYSTEM_RU = """
Ты — преподаватель Торы. Твоя задача — помогать изучать недельную главу точно, уважительно и без выдумок.

Правила:
- Работай только с текстом, который тебе дали.
- Не добавляй события, которых нет в тексте.
- Не придумывай цитаты мудрецов.
- Если не уверен — пиши более общо.
- Пиши на русском языке.
- Используй еврейские термины: Моше, Аарон, Всевышний, Мишкан, Синай, Песах.
- Не используй христианские термины вроде "Пасха" или "Скиния".
- Стиль: простой, ясный, уважительный, без академического тона.
""".strip()


async def ask_ai(task: str, source_text: str, extra: str = "") -> str:
    """
    Uses chat.completions.
    Fixes error: 'AsyncOpenAI' object has no attribute 'responses'
    """
    max_chars = 52000
    clipped = source_text[:max_chars]

    prompt = f"""
ЗАДАНИЕ:
{task}

ДОПОЛНИТЕЛЬНЫЕ ПРАВИЛА:
{extra}

ТЕКСТ ДЛЯ РАБОТЫ:
{clipped}
""".strip()

    response = await openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_RU},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )

    return response.choices[0].message.content.strip()


async def generate_summary(full_text: str) -> str:
    return await ask_ai(
        task="Сделай краткий, но точный пересказ недельной главы Торы.",
        source_text=full_text,
        extra="""
- Не пропускай главные события.
- Не добавляй объяснения и мидраши.
- Объём: 8-12 коротких предложений.
- Пиши так, чтобы это было удобно читать в Telegram.
""",
    )


async def generate_lesson(full_text: str) -> str:
    return await ask_ai(
        task="Объясни смысл и главный урок этой недельной главы.",
        source_text=full_text,
        extra="""
- Сначала назови главную духовную идею главы.
- Потом объясни её простым языком.
- Заверши 2-3 предложениями о том, как это применить в жизни.
- Без морализаторства и без пугающих формулировок.
""",
    )


async def generate_questions(full_text: str) -> str:
    return await ask_ai(
        task="Составь вопросы для размышления по недельной главе.",
        source_text=full_text,
        extra="""
- Дай 5-7 вопросов.
- Вопросы должны быть глубокими, но понятными.
- Вопросы должны опираться только на текст главы.
- Не добавляй ответы.
""",
    )


async def generate_rashi_explanation(full_text: str, rashi_text: str) -> str:
    if not rashi_text.strip():
        return (
            "📚 Комментарий Раши\n\n"
            "Я не смог получить комментарий Раши из источника для этой главы. "
            "Лучше не генерировать его из памяти, чтобы не приписать Раши то, чего он не говорил."
        )

    combined = f"""
ТЕКСТ ГЛАВЫ:
{full_text[:28000]}

КОММЕНТАРИИ РАШИ:
{rashi_text[:28000]}
""".strip()

    return await ask_ai(
        task="Выбери 2-3 важных комментария Раши к этой главе и объясни их простым языком.",
        source_text=combined,
        extra="""
- Не придумывай цитаты Раши.
- Используй только предоставленный текст комментариев.
- Не перегружай деталями.
- Формат: короткий заголовок и объяснение простым языком.
""",
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📜 Полная глава", callback_data="full")],
            [
                InlineKeyboardButton("✂️ Кратко", callback_data="summary"),
                InlineKeyboardButton("💡 Смысл и урок", callback_data="lesson"),
            ],
            [
                InlineKeyboardButton("📚 Раши", callback_data="rashi"),
                InlineKeyboardButton("❓ Вопросы", callback_data="questions"),
            ],
        ]
    )


async def build_intro_message() -> str:
    data = await get_parsha_package()

    title = html.escape(data.get("title_ru") or data["title"])
    hebrew = html.escape(data.get("hebrew", ""))
    ref = html.escape(data.get("torah_ref", ""))

    hebrew_line = f"\n{hebrew}" if hebrew else ""
    ref_line = f"\n\nИсточник: {ref}" if ref else ""

    return (
        f"📖 <b>Недельная глава: {title}</b>{hebrew_line}\n\n"
        f"Выбери, что открыть:{ref_line}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not is_allowed(user_id):
        await update.message.reply_text("У тебя нет доступа к этому боту.")
        return

    register_user(user_id)

    await update.message.reply_text(
        "Готово. Я буду присылать недельную главу по воскресеньям.\n\n"
        "Для теста нажми /send_now"
    )


async def send_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not is_allowed(user_id):
        await update.message.reply_text("У тебя нет доступа к этому боту.")
        return

    register_user(user_id)

    msg = await build_intro_message()

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id

    if not is_allowed(user_id):
        await query.answer("Нет доступа.", show_alert=True)
        return

    await query.answer("Готовлю...")

    data = await get_parsha_package()
    action = query.data

    try:
        if action == "full":
            text = (
                f"📜 <b>Полная глава: "
                f"{html.escape(data.get('title_ru') or data['title'])}</b>\n\n"
                f"{html.escape(data['full_text'])}"
            )
            await send_long_message(context, query.message.chat_id, text)

        elif action == "summary":
            result = await generate_summary(data["full_text"])
            await send_long_message(
                context,
                query.message.chat_id,
                f"✂️ <b>Кратко</b>\n\n{html.escape(result)}",
            )

        elif action == "lesson":
            result = await generate_lesson(data["full_text"])
            await send_long_message(
                context,
                query.message.chat_id,
                f"💡 <b>Смысл и урок</b>\n\n{html.escape(result)}",
            )

        elif action == "questions":
            result = await generate_questions(data["full_text"])
            await send_long_message(
                context,
                query.message.chat_id,
                f"❓ <b>Вопросы</b>\n\n{html.escape(result)}",
            )

        elif action == "rashi":
            rashi = await fetch_rashi_commentary(data["torah_ref"])
            result = await generate_rashi_explanation(data["full_text"], rashi)
            await send_long_message(
                context,
                query.message.chat_id,
                html.escape(result),
            )

        else:
            await query.message.reply_text("Неизвестная кнопка.")

    except Exception as e:
        await query.message.reply_text(
            f"Ошибка при подготовке текста: {html.escape(str(e))}",
            parse_mode=ParseMode.HTML,
        )


async def weekly_broadcast(app: Application) -> None:
    users = load_users()

    if not users:
        return

    try:
        msg = await build_intro_message()
    except Exception as e:
        msg = f"Не смог получить недельную главу: {html.escape(str(e))}"

    for user_id in users:
        try:
            await app.bot.send_message(
                chat_id=user_id,
                text=msg,
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
                disable_web_page_preview=True,
            )
        except Exception:
            pass


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN")

    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("send_now", send_now))
    app.add_handler(CallbackQueryHandler(button_handler))

    scheduler = AsyncIOScheduler(timezone=SCHEDULE_TZ)

    scheduler.add_job(
        weekly_broadcast,
        trigger="cron",
        day_of_week=SCHEDULE_DAY_OF_WEEK,
        hour=SCHEDULE_HOUR,
        minute=SCHEDULE_MINUTE,
        args=[app],
        id="weekly_parsha_broadcast",
        replace_existing=True,
    )

    scheduler.start()

    print(
        f"TorahBot started. Weekly schedule: "
        f"{SCHEDULE_DAY_OF_WEEK} {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} "
        f"{SCHEDULE_TZ}"
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
