import os
import asyncio
import logging
from datetime import date, timedelta
from typing import Optional, List, Dict, Any
from urllib.parse import quote

import httpx
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

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
HEBCAL_GEONAMEID = int(os.getenv("HEBCAL_GEONAMEID", "292223"))  # Dubai default

# Можно переопределить списки моделей через переменные:
# OPENAI_MODELS_FAST="gpt-5-mini,gpt-5,gpt-4.1-mini"
OPENAI_MODELS_FAST = os.getenv("OPENAI_MODELS_FAST", "gpt-5-mini,gpt-5,gpt-4.1-mini")
MODEL_CHAIN = [m.strip() for m in OPENAI_MODELS_FAST.split(",") if m.strip()]

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

Если в STRUCTURE чего-то нет — НЕ добавляй.
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

async def send_typing(chat, duration_seconds: int = 30):
    for _ in range(duration_seconds * 2):
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

# ---------------- Debug storage ----------------

LAST_ERROR_BY_USER: Dict[int, str] = {}

def set_last_error(user_id: int, msg: str):
    LAST_ERROR_BY_USER[user_id] = msg[:3500]

def get_last_error(user_id: int) -> str:
    return LAST_ERROR_BY_USER.get(user_id, "Нет сохранённой ошибки. Всё ок или бот ещё не падал 🙂")

# ---------------- HTTP helpers ----------------

async def http_get_json(url: str, params: Optional[dict] = None, timeout: int = 25) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        return r.json()

# ---------------- Hebcal: текущая парша (Diaspora) ----------------

async def get_current_parsha_diaspora() -> Optional[str]:
    shabbat_url = "https://www.hebcal.com/shabbat"
    shabbat_params = {
        "cfg": "json",
        "geo": "geoname",
        "geonameid": HEBCAL_GEONAMEID,
        "m": "50",
        "leyning": "on",
    }

    try:
        data = await http_get_json(shabbat_url, params=shabbat_params, timeout=20)
    except Exception as e:
        logger.warning(f"Hebcal shabbat API failed: {e}")
        data = {}

    for item in data.get("items", []):
        if item.get("category") == "parashat" and item.get("title"):
            return item["title"].replace("Parashat ", "").strip()

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
    }

    try:
        data2 = await http_get_json(cal_url, params=cal_params, timeout=20)
    except Exception as e:
        logger.warning(f"Hebcal calendar API failed: {e}")
        return None

    for item in data2.get("items", []):
        if item.get("category") == "parashat" and item.get("title"):
            return item["title"].replace("Parashat ", "").strip()

    return None

# ---------------- Sefaria: robust fetch ----------------

async def sefaria_get_text_by_ref(ref: str) -> str:
    encoded_ref = quote(ref, safe="")
    url = f"https://www.sefaria.org/api/texts/{encoded_ref}"
    params = {"lang": "he", "context": "0", "commentary": "0"}

    data = await http_get_json(url, params=params, timeout=30)

    chunks: List[str] = []
    txt = data.get("text")
    if isinstance(txt, list):
        for section in txt:
            if isinstance(section, list):
                chunks.append(" ".join([str(x) for x in section if x]))
            elif section:
                chunks.append(str(section))

    joined = " ".join(chunks).strip()
    if not joined:
        raise RuntimeError(f"Sefaria empty text for ref='{ref}'")
    return joined[:20000]

async def sefaria_try_parsha_text(parsha_name: str) -> str:
    candidates = [
        f"Torah, {parsha_name}",
        parsha_name,
        f"Parashat {parsha_name}",
    ]
    last_err = None
    for ref in candidates:
        try:
            return await sefaria_get_text_by_ref(ref)
        except Exception as e:
            last_err = e
            logger.warning(f"Sefaria ref failed: {ref} -> {e}")

    # calendars fallback (diaspora=1)
    try:
        cal = await http_get_json("https://www.sefaria.org/api/calendars", params={"diaspora": "1"}, timeout=25)
        items = cal.get("calendar_items") or cal.get("items") or []
        ref_from_calendar = None
        for it in items:
            title = (it.get("title") or it.get("displayValue") or "").lower()
            if "parash" in title or "parsha" in title or "hashavua" in title:
                ref_from_calendar = it.get("ref") or it.get("displayRef") or it.get("anchorRef")
                if ref_from_calendar:
                    break
        if ref_from_calendar:
            return await sefaria_get_text_by_ref(ref_from_calendar)
        raise RuntimeError("Sefaria calendars: parasha ref not found")
    except Exception as e:
        raise RuntimeError(f"Sefaria failed for '{parsha_name}'. Last: {last_err}") from e

