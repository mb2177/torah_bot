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

# Цепочка моделей (если какая-то не доступна — упадём на следующую)
# Можно поменять в Railway Variables:
# OPENAI_MODELS="gpt-5-mini,gpt-5,gpt-4.1-mini"
OPENAI_MODELS = os.getenv("OPENAI_MODELS", "gpt-5-mini,gpt-5,gpt-4.1-mini")
MODEL_CHAIN = [m.strip() for m in OPENAI_MODELS.split(",") if m.strip()]

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ---------------- ТВОЙ ПРОМПТ (SYSTEM) ----------------

SYSTEM_PROMPT_MAIN = r"""
Ты - преподаватель Торы, пишешь для Telegram-бота краткий и максимально точный рассказ о недельной главе Торы.

Цель: дать читателю (без религиозного образования) понятный, уважительный и интересный пересказ главы, без ошибок и без выдумок.

ОЧЕНЬ ВАЖНО:
- Описывай только то, что действительно есть в тексте этой недельной главы.
- Никаких мидрашей, каббалы, талмудических споров, современных сравнений и "добавленных деталей".
- Если есть сомнение в детали - НЕ добавляй её. Лучше напиши более общими словами.

-----------------------
ШАГ 1: САМOПРОВЕРКА ВНАЧАЛЕ (ВНУТРЕННЕ, НЕ ПОКАЗЫВАЙ ЧИТАТЕЛЮ)
Перед тем как писать финальный текст, сделай внутренний план-проверку:
1) Мысленно перечисли 8-12 ключевых событий/заповедей этой главы (без лишних деталей).
2) Проверь, что они относятся именно к этой главе, а не к соседним.
3) Проверь, что не путаешь названия предметов и мест (например: Шатёр встречи ≠ Мишкан; Скрижали ≠ Ковчег; жертвенник ≠ Мишкан).
4) Только после этой проверки начинай писать пост.

Важно: этот внутренний план НЕ выводи в ответ. Пользователь должен видеть только готовый текст.

-----------------------
ПРАВИЛА СТИЛЯ И ТОЧНОСТИ
1) Пиши простым и живым языком: как будто объясняешь другу спокойно и уважительно.
2) Короткие предложения. Короткие абзацы. Удобно читать с телефона.
3) Используй традиционные еврейские имена и термины:
   Моше, Аарон, Всевышний, Мишкан, Синай, левиты, скрижали и т.д.
4) Не используй церковно-академические слова и тяжёлые формулировки:
   избегай "божественная кара", "беззаконие", "курительная смесь".
   пиши проще: "народ был наказан", "народ согрешил", "священные благовония".
5) Описывай Всевышнего уважительно и аккуратно:
   "Всевышний сказал/повелел/сообщил", без слишком человеческих выражений.
6) Сохраняй порядок событий, как в тексте Торы.
7) Не пиши слишком длинно: объём поста должен читаться за 45-90 секунд.

-----------------------
ОБЯЗАТЕЛЬНЫЕ ЭЛЕМЕНТЫ ПОСТА
- Заголовок: "📖 Недельная глава: <название>"
- Далее: последовательный пересказ главных событий (без перегруза деталями).
- В конце: 1-2 мягкие жизненные мысли (без морализаторства и без "пугающих" формулировок),
  максимум 3-4 предложения: чему учит глава и как это применить в жизни.

-----------------------
ШАГ 2: САМOПРОВЕРКА ПЕРЕД ОТПРАВКОЙ (ВНУТРЕННЕ, НЕ ПОКАЗЫВАЙ ЧИТАТЕЛЮ)
Перед тем как выдать финальный текст, проверь:
- Все события действительно из этой главы?
- Ничего не добавлено "от себя" как факт?
- Не перепутаны термины (Шатёр встречи/Мишкан и т.п.)?
- Текст простой и лёгкий для Telegram?
- Нет тяжёлых слов и академического тона?
Если что-то не уверен - упростить и убрать спорные детали.

Важно: этот чеклист НЕ выводи в ответ. Пользователь должен видеть только финальный пост.
""".strip()

# ---------------- Шаг 1 (извлечение событий) ----------------

