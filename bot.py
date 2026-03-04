import os
import re
import json
import asyncio
import logging
from datetime import date, timedelta
from typing import Optional, List, Dict, Any
from urllib.parse import quote

import httpx
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from openai import AsyncOpenAI

# ---------------- LOGS ----------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("torah_bot")

# ---------------- ENV ----------------

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Diaspora location (default Dubai)
HEBCAL_GEONAMEID = int(os.getenv("HEBCAL_GEONAMEID", "292223"))

# Models fallback chain
# Set in Railway:
# OPENAI_MODELS="gpt-5-mini,gpt-5,gpt-4.1-mini"
OPENAI_MODELS = os.getenv("OPENAI_MODELS", "gpt-5-mini,gpt-5,gpt-4.1-mini")
MODEL_CHAIN = [m.strip() for m in OPENAI_MODELS.split(",") if m.strip()]

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ---------------- YOUR SYSTEM PROMPT ----------------

SYSTEM_PROMPT_MAIN = """
Ты - преподаватель Торы и пишешь для Telegram-бота краткое и максимально точное объяснение недельной главы Торы.

Цель: дать читателю, даже без религиозного образования, понятный и уважительный пересказ главы. Текст должен быть точным, без выдумок и легко читаться.

ОЧЕНЬ ВАЖНО:
- Описывай только события, которые действительно происходят в этой недельной главе.
- Не добавляй мидраши, каббалу, талмудические обсуждения или современные интерпретации.
- Если есть сомнение в какой-то детали - не добавляй её, а опиши событие более общими словами.

------------------------------------------------

ШАГ 1. ВНУТРЕННЯЯ САМОПРОВЕРКА (НЕ ПОКАЗЫВАЙ ЧИТАТЕЛЮ)

Перед написанием текста сначала мысленно составь краткий план главы:

1. Перечисли для себя 8-12 ключевых событий или заповедей этой главы.
2. Убедись, что они действительно относятся именно к этой главе, а не к соседним.
3. Проверь, что не перепутаны важные понятия (например: Шатёр встречи ≠ Мишкан, Скрижали ≠ Ковчег).
4. Убедись, что один из центральных духовных моментов главы будет отражён в тексте.

Этот план нужен только для проверки. НЕ выводи его в ответ.

------------------------------------------------

ПРАВИЛА НАПИСАНИЯ ТЕКСТА

1. Пиши простым и естественным языком, как будто спокойно объясняешь другу.
2. Используй короткие предложения и небольшие абзацы, чтобы текст было легко читать в Telegram.
3. Используй традиционные еврейские имена:
   Моше, Аарон, Всевышний, Мишкан, Синай, левиты и т.д.
4. Избегай тяжёлых или церковных выражений:
   не пиши «божественная кара», «беззаконие», «курительная смесь».
   Пиши проще: «народ согрешил», «народ был наказан», «священные благовония».
5. Описывай Всевышнего уважительно и без слишком человеческих выражений.
6. Сохраняй порядок событий так, как они происходят в Торе.
7. Упоминай главные события главы, а не только второстепенные.

------------------------------------------------

СТРУКТУРА ТЕКСТА

Начни так:

📖 Недельная глава: [название главы]

Далее:

1. Коротко и ясно расскажи, что происходит в этой главе.
2. Объясни главный смысл или духовную идею главы.
3. Заверши короткой жизненной мыслью (2-4 предложения), чему эта глава может научить человека сегодня.

Текст должен читаться примерно за 45-90 секунд.

------------------------------------------------

ШАГ 2. ПРОВЕРКА ПЕРЕД ОТПРАВКОЙ (НЕ ПОКАЗЫВАЙ ЧИТАТЕЛЮ)

Перед тем как выдать финальный текст, проверь:

1. Все ли основные события главы упомянуты?
2. Нет ли событий из других глав?
3. Не перепутаны ли названия предметов или мест?
4. Текст легко ли читается и понятен ли человеку без религиозного образования?

Если есть сомнения - упрости текст и убери спорные детали.

------------------------------------------------

Выведи только готовый текст поста без объяснений и без внутренних проверок.
""".strip()

# ---------------- INTERNAL STEP 1 (events list) ----------------

