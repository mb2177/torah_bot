import os
import logging
from enum import Enum
from typing import Dict, Tuple, Optional
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.job import Job

from openai import AsyncOpenAI

# ---------- ЛОГИ ----------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- OPENAI КЛИЕНТ ----------

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.1-mini")
client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = """
Ты - ассистент, который объясняет недельные главы Торы простым человеческим языком.

ЖЕСТКИЕ ПРАВИЛА ТОЧНОСТИ:
- Не придумывай событий, которых нет в Торе.
- Не меняй порядок событий внутри главы.
- Не добавляй персонажей.
- Не смешивай главы между собой.
- Не цитируй Тору дословно.
- Не пиши галахические законы.
- Не давай религиозные предписания.
- Не используй каббалу.
- Не используй редкие спорные мнения.
Если ты не уверен, опиши только то, что достоверно известно и общепринято.

СТРУКТУРА ЛЮБОГО ОТВЕТА:
1) Факты (60-75% текста) - точный пересказ событий недельной главы без лишних деталей.
2) Мягкий традиционный смысл (15-25%) - простое объяснение идеи главы, без терминов и споров.
3) Современное объяснение (5-10%) - человеческий язык, легкие примеры, без морализаторства и давления.

СТИЛИ:
- friend - живо, как другу, но без грубого сленга.
- story - плавно, как рассказ.
- rabbi - структурно, по пунктам, но простым языком.

УРОВНИ:
- level 1 - минимум деталей, максимум понятности.
- level 2 - больше логики и связей.
- level 3 - структурное объяснение и мягкие комментарии.

ТОН:
- спокойный, уважительный, теплый.
- без проповедей, без давления, без сравнения религий, без политики.

ТЫ ВСЕГДА УЧИТЫВАЕШЬ:
- язык (ru или en),
- уровень (1-3),
- стиль (friend/story/rabbi),
- тип сообщения (воскресное объяснение, середина недели, пятничная фраза, онбординг).

Не пиши ничего про правила и инструкции, отвечай только конечным текстом для пользователя.
"""

# ---------- НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ ----------

class Language(str, Enum):
    RU = "ru"
    EN = "en"


class KnowledgeLevel(int, Enum):
    LEVEL1 = 1
    LEVEL2 = 2
    LEVEL3 = 3


class Style(str, Enum):
    FRIEND = "friend"
    STORY = "story"
    RABBI = "rabbi"


class SendTime(str, Enum):
    MORNING = "morning"
    DAY = "day"
    EVENING = "evening"
    ANYTIME = "anytime"


class UserSettings:
    def __init__(
        self,
        user_id: int,
        language: Language = Language.RU,
        level: KnowledgeLevel = KnowledgeLevel.LEVEL1,
        style: Style = Style.FRIEND,
        send_time: SendTime = SendTime.ANYTIME,
        timezone: str = "Asia/Dubai",
    ):
        self.user_id = user_id
        self.language = language
        self.level = level
        self.style = style
        self.send_time = send_time
        self.timezone = timezone
        # id задач в планировщике
        self.job_ids: Dict[str, str] = {}

    def __repr__(self) -> str:
        return (
            f"UserSettings(user_id={self.user_id}, "
            f"language={self.language}, level={self.level}, "
            f"style={self.style}, send_time={self.send_time}, "
            f"timezone='{self.timezone}', job_ids={self.job_ids})"
        )


USER_SETTINGS: Dict[int, UserSettings] = {}
TIMEZONE_AWAIT_USERS: set[int] = set()

# ---------- APSCHEDULER ----------

scheduler = AsyncIOScheduler()


def map_send_time_to_hour_minute(send_time: SendTime) -> Tuple[int, int]:
    if send_time == SendTime.MORNING:
        return 9, 0
    if send_time == SendTime.DAY:
        return 13, 0
    if send_time == SendTime.EVENING:
        return 20, 0
    return 12, 0  # ANYTIME


# ---------- ЗАГЛУШКА НАЗВАНИЯ ГЛАВЫ ----------

def get_current_parsha() -> str:
    # TODO: реальный календарь
    return "Vayishlach"


# ---------- ГЕНЕРАЦИЯ ТЕКСТА ----------

