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

# Можно переопределить цепочку моделей:
# OPENAI_MODELS_FAST="gpt-5-mini,gpt-5,gpt-4.1-mini"
OPENAI_MODELS_FAST = os.getenv("OPENAI_MODELS_FAST", "gpt-5-mini,gpt-5,gpt-4.1-mini")
MODEL_CHAIN = [m.strip() for m in OPENAI_MODELS_FAST.split(",") if m.strip()]

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ---------------- ПРАВИЛА (как ты дал) ----------------
# Важно: пункт #2 про "упрощать язык" игнорируем (как ты попросил).
# Но краткость и понятность оставляем, потому что Telegram.

SYSTEM_PROMPT_EXTRACT = """
Ты извлекаешь ТОЛЬКО последовательность событий недельной главы из исходного текста Торы.

Выход: список событий (12-30 коротких пунктов), строго в правильном порядке.

Запреты:
- не добавляй ничего от себя
- не смешивай с другими главами
- не меняй порядок
- не вставляй объяснения или выводы
- не цитируй дословно длинные куски
- если не уверен - пиши более общо и без деталей
"""

SYSTEM_PROMPT_RABBI = """
Ты пишешь сообщение для Telegram о недельной главе Торы. Пишешь ТОЛЬКО по-русски.
Тон - спокойный, уважительный, без шуток, без сарказма, без давления.

Тебе передан список событий главы в правильном порядке. Ты обязан:
- описывать только то, что есть в списке
- не добавлять новых событий, людей, сцен
- не менять порядок событий
- не добавлять диалоги
- если есть сомнение - описывай обобщенно и без деталей

Лексика:
- используй традиционные еврейские имена: Моше, Аарон, Всевышний, Мишкан, Синай
- НЕ используй: Моисей, Господь, Библия, табернакль
- про Всевышнего пиши аккуратно: "Всевышний сказал/сообщил/повелел"
- НЕ очеловечивай: нельзя "разозлился", "передумал", "обиделся" и т.п.

Содержание:
- без галахи и практических предписаний
- без каббалы, без сложных мидрашей, без талмудических дискуссий
- без политики и современных конфликтов

Длина:
- "Что произошло" - 2-4 абзаца (не список, а связное повествование)
- "Смысл" - 1 абзац
- "Мысль" - 2-3 предложения

Формат (ровно так, с заголовками):
Что произошло на этой неделе
<2-4 абзаца>

Какой в этом смысл
<1 абзац>

Мысль на жизнь
<2-3 предложения>
"""

SYSTEM_PROMPT_CHECK = """
Ты строгий проверяющий текста. Тебе дают:
1) EVENTS - список событий главы
2) DRAFT - готовый текст

Нужно проверить:
A) Нет ли в DRAFT событий, которых нет в EVENTS
B) Не перепутан ли порядок (грубо, по смыслу)
C) Используются ли правильные имена: Моше, Всевышний, Аарон, Мишкан
D) Нет ли запрещенной лексики: Моисей, Господь, Библия, "Бог разозлился/передумал" и т.п.
E) Нет ли галахи, каббалы, политики, морализаторства

Выход строго в JSON:
{
  "ok": true/false,
  "issues": ["...","..."],
  "fix_instructions": "одной строкой, что исправить"
}

Если сомневаешься - ставь ok=false.
"""

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
    return LAST_ERROR_BY_USER.get(user_id, "Нет сохранённой ошибки. Всё ок или бот ещё не падал 🙂")

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

# ---------------- Sefaria ----------------

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

# ---------------- OpenAI: fallback runner ----------------

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

# ---------------- Шаг 1: события ----------------

async def extract_events_list(parsha_text: str) -> str:
    prompt = f"""
Извлеки последовательность событий недельной главы.

Требования:
- 12-30 коротких пунктов
- строго в правильном порядке
- только события, без объяснений
- если не уверен в детали - не уточняй

Текст:
{parsha_text}
"""
    return await openai_chat_with_fallback(
        system_prompt=SYSTEM_PROMPT_EXTRACT,
        user_prompt=prompt,
        temperature=0.1,
        timeout_s=35,
    )

# ---------------- Шаг 2: текст раввина ----------------

async def generate_rabbi_message(events_list_text: str) -> str:
    prompt = f"""
События главы (в правильном порядке):
{events_list_text}

Сделай итоговое Telegram-сообщение строго по правилам.
Важно: не делай списков и нумерации - это должен быть связный рассказ.
"""
    return await openai_chat_with_fallback(
        system_prompt=SYSTEM_PROMPT_RABBI,
        user_prompt=prompt,
        temperature=0.5,
        timeout_s=45,
    )

# ---------------- Шаг 3: проверка ----------------

async def check_draft(events_list_text: str, draft_text: str) -> Dict[str, Any]:
    prompt = f"""
EVENTS:
{events_list_text}

DRAFT:
{draft_text}
"""
    raw = await openai_chat_with_fallback(
        system_prompt=SYSTEM_PROMPT_CHECK,
        user_prompt=prompt,
        temperature=0.0,
        timeout_s=30,
    )
    # Пытаемся распарсить JSON максимально мягко
    try:
        import json
        return json.loads(raw)
    except Exception:
        return {"ok": False, "issues": ["checker_json_parse_failed"], "fix_instructions": "Убери лишнее, соблюдай правила и корректные имена."}

async def regenerate_with_fixes(events_list_text: str, draft_text: str, fix_instructions: str) -> str:
    prompt = f"""
События главы (в правильном порядке):
{events_list_text}

Текущий текст (его нельзя копировать, нужно переписать):
{draft_text}

Исправь строго по инструкции:
{fix_instructions}

Важно: связное повествование, коротко, без списков, без нумерации.
"""
    return await openai_chat_with_fallback(
        system_prompt=SYSTEM_PROMPT_RABBI,
        user_prompt=prompt,
        temperature=0.3,
        timeout_s=45,
    )

# ---------------- Команды ----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Шалом.\n\n"
        "Я объясняю недельную главу Торы по-русски, уважительно и по делу.\n"
        "Без фантазий, без галахи, без политики.\n\n"
        "Нажми /parsha - пришлю объяснение главы этой недели."
    )
    await update.message.reply_text(text)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/parsha - объяснение текущей недельной главы\n"
        "/debug - показать последнюю ошибку\n"
        "/start - приветствие\n"
        "/help - помощь\n"
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
            set_last_error(user_id, "Hebcal: не удалось определить parasha.")
            await chat.send_message("Не удалось определить текущую недельную главу. Попробуй еще раз чуть позже.")
            return

        parsha_text = await sefaria_try_parsha_text(parsha_name)

        events_list = await extract_events_list(parsha_text)

        draft = await generate_rabbi_message(events_list)

        check = await check_draft(events_list, draft)
        if not check.get("ok", False):
            fix = check.get("fix_instructions", "Соблюдай правила и корректные имена, не добавляй событий.")
            draft = await regenerate_with_fixes(events_list, draft, fix)

        typing_task.cancel()
        set_last_error(user_id, "OK: success")

        header = f"📖 Недельная глава: {parsha_name}\n"
        parts = split_text(draft)
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
        await chat.send_message("Техническая ошибка. Напиши /debug - покажу подробности.")

# ---------------- post_init: меню команд ----------------

async def post_init(app):
    commands = [
        BotCommand("start", "Начать"),
        BotCommand("parsha", "Текущая глава"),
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