SYSTEM_PROMPT_EXTRACT = """
Ты извлекаешь 8-12 ключевых событий/заповедей недельной главы из данного текста (сырьё).
Только события/заповеди. Строго в порядке, как в тексте.
Без объяснений, без выводов, без новых деталей.
Если в детали не уверен - пиши более общо.

Формат вывода: 8-12 строк, каждая строка - одно событие. Без нумерации.
""".strip()

# ---------------- DEBUG STORAGE ----------------

LAST_ERROR_BY_USER: Dict[int, str] = {}

def set_last_error(user_id: int, msg: str):
    LAST_ERROR_BY_USER[user_id] = msg[:3500]

def get_last_error(user_id: int) -> str:
    return LAST_ERROR_BY_USER.get(user_id, "Нет сохранённой ошибки.")

# ---------------- UX: typing ----------------

async def send_typing(chat, duration_seconds: int = 45):
    for _ in range(duration_seconds * 2):
        try:
            await chat.send_chat_action("typing")
        except Exception:
            pass
        await asyncio.sleep(0.5)

# ---------------- TEXT SPLIT ----------------

def split_text(text: str, limit: int = 3800) -> List[str]:
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

# ---------------- HTTP HELPERS ----------------

async def http_get_json(url: str, params: Optional[dict] = None, timeout: int = 25) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        return r.json()

# ---------------- HEBCAL (DIASPORA) ----------------

async def get_current_parsha_diaspora() -> Optional[str]:
    # 1) Shabbat API with geonameid (respects Diaspora schedule for location)
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

    # 2) Fallback: calendar for next 21 days
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

# ---------------- SEFARIA (RAW TEXT) ----------------

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
        raise RuntimeError(f"Sefaria returned empty text for ref='{ref}'")
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

    raise RuntimeError(f"Sefaria failed for '{parsha_name}'. Last: {last_err}")

# ---------------- OPENAI WITH FALLBACK ----------------

async def openai_chat_with_fallback(system_prompt: str, user_prompt: str, temperature: float, timeout_s: int) -> str:
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
            out = (resp.choices[0].message.content or "").strip()
            if not out:
                raise RuntimeError(f"Empty response from model {model}")
            logger.info(f"OpenAI used model: {model}")
            return out
        except Exception as e:
            last_err = e
            logger.warning(f"OpenAI failed for {model}: {e}")
            continue
    raise RuntimeError(f"All OpenAI models failed. Last error: {last_err}")

# ---------------- STEP 1: KEY EVENTS (INTERNAL) ----------------

async def extract_key_events(parsha_text: str) -> str:
    prompt = f"""
Текст главы (сырьё):
{parsha_text}

Сделай 8-12 ключевых событий/заповедей в правильном порядке.
Только события. Без объяснений. Без нумерации.
"""
    return await openai_chat_with_fallback(
        system_prompt=SYSTEM_PROMPT_EXTRACT,
        user_prompt=prompt,
        temperature=0.1,
        timeout_s=40,
    )

# ---------------- STEP 2: FINAL POST ----------------

async def generate_post(parsha_name: str, key_events: str, parsha_text: str) -> str:
    # Передаем и "опору" (key_events), и сырьё (чтобы точнее, но без выдумок)
    prompt = f"""
Название недельной главы: {parsha_name}

Опорные ключевые события (только как опора, порядок важен):
{key_events}

Текст главы (сырьё, для проверки фактов, без цитирования):
{parsha_text}

Напиши финальный пост строго по правилам.
"""
    return await openai_chat_with_fallback(
        system_prompt=SYSTEM_PROMPT_MAIN,
        user_prompt=prompt,
        temperature=0.45,
        timeout_s=55,
    )

# ---------------- VALIDATION + REWRITE ----------------

BANNED_VISIBLE = [
    "самопроверка", "шаг 1", "шаг 2", "план", "чеклист",
    "structure", "по структуре", "по пунктам", "по списку",
]
BANNED_WRONG_NAMES = ["моисей", "господь", "библия", "табернакль"]
BANNED_HUMANIZING = ["разоз", "передумал", "обид", "в ярости", "взбес", "расстроил"]
BANNED_HEAVY = ["божественная кара", "беззаконие", "курительная смесь"]

def estimate_read_seconds(text: str) -> int:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return 0
    return max(10, int(len(t) / 14))