SYSTEM_PROMPT_EXTRACT = """
Ты извлекаешь 8-12 ключевых событий/заповедей недельной главы Торы из данного текста (сырьё).
Только события/заповеди. Строго в порядке, как в тексте.
Без объяснений. Без выводов. Без новых деталей.
Если в детали не уверен — пиши более общо.

Выход: ровно 8-12 коротких пунктов, каждый с новой строки, без нумерации.
""".strip()

# ---------------- UX: typing ----------------

async def send_typing(chat, duration_seconds: int = 35):
    for _ in range(duration_seconds * 2):
        try:
            await chat.send_chat_action("typing")
        except Exception:
            pass
        await asyncio.sleep(0.5)

# ---------------- Telegram split ----------------

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

# ---------------- Debug ----------------

LAST_ERROR_BY_USER: Dict[int, str] = {}

def set_last_error(user_id: int, msg: str):
    LAST_ERROR_BY_USER[user_id] = msg[:3500]

def get_last_error(user_id: int) -> str:
    return LAST_ERROR_BY_USER.get(user_id, "Нет сохранённой ошибки 🙂")

# ---------------- HTTP ----------------

async def http_get_json(url: str, params: Optional[dict] = None, timeout: int = 25) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        return r.json()

# ---------------- Hebcal: parsha (Diaspora) ----------------

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

    # fallback: calendar API ближайшие 21 день
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

# ---------------- Sefaria: текст как сырьё ----------------

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

# ---------------- OpenAI: runner with fallback ----------------

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

# ---------------- Step 1: extract 8-12 key events ----------------

async def extract_key_events(parsha_text: str) -> str:
    prompt = f"""
Текст недельной главы (сырьё):
{parsha_text}

Сделай 8-12 ключевых событий/заповедей в правильном порядке.
Только события. Без объяснений. Без нумерации.
"""
    return await openai_chat_with_fallback(
        system_prompt=SYSTEM_PROMPT_EXTRACT,
        user_prompt=prompt,
        temperature=0.1,
        timeout_s=35,
    )

# ---------------- Step 2: generate final post (your prompt) ----------------

async def generate_post(parsha_name: str, key_events: str) -> str:
    prompt = f"""
Название недельной главы: {parsha_name}

Опорные ключевые события/заповеди (в правильном порядке, только как опора):
{key_events}

Напиши финальный пост строго по правилам.
"""
    return await openai_chat_with_fallback(
        system_prompt=SYSTEM_PROMPT_MAIN,
        user_prompt=prompt,
        temperature=0.45,
        timeout_s=45,
    )

# ---------------- Validator (hard rules) ----------------

BANNED_VISIBLE_WORDS = [
    "самопроверка", "чеклист", "шаг 1", "шаг 2", "план",
    "structure", "по структуре", "по списку", "по пунктам",
]
BANNED_CHRISTIAN = ["моисей", "господь", "библия", "табернакль"]
BANNED_HUMANIZING = ["разозли", "передумал", "обидел", "расстроил", "взбес", "в ярости"]
BANNED_POLITICS = ["президент", "война", "израиль", "палест", "украин", "росси"]  # грубый фильтр
BANNED_HEAVY = ["божественная кара", "беззаконие", "курительная смесь"]

def estimate_read_seconds(text: str) -> int:
    # грубо: 14 символов/сек + пробелы (быстрое чтение). Хватает для контроля длины.
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return 0
    return max(10, int(len(t) / 14))

