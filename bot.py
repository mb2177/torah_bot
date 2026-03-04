import os
import asyncio
import logging
from datetime import date, timedelta
from typing import Optional, List

import httpx
from zoneinfo import ZoneInfo

from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from openai import AsyncOpenAI

# ---------------- ЛОГИ ----------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("torah_bot")

# ---------------- ENV ----------------

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# geonameid по умолчанию: Dubai (Diaspora)
HEBCAL_GEONAMEID = int(os.getenv("HEBCAL_GEONAMEID", "292223"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ---------------- PROMPTS ----------------

SYSTEM_PROMPT_RABBI = """
Ты раввин и преподаватель Торы.

Тебе передают STRUCTURE — список событий главы в правильном порядке.
Ты НЕ имеешь права добавлять события вне STRUCTURE.
Ты НЕ имеешь права менять порядок.
Ты НЕ имеешь права добавлять диалоги.
Ты НЕ имеешь права цитировать Тору дословно.
Ты НЕ имеешь права писать галаху и практические предписания.
Ты НЕ имеешь права использовать каббалу и спорные мнения.
Ты НЕ имеешь права давить морально.

Пиши только по-русски.
Пиши уверенно, структурно, понятно.

Формат ответа:
1) Что произошло на этой неделе (главная часть, по пунктам, строго по STRUCTURE)
2) Что в этом обычно видят комментаторы (мягко, без терминов)
3) Короткая современная мысль (тепло, без морали)
"""

SYSTEM_PROMPT_EXTRACT = """
Ты выделяешь ТОЛЬКО структуру событий главы из исходного текста.
Запреты:
- не добавляй ничего от себя
- не делай комментариев
- не цитируй дословно длинные куски
Выход: список пунктов (10-25 пунктов), в правильном порядке.
"""

# ---------------- UX: typing ----------------

async def send_typing(chat, duration_seconds: int = 20):
    for _ in range(duration_seconds * 2):  # раз в 0.5 сек
        try:
            await chat.send_chat_action("typing")
        except Exception:
            pass
        await asyncio.sleep(0.5)

# ---------------- Telegram limit split ----------------

def split_text(text: str, limit: int = 4000) -> List[str]:
    text = (text or "").strip()
    if not text:
        return ["(пустой ответ)"]

    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = text.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    parts.append(text)
    return parts

# ---------------- Hebcal: текущая парша (Diaspora) ----------------

async def get_current_parsha_diaspora() -> Optional[str]:
    """
    1) Пытаемся через Shabbat API (лучше всего для parashat).
    2) Если нет parashat (иногда праздник) — fallback на Calendar API и ищем ближайшую parashat в 21 день.
    """

    # 1) Shabbat Times REST API
    shabbat_url = "https://www.hebcal.com/shabbat"
    shabbat_params = {
        "cfg": "json",
        "geo": "geoname",
        "geonameid": HEBCAL_GEONAMEID,
        "m": "50",
        "leyning": "on",
        # Diaspora: i=on НЕ ставим
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client_http:
            r = await client_http.get(shabbat_url, params=shabbat_params)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.warning(f"Hebcal shabbat API failed: {e}")
        data = {}

    for item in data.get("items", []):
        if item.get("category") == "parashat" and item.get("title"):
            title = item["title"]
            return title.replace("Parashat ", "").strip()

    # 2) Fallback: Calendar API (ищем ближайшую parashat в диапазоне)
    cal_url = "https://www.hebcal.com/hebcal"
    today = date.today()
    end = today + timedelta(days=21)

    cal_params = {
        "v": "1",
        "cfg": "json",
        "start": today.isoformat(),
        "end": end.isoformat(),
        "ss": "on",
        "maj": "off",
        "min": "off",
        "mod": "off",
        "nx": "off",
        # Diaspora: i=on НЕ ставим
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client_http:
            r2 = await client_http.get(cal_url, params=cal_params)
            r2.raise_for_status()
            data2 = r2.json()
    except Exception as e:
        logger.warning(f"Hebcal calendar API failed: {e}")
        return None

    for item in data2.get("items", []):
        if item.get("category") == "parashat" and item.get("title"):
            title = item["title"]
            return title.replace("Parashat ", "").strip()

    return None

# ---------------- Sefaria: получить текст главы ----------------

async def get_parsha_text_sefaria(parsha_name: str) -> str:
    """
    Берём текст из Sefaria (иврит), только как сырьё для извлечения структуры.
    В чат это не отправляем.
    """
    # Обычно Sefaria понимает такие refs: "Torah, Vayishlach"
    # Иногда могут быть нюансы с написанием. Если что — добавим fallback.
    ref = f"Torah, {parsha_name}"
    url = f"https://www.sefaria.org/api/texts/{ref}"
    params = {
        "lang": "he",
        "context": "0",
        "commentary": "0",
    }

    async with httpx.AsyncClient(timeout=30) as client_http:
        r = await client_http.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    # Sefaria возвращает массивы секций/стихов — склеиваем в одну строку, ограничиваем размер
    chunks: List[str] = []
    txt = data.get("text")
    if isinstance(txt, list):
        for section in txt:
            if isinstance(section, list):
                chunks.append(" ".join([str(x) for x in section if x]))
            elif section:
                chunks.append(str(section))
    joined = " ".join(chunks).strip()
    return joined[:20000]  # ограничим, чтобы не раздувать контекст

# ---------------- OpenAI: шаг 1 (структура) ----------------

async def extract_structure_from_text(parsha_text: str) -> str:
    prompt = f"""
Ниже дан текст главы (сырьё). Сделай список событий в правильном порядке.
Только события. Без объяснений. Без цитат.

Текст:
{parsha_text}
"""

    # Быстрый/дешёвый режим для структуры
    resp = await openai_client.chat.completions.create(
        model="gpt-5.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_EXTRACT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        timeout=25,
    )
    return resp.choices[0].message.content.strip()

# ---------------- OpenAI: шаг 2 (форматирование "как раввин") ----------------

async def format_as_rabbi(structure_text: str) -> str:
    prompt = f"STRUCTURE:\n{structure_text}\n"

    # Основной текст: тоже на gpt-5.1-mini
    # Можно добавить fallback при желании, но сначала проверим качество/стабильность.
    resp = await openai_client.chat.completions.create(
        model="gpt-5.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_RABBI},
            {"role": "user", "content": prompt},
        ],
        temperature=0.6,
        timeout=30,
    )
    return resp.choices[0].message.content.strip()

# ---------------- Команды Telegram ----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Шалом.\n\n"
        "Я объясняю недельную главу Торы по-русски, структурно и точно.\n"
        "Без цитат, без галахи, без фантазий.\n\n"
        "Нажми /parsha — и я пришлю объяснение главы этой недели."
    )
    await update.message.reply_text(text)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/parsha — объяснение текущей недельной главы\n"
        "/start — приветствие\n"
        "/help — помощь\n"
    )

async def cmd_parsha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    typing_task = asyncio.create_task(send_typing(chat, duration_seconds=25))

    try:
        parsha_name = await get_current_parsha_diaspora()
        if not parsha_name:
            typing_task.cancel()
            await chat.send_message(
                "Не удалось определить текущую недельную главу.\n"
                "Попробуй ещё раз через минуту."
            )
            return

        # 1) берём текст из Sefaria (сырьё)
        parsha_text = await get_parsha_text_sefaria(parsha_name)

        # 2) извлекаем структуру событий
        structure = await extract_structure_from_text(parsha_text)

        # 3) оформляем как раввин
        final_text = await format_as_rabbi(structure)

        typing_task.cancel()

        header = f"📖 Недельная глава: {parsha_name}\n"
        for idx, part in enumerate(split_text(final_text)):
            if idx == 0:
                await chat.send_message(header + "\n" + part)
            else:
                await chat.send_message(part)

    except Exception as e:
        typing_task.cancel()
        logger.exception(f"/parsha error: {e}")
        await chat.send_message("Техническая ошибка. Попробуй чуть позже.")

# ---------------- post_init: меню команд ----------------

async def post_init(app):
    commands = [
        BotCommand("start", "Начать"),
        BotCommand("parsha", "Текущая глава"),
        BotCommand("help", "Помощь"),
    ]
    await app.bot.set_my_commands(commands)
    logger.info("Commands menu set")

# ---------------- MAIN ----------------

def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("parsha", cmd_parsha))

    logger.info("Bot polling started")
    app.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=1.0)

if __name__ == "__main__":
    main()
