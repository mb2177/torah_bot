import os
import asyncio
import logging
import httpx
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from openai import AsyncOpenAI

# ---------------- ЛОГИ ----------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- НАСТРОЙКИ ----------------

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not set")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY not set")

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ---------------- SYSTEM PROMPT ----------------

SYSTEM_PROMPT = """
Ты раввин и преподаватель Торы.

Ты получаешь STRUCTURE — это список событий главы в правильном порядке.
Ты НЕ имеешь права добавлять события вне STRUCTURE.
Ты НЕ имеешь права менять порядок.
Ты НЕ имеешь права добавлять диалоги.
Ты НЕ имеешь права цитировать Тору дословно.
Ты НЕ имеешь права писать галаху.
Ты НЕ имеешь права использовать каббалу.
Ты НЕ имеешь права давить морально.

Формат ответа:
1) Структурный пересказ событий главы (по пунктам, строго по STRUCTURE)
2) Мягкое объяснение того, что обычно в этом видят комментаторы
3) Короткая современная мысль

Пиши только по-русски.
Пиши ясно, структурно, уверенно, как раввин.
"""

# ---------------- HELPER: typing ----------------

async def send_typing(chat, duration=15):
    for _ in range(duration * 2):
        try:
            await chat.send_chat_action("typing")
        except:
            pass
        await asyncio.sleep(0.5)

# ---------------- HEBcal: текущая парша (Diaspora) ----------------

async def get_current_parsha():
    url = "https://www.hebcal.com/hebcal"
    params = {
        "v": "1",
        "cfg": "json",
        "maj": "on",
        "min": "on",
        "mod": "on",
        "nx": "on",
        "year": "now",
        "month": "x",
        "ss": "on",
        # НЕТ i=on → значит Diaspora
    }

    async with httpx.AsyncClient() as client_http:
        r = await client_http.get(url, params=params)
        data = r.json()

    for item in data.get("items", []):
        if item.get("category") == "parashat":
            return item.get("title")

    return None

# ---------------- Sefaria: получить текст главы ----------------

async def get_parsha_text(parsha_name):
    ref = f"Torah, {parsha_name}"
    url = f"https://www.sefaria.org/api/texts/{ref}?lang=he&context=0"

    async with httpx.AsyncClient(timeout=30) as client_http:
        r = await client_http.get(url)
        data = r.json()

    # Возвращаем весь текст без форматирования
    text = ""
    if isinstance(data.get("text"), list):
        for section in data["text"]:
            if isinstance(section, list):
                text += " ".join(section) + " "
            else:
                text += str(section) + " "
    return text[:15000]  # ограничиваем размер

# ---------------- ШАГ 1: извлечь структуру ----------------

async def extract_structure(parsha_text):
    prompt = f"""
На основе следующего текста главы Торы выдели ТОЛЬКО структуру событий.
Сделай список пунктов в правильном порядке.
Без объяснений. Только события.

Текст:
{parsha_text}
"""

    response = await client.chat.completions.create(
        model="gpt-5.1-mini",
        messages=[
            {"role": "system", "content": "Ты выделяешь только структуру событий."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )

    return response.choices[0].message.content

# ---------------- ШАГ 2: форматирование как раввин ----------------

async def format_as_rabbi(structure_text):
    prompt = f"""
STRUCTURE:
{structure_text}
"""

    response = await client.chat.completions.create(
        model="gpt-5.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.6,
    )

    return response.choices[0].message.content

# ---------------- Telegram limit split ----------------

def split_text(text, limit=4000):
    parts = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        parts.append(text[:split_at])
        text = text[split_at:]
    parts.append(text)
    return parts

# ---------------- /start ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Я объясняю недельную главу Торы структурно и точно.\n\n"
        "Используй команду /parsha чтобы получить объяснение текущей главы."
    )
    await update.message.reply_text(text)

# ---------------- /parsha ----------------

async def parsha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    typing_task = asyncio.create_task(send_typing(chat))

    try:
        parsha_name = await get_current_parsha()
        if not parsha_name:
            await chat.send_message("Не удалось определить текущую главу.")
            return

        parsha_text = await get_parsha_text(parsha_name)
        structure = await extract_structure(parsha_text)
        final_text = await format_as_rabbi(structure)

        typing_task.cancel()

        for part in split_text(final_text):
            await chat.send_message(part)

    except Exception as e:
        typing_task.cancel()
        logger.exception(e)
        await chat.send_message("Произошла ошибка. Попробуй позже.")

# ---------------- /help ----------------

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/parsha — объяснение текущей главы\n"
        "/start — приветствие"
    )

# ---------------- MAIN ----------------

async def post_init(app):
    commands = [
        BotCommand("start", "Начать"),
        BotCommand("parsha", "Текущая глава"),
        BotCommand("help", "Помощь"),
    ]
    await app.bot.set_my_commands(commands)

def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("parsha", parsha))
    app.add_handler(CommandHandler("help", help_command))

    app.run_polling()

if __name__ == "__main__":
    main()