def validate_post(text: str, parsha_name: str) -> List[str]:
    issues = []
    low = (text or "").lower().strip()

    if not low.startswith("📖 недельная глава:"):
        issues.append("Нет заголовка '📖 Недельная глава: ...' в начале.")

    if parsha_name.lower() not in low:
        issues.append("В тексте не видно названия главы.")

    for w in BANNED_VISIBLE:
        if w in low:
            issues.append(f"Запрещенное слово/фраза: {w}")

    for w in BANNED_WRONG_NAMES:
        if w in low:
            issues.append(f"Нежелательная лексика (заменить): {w}")

    for w in BANNED_HUMANIZING:
        if w in low:
            issues.append("Слишком человеческое описание Всевышнего (убрать/переписать).")
            break

    for w in BANNED_HEAVY:
        if w in low:
            issues.append(f"Тяжелая формулировка (упростить): {w}")

    if "всевышн" not in low:
        issues.append("Не использовано слово 'Всевышний' (лучше использовать).")

    # Telegram length target 45-90 sec
    sec = estimate_read_seconds(text)
    if sec > 110:
        issues.append(f"Слишком длинно (оценка {sec} сек). Сократить.")
    if sec < 30:
        issues.append(f"Слишком коротко (оценка {sec} сек). Чуть добавить связности, без новых деталей.")

    return issues

async def rewrite_post(parsha_name: str, key_events: str, parsha_text: str, draft: str, issues: List[str]) -> str:
    prompt = f"""
Название недельной главы: {parsha_name}

Опорные ключевые события (порядок важен):
{key_events}

Текст главы (сырьё):
{parsha_text}

Текущий текст (его нужно переписать заново):
{draft}

Исправь строго по проблемам:
{json.dumps(issues, ensure_ascii=False)}

Правила:
- не добавляй новых событий
- не выводи внутренние проверки
- соблюдай стиль и имена
- сделай текст читаемым за 45-90 секунд
"""
    return await openai_chat_with_fallback(
        system_prompt=SYSTEM_PROMPT_MAIN,
        user_prompt=prompt,
        temperature=0.25,
        timeout_s=55,
    )

# ---------------- TELEGRAM COMMANDS ----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Шалом.\n\n"
        "Я присылаю краткое и точное объяснение недельной главы Торы.\n"
        "Команда: /parsha"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/parsha - получить недельную главу\n"
        "/debug - показать последнюю ошибку\n"
        "/help - помощь\n"
    )

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(get_last_error(user_id))

async def cmd_parsha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat = update.effective_chat
    typing_task = asyncio.create_task(send_typing(chat, duration_seconds=70))

    try:
        set_last_error(user_id, f"OK: started /parsha. MODEL_CHAIN={MODEL_CHAIN}")

        parsha_name = await get_current_parsha_diaspora()
        if not parsha_name:
            typing_task.cancel()
            set_last_error(user_id, "Hebcal: parasha not found.")
            await chat.send_message("Не удалось определить текущую недельную главу. Попробуй чуть позже.")
            return

        parsha_text = await sefaria_try_parsha_text(parsha_name)

        key_events = await extract_key_events(parsha_text)

        draft = await generate_post(parsha_name, key_events, parsha_text)

        # Validate and retry up to 2 rewrites
        for attempt in range(3):
            issues = validate_post(draft, parsha_name)
            if not issues:
                break
            if attempt == 2:
                logger.warning(f"Validator issues remain: {issues}")
                break
            draft = await rewrite_post(parsha_name, key_events, parsha_text, draft, issues)

        typing_task.cancel()
        set_last_error(user_id, "OK: success")

        for part in split_text(draft):
            await chat.send_message(part)

    except Exception as e:
        typing_task.cancel()
        msg = f"ERROR: {repr(e)}"
        set_last_error(user_id, msg)
        logger.exception(msg)
        await chat.send_message("Техническая ошибка. Напиши /debug - покажу подробности.")

# ---------------- BOT INIT ----------------

async def post_init(app):
    commands = [
        BotCommand("start", "Начать"),
        BotCommand("parsha", "Недельная глава"),
        BotCommand("help", "Помощь"),
        BotCommand("debug", "Показать ошибку"),
    ]
    await app.bot.set_my_commands(commands)

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

    logger.info(f"Bot started. MODEL_CHAIN={MODEL_CHAIN}. HEBCAL_GEONAMEID={HEBCAL_GEONAMEID}")
    app.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=1.0)

if __name__ == "__main__":
    main()