def validate_post(text: str, parsha_name: str) -> List[str]:
    issues = []
    low = (text or "").lower()

    # Must start with correct header
    if not text.strip().startswith("📖 Недельная глава:"):
        issues.append("Нет заголовка '📖 Недельная глава: ...' в начале.")

    # Must contain parsha name somewhere near header
    if parsha_name.lower() not in low:
        issues.append("В заголовке или тексте не видно названия главы.")

    # Banned visible words
    for w in BANNED_VISIBLE_WORDS:
        if w in low:
            issues.append(f"Запрещенное слово/фраза в тексте: '{w}'")

    # Christian terms
    for w in BANNED_CHRISTIAN:
        if w in low:
            issues.append(f"Нежелательная лексика (заменить на еврейскую): '{w}'")

    # Humanizing Hashem
    for w in BANNED_HUMANIZING:
        if w in low:
            issues.append("Слишком человеческое описание Всевышнего (убрать/переписать).")
            break

    # Politics / modern conflicts
    for w in BANNED_POLITICS:
        if w in low:
            issues.append("Есть современные/политические упоминания (убрать).")
            break

    # Heavy words explicitly banned
    for w in BANNED_HEAVY:
        if w in low:
            issues.append(f"Тяжелая формулировка (упростить): '{w}'")

    # Required names style (soft requirement)
    if "всевышн" not in low:
        issues.append("Не использовано слово 'Всевышний' (лучше использовать).")
    if "моше" not in low and "аарон" not in low:
        issues.append("Слишком безлично: нет Моше/Аарона (если они есть в главе, упомяни).")

    # Length check: 45-90 sec target
    sec = estimate_read_seconds(text)
    if sec > 110:
        issues.append(f"Слишком длинно для Telegram (оценка {sec} сек). Сократить.")
    if sec < 30:
        issues.append(f"Слишком коротко (оценка {sec} сек). Чуть добавить связности, без новых деталей.")

    return issues

async def rewrite_with_instructions(parsha_name: str, key_events: str, draft: str, issues: List[str]) -> str:
    instruction = (
        "Исправь текст строго по правилам. "
        "Не добавляй новых событий, держись только ключевых событий. "
        "Убери запрещенные слова и нежелательную лексику. "
        "Сделай коротко для Telegram.\n"
        f"Проблемы: {json.dumps(issues, ensure_ascii=False)}"
    )
    prompt = f"""
Название недельной главы: {parsha_name}

Ключевые события/заповеди (опора, порядок важен):
{key_events}

Текущий текст:
{draft}

Инструкция:
{instruction}

Перепиши финальный пост заново (не списком), сохраняя порядок событий.
"""
    return await openai_chat_with_fallback(
        system_prompt=SYSTEM_PROMPT_MAIN,
        user_prompt=prompt,
        temperature=0.25,
        timeout_s=45,
    )

# ---------------- Telegram commands ----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Шалом.\n\n"
        "Я присылаю краткий и точный рассказ о недельной главе Торы.\n"
        "Нажми /parsha — и я отправлю главу этой недели."
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/parsha — получить недельную главу\n"
        "/debug — последняя ошибка\n"
        "/start — приветствие\n"
        "/help — помощь\n"
    )

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(get_last_error(user_id))

async def cmd_parsha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat = update.effective_chat
    typing_task = asyncio.create_task(send_typing(chat, duration_seconds=60))

    try:
        set_last_error(user_id, f"OK: started /parsha. MODEL_CHAIN={MODEL_CHAIN}")

        parsha_name = await get_current_parsha_diaspora()
        if not parsha_name:
            typing_task.cancel()
            set_last_error(user_id, "Hebcal: parasha not found.")
            await chat.send_message("Не удалось определить текущую недельную главу. Попробуй чуть позже.")
            return

        parsha_text = await sefaria_try_parsha_text(parsha_name)

        # Step 1: 8-12 key events (internal, not shown)
        key_events = await extract_key_events(parsha_text)

        # Step 2: generate final post
        draft = await generate_post(parsha_name, key_events)

        # Local validator + up to 2 rewrites
        for attempt in range(3):
            issues = validate_post(draft, parsha_name)
            if not issues:
                break
            if attempt == 2:
                # last attempt: accept but still send (лучше чем ничего)
                logger.warning(f"Validator issues remain after retries: {issues}")
                break
            draft = await rewrite_with_instructions(parsha_name, key_events, draft, issues)

        typing_task.cancel()
        set_last_error(user_id, "OK: success")

        for part in split_text(draft):
            await chat.send_message(part)

    except Exception as e:
        typing_task.cancel()
        msg = f"ERROR: {repr(e)}"
        set_last_error(user_id, msg)
        logger.exception(msg)
        await chat.send_message("Техническая ошибка. Напиши /debug — покажу подробности.")

# ---------------- post_init: command menu ----------------

async def post_init(app):
    commands = [
        BotCommand("start", "Начать"),
        BotCommand("parsha", "Недельная глава"),
        BotCommand("help", "Помощь"),
        BotCommand("debug", "Показать ошибку"),
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