def build_user_prompt(
    language: str,
    level: int,
    style: str,
    parsha_name: str,
    mode: str,
) -> str:
    if language == "ru":
        lang_prefix = "Пиши по-русски."
    else:
        lang_prefix = "Write in clear, simple English."

    if mode == "sunday_main":
        core = (
            "Сделай основное объяснение недельной главы."
            " Сначала коротко расскажи события главы, затем мягко объясни смысл,"
            " и в конце добавь современное человеческое объяснение."
        )
    elif mode == "midweek_detail":
        core = (
            "Выбери один интересный момент из этой недельной главы и объясни его."
            " Покажи, чем он важен, и добавь мягкую человеческую мысль."
        )
    elif mode == "friday_toast":
        core = (
            "Сделай текст строго из трех предложений. "
            "1) Напомни один момент из главы. "
            "2) Дай простую теплую мудрость. "
            "3) Сформулируй фразу, которую можно сказать семье или друзьям за столом."
        )
    elif mode == "onboarding_now":
        core = (
            "Сначала одной фразой скажи, что обычно объяснение приходит по воскресеньям,"
            " но сейчас ты отправляешь объяснение текущей главы, чтобы человек не пропустил неделю."
            " Потом сделай объяснение главы так же, как в воскресной версии."
        )
    else:
        core = "Сделай объяснение недельной главы так же, как в воскресной версии."

    return (
        f"{lang_prefix}\n"
        f"Недельная глава: {parsha_name}.\n"
        f"Уровень знания: {level}.\n"
        f"Стиль: {style}.\n"
        f"Тип сообщения: {mode}.\n"
        f"{core}"
    )


async def generate_parsha_text(
    settings: UserSettings,
    mode: str,
    parsha_name: Optional[str] = None,
) -> str:
    if parsha_name is None:
        parsha_name = get_current_parsha()

    user_prompt = build_user_prompt(
        language=settings.language.value,
        level=int(settings.level),
        style=settings.style.value,
        parsha_name=parsha_name,
        mode=mode,
    )

    resp = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()


# ---------- РАССЫЛКИ ДЛЯ ОДНОГО ПОЛЬЗОВАТЕЛЯ ----------

async def send_sunday_parsha_for_user(bot, user_id: int):
    settings = USER_SETTINGS.get(user_id)
    if not settings:
        return
    try:
        parsha_name = get_current_parsha()
        text = await generate_parsha_text(settings, mode="sunday_main", parsha_name=parsha_name)
        await bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        logger.exception(f"Error sending sunday parsha to {user_id}: {e}")


async def send_midweek_detail_for_user(bot, user_id: int):
    settings = USER_SETTINGS.get(user_id)
    if not settings:
        return
    try:
        parsha_name = get_current_parsha()
        text = await generate_parsha_text(settings, mode="midweek_detail", parsha_name=parsha_name)
        await bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        logger.exception(f"Error sending midweek detail to {user_id}: {e}")


async def send_friday_toast_for_user(bot, user_id: int):
    settings = USER_SETTINGS.get(user_id)
    if not settings:
        return
    try:
        parsha_name = get_current_parsha()
        text = await generate_parsha_text(settings, mode="friday_toast", parsha_name=parsha_name)
        await bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        logger.exception(f"Error sending friday toast to {user_id}: {e}")


def schedule_jobs_for_user(application: Application, settings: UserSettings):
    for job_id in settings.job_ids.values():
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
    settings.job_ids = {}

    hour, minute = map_send_time_to_hour_minute(settings.send_time)

    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = ZoneInfo("Asia/Dubai")
        settings.timezone = "Asia/Dubai"

    job_sun: Job = scheduler.add_job(
        send_sunday_parsha_for_user,
        trigger=CronTrigger(day_of_week="sun", hour=hour, minute=minute, timezone=tz),
        args=[application.bot, settings.user_id],
    )
    job_mid: Job = scheduler.add_job(
        send_midweek_detail_for_user,
        trigger=CronTrigger(day_of_week="wed", hour=hour, minute=minute, timezone=tz),
        args=[application.bot, settings.user_id],
    )
    job_fri: Job = scheduler.add_job(
        send_friday_toast_for_user,
        trigger=CronTrigger(day_of_week="fri", hour=hour, minute=minute, timezone=tz),
        args=[application.bot, settings.user_id],
    )

    settings.job_ids = {
        "sunday": job_sun.id,
        "midweek": job_mid.id,
        "friday": job_fri.id,
    }
    logger.info(f"Scheduled jobs for user {settings.user_id}: {settings.job_ids}")


# ---------- КОМАНДЫ ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    USER_SETTINGS[user.id] = UserSettings(user_id=user.id)
    logger.info(f"/start from {user.id}")

    keyboard = [
        [
            InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
            InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
        ]
    ]
    text = (
        "Я объясняю недельную главу Торы простым языком — без терминов и без давления.\n\n"
        "Для начала выбери язык:"
    )
    await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "/start — начать и настроить бота заново\n"
        "/parsha — объяснение текущей недельной главы\n"
        "/help — краткая помощь\n"
    )
    await update.message.reply_text(text)


async def parsha_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = USER_SETTINGS.get(user_id)
    if not settings:
        await update.message.reply_text("Сначала введи /start, чтобы настроить бота.")
        return

    parsha_name = get_current_pa_