# ---------------- OpenAI: helper with model fallback ----------------

async def openai_chat_with_fallback(
    system_prompt: str,
    user_prompt: str,
    temperature: float,
    timeout_s: int,
) -> str:
    last_err = None
    for model in MODEL_CHAIN:
        try:
            resp = await openai_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                timeout=timeout_s,
            )
            out = resp.choices[0].message.content.strip()
            if not out:
                raise RuntimeError(f"Empty response from model {model}")
            logger.info(f"OpenAI used model: {model}")
            return out
        except Exception as e:
            last_err = e
            logger.warning(f"OpenAI model failed {model}: {e}")
            continue
    raise RuntimeError(f"All OpenAI models failed. Last error: {last_err}")

# ---------------- OpenAI: шаг 1 (структура) ----------------

async def extract_structure_from_text(parsha_text: str) -> str:
    prompt = f"""
Ниже дан текст главы (сырьё). Сделай список событий в правильном порядке.
Только события. Без объяснений. Без цитат.

Текст:
{parsha_text}
"""
    return await openai_chat_with_fallback(
        system_prompt=SYSTEM_PROMPT_EXTRACT,
        user_prompt=prompt,
        temperature=0.1,
        timeout_s=25,
    )

# ---------------- OpenAI: шаг 2 (раввин) ----------------

async def format_as_rabbi(structure_text: str) -> str:
    prompt = f"STRUCTURE:\n{structure_text}\n"
    return await openai_chat_with_fallback(
        system_prompt=SYSTEM_PROMPT_RABBI,
        user_prompt=prompt,
        temperature=0.6,
        timeout_s=30,
    )

# ---------------- Команды ----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Шалом.\n\n"
        "Я объясняю недельную главу Торы по-русски, структурно и точно.\n"
        "Без цитат, без галахи, без фантазий.\n\n"
        "Нажми /parsha — и я пришлю объяснение главы этой недели.\n"
        "Если что-то сломалось — /debug покажет последнюю ошибку."
    )
    await update.message.reply_text(text)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/parsha — объяснение текущей недельной главы\n"
        "/debug — показать последнюю ошибку\n"
        "/start — приветствие\n"
        "/help — помощь\n"
    )

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(get_last_error(user_id))

async def cmd_parsha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat = update.effective_chat
    typing_task = asyncio.create_task(send_typing(chat, duration_seconds=40))

    try:
        set_last_error(user_id, f"OK: started /parsha. MODEL_CHAIN={MODEL_CHAIN}")

        # 1) Hebcal
        parsha_name = await get_current_parsha_diaspora()
        if not parsha_name:
            typing_task.cancel()
            set_last_error(user_id, "Hebcal: не удалось определить parasha (нет category=parashat).")
            await chat.send_message("Не удалось определить текущую недельную главу. Попробуй ещё раз через минуту.")
            return

        # 2) Sefaria
        parsha_text = await sefaria_try_parsha_text(parsha_name)

        # 3) OpenAI step 1
        structure = await extract_structure_from_text(parsha_text)

        # 4) OpenAI step 2
        final_text = await format_as_rabbi(structure)

        typing_task.cancel()
        set_last_error(user_id, "OK: success")

        header = f"📖 Недельная глава: {parsha_name}\n"
        parts = split_text(final_text)
        for idx, part in enumerate(parts):
            if idx == 0:
                await chat.send_message(header + "\n" + part)
            else:
                await chat.send_message(part)

    except Exception as e:
        typing_task.cancel()
        msg = f"ERROR: {repr(e)}"
        set_last_error(user_id, msg)
        logger.exception(msg)
        await chat.send_message("Техническая ошибка. Напиши /debug — покажу подробности.")

# ---------------- post_init: меню команд ----------------

async def post_init(app):
    commands = [
        BotCommand("start", "Начать"),
        BotCommand("parsha", "Текущая глава"),
        BotCommand("debug", "Показать последнюю ошибку"),
        BotCommand("help", "Помощь"),
    ]
    await app.bot.set_my_commands(commands)

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
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("parsha", cmd_parsha))

    logger.info(f"Bot started. MODEL_CHAIN={MODEL_CHAIN}")
    app.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=1.0)

if __name__ == "__main__":
    main()
