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

# Кто сейчас вводит текстом свой timezone
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
    # ANYTIME - дефолт
    return 12, 0


# ---------- ПОЛУЧЕНИЕ НАЗВАНИЯ ГЛАВЫ (ПОКА ЗАГЛУШКА) ----------

def get_current_parsha() -> str:
    # TODO: заменить на реальный календарь недельных глав
    return "Vayishlach"


# ---------- ГЕНЕРАЦИЯ PROMPT И ВЫЗОВ OPENAI ----------

def build_user_prompt(
    language: str,
    level: int,
    style: str,
    parsha_name: str,
    mode: str,
) -> str:
    # mode: sunday_main | midweek_detail | friday_toast | onboarding_now | manual_parsha
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


# ---------- ФУНКЦИИ РАССЫЛКИ ДЛЯ КОНКРЕТНОГО ПОЛЬЗОВАТЕЛЯ ----------

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
    # удаляем старые задачи если есть
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

    keyboard = [
        [
            InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
            InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
        ]
    ]
    text = (
        "Я объясняю недельную главу Торы простым языком - без терминов и без давления.\n\n"
        "Для начала выбери язык:"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.effective_chat.send_message(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "/start - начать и настроить бота заново\n"
        "/parsha - объяснение текущей недельной главы\n"
        "/help - краткая помощь\n"
    )
    await update.message.reply_text(text)


async def parsha_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = USER_SETTINGS.get(user_id)
    if not settings:
        await update.message.reply_text("Сначала введи /start, чтобы настроить бота.")
        return

    parsha_name = get_current_parsha()
    text = await generate_parsha_text(settings, mode="manual_parsha", parsha_name=parsha_name)
    await update.message.reply_text(text)


# ---------- CALLBACK ДЛЯ КНОПОК ОНБОРДИНГА ----------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    settings = USER_SETTINGS.get(user_id)
    if not settings:
        USER_SETTINGS[user_id] = UserSettings(user_id=user_id)
        settings = USER_SETTINGS[user_id]

    data = query.data

    # выбор языка
    if data == "lang_ru":
        settings.language = Language.RU
        text = (
            "Язык: русский.\n\n"
            "Теперь выбери, когда тебе удобнее получать сообщения:\n\n"
            "☀️ Утром\n🌤 Днем\n🌇 Вечером\n🔄 Не важно\n\n"
            "Это можно изменить в будущем."
        )
        keyboard = [
            [
                InlineKeyboardButton("☀️ Утром", callback_data="time_morning"),
                InlineKeyboardButton("🌤 Днем", callback_data="time_day"),
            ],
            [
                InlineKeyboardButton("🌇 Вечером", callback_data="time_evening"),
                InlineKeyboardButton("🔄 Не важно", callback_data="time_anytime"),
            ],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "lang_en":
        settings.language = Language.EN
        text = (
            "Language set to English.\n\n"
            "Now choose when you prefer to receive the messages:\n\n"
            "☀️ Morning\n🌤 Day\n🌇 Evening\n🔄 Any time\n\n"
            "You can change this later."
        )
        keyboard = [
            [
                InlineKeyboardButton("☀️ Morning", callback_data="time_morning"),
                InlineKeyboardButton("🌤 Day", callback_data="time_day"),
            ],
            [
                InlineKeyboardButton("🌇 Evening", callback_data="time_evening"),
                InlineKeyboardButton("🔄 Any time", callback_data="time_anytime"),
            ],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # выбор времени
    if data.startswith("time_"):
        mapping = {
            "time_morning": SendTime.MORNING,
            "time_day": SendTime.DAY,
            "time_evening": SendTime.EVENING,
            "time_anytime": SendTime.ANYTIME,
        }
        settings.send_time = mapping[data]

        # выбор часового пояса
        if settings.language == Language.RU:
            text = (
                "Теперь выбери свой часовой пояс, чтобы сообщения приходили в твое местное время.\n\n"
                "Если не видишь нужный вариант - нажми «📍 Другое» и напиши, например: Europe/Berlin или America/New_York."
            )
            keyboard = [
                [
                    InlineKeyboardButton("🇮🇱 Israel (Asia/Jerusalem)", callback_data="tz_Asia/Jerusalem"),
                ],
                [
                    InlineKeyboardButton("🇷🇺 Moscow (Europe/Moscow)", callback_data="tz_Europe/Moscow"),
                ],
                [
                    InlineKeyboardButton("🇩🇪 Europe (Europe/Berlin)", callback_data="tz_Europe/Berlin"),
                ],
                [
                    InlineKeyboardButton("🇦🇪 Dubai (Asia/Dubai)", callback_data="tz_Asia/Dubai"),
                ],
                [
                    InlineKeyboardButton("🇺🇸 New York (America/New_York)", callback_data="tz_America/New_York"),
                ],
                [
                    InlineKeyboardButton("📍 Другое", callback_data="tz_custom"),
                ],
            ]
        else:
            text = (
                "Now choose your time zone so that messages arrive in your local time.\n\n"
                "If you do not see your option - tap “📍 Other” and send something like: Europe/Berlin or America/New_York."
            )
            keyboard = [
                [
                    InlineKeyboardButton("🇮🇱 Israel (Asia/Jerusalem)", callback_data="tz_Asia/Jerusalem"),
                ],
                [
                    InlineKeyboardButton("🇪🇺 Europe (Europe/Berlin)", callback_data="tz_Europe/Berlin"),
                ],
                [
                    InlineKeyboardButton("🇦🇪 Dubai (Asia/Dubai)", callback_data="tz_Asia/Dubai"),
                ],
                [
                    InlineKeyboardButton("🇺🇸 New York (America/New_York)", callback_data="tz_America/New_York"),
                ],
                [
                    InlineKeyboardButton("📍 Other", callback_data="tz_custom"),
                ],
            ]

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # выбор часового пояса из списка
    if data.startswith("tz_") and data != "tz_custom":
        tz_name = data.removeprefix("tz_")
        try:
            ZoneInfo(tz_name)
            settings.timezone = tz_name
        except Exception:
            settings.timezone = "Asia/Dubai"

        # следующий шаг - выбор уровня
        if settings.language == Language.RU:
            text = (
                "Выбери, насколько ты знаком(а) с недельными главами:\n\n"
                "1) «Мало интересовался, хочу понимать»\n"
                "2) «Слышал, знаю немного, но не углублялся»\n"
                "3) «Знаком с основами, хочу структурнее»\n\n"
                "Это можно изменить в любой момент 🙂"
            )
        else:
            text = (
                "Choose your familiarity level with the weekly Torah portion:\n\n"
                "1) “I have not really studied, I just want to understand the basics”\n"
                "2) “I have heard things, I know a bit but not deeply”\n"
                "3) “I know the basics and want more structure”\n\n"
                "You can change this anytime 🙂"
            )
        keyboard = [
            [
                InlineKeyboardButton("1️⃣", callback_data="level_1"),
                InlineKeyboardButton("2️⃣", callback_data="level_2"),
                InlineKeyboardButton("3️⃣", callback_data="level_3"),
            ]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # пользователь выбрал "другое" - ждем текст
    if data == "tz_custom":
        TIMEZONE_AWAIT_USERS.add(user_id)
        if settings.language == Language.RU:
            await query.edit_message_text(
                "Напиши свой часовой пояс текстом, например: Europe/Berlin, Asia/Jerusalem, America/New_York."
            )
        else:
            await query.edit_message_text(
                "Please type your time zone, for example: Europe/Berlin, Asia/Jerusalem, America/New_York."
            )
        return

    # выбор уровня
    if data.startswith("level_"):
        mapping = {
            "level_1": KnowledgeLevel.LEVEL1,
            "level_2": KnowledgeLevel.LEVEL2,
            "level_3": KnowledgeLevel.LEVEL3,
        }
        settings.level = mapping[data]

        if settings.language == Language.RU:
            text = (
                "Как тебе было бы комфортнее получать объяснения недельных глав?\n\n"
                "🧑‍🤝‍🧑 Как другу\n"
                "— Я объясняю простым разговорным языком, без лишних формальностей.\n"
                "Пример: «Смотри, в этой главе происходит вот что… и вот почему это важно.»\n\n"
                "📖 Как рассказ\n"
                "— Плавно, спокойно, как короткую историю.\n"
                "Пример: «Глава начинается с того, что… шаг за шагом события раскрывают идею.»\n\n"
                "📌 Как раввин\n"
                "— По пунктам и структурно, но простым языком.\n"
                "Пример: «1) Сначала происходит это. 2) Затем — это. 3) А смысл такой.»\n\n"
                "Выбери стиль — его можно поменять в любой момент 😊"
            )
        else:
            text = (
                "How would you like me to explain the weekly portions?\n\n"
                "🧑‍🤝‍🧑 Like a friend\n"
                "— Warm, simple, conversational.\n"
                "Example: “So here’s what’s happening in this week’s portion, and why it matters.”\n\n"
                "📖 Like a story\n"
                "— Smooth and narrative, like a short chapter.\n"
                "Example: “The portion opens with… and step by step the story reveals its idea.”\n\n"
                "📌 Like a rabbi\n"
                "— Structured and clear, but easy to understand.\n"
                "Example: “1) This happens first. 2) Then this. 3) And here is the idea.”\n\n"
                "Choose the style — you can change it anytime 😊"
            )
        keyboard = [
            [
                InlineKeyboardButton("Как другу / Friend", callback_data="style_friend"),
            ],
            [
                InlineKeyboardButton("Как рассказ / Story", callback_data="style_story"),
            ],
            [
                InlineKeyboardButton("Как раввин / Rabbi", callback_data="style_rabbi"),
            ],
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # выбор стиля — завершение онбординга
    if data.startswith("style_"):
        mapping = {
            "style_friend": Style.FRIEND,
            "style_story": Style.STORY,
            "style_rabbi": Style.RABBI,
        }
        settings.style = mapping.get(data, Style.FRIEND)

        # создаём персональное расписание
        try:
            schedule_jobs_for_user(context.application, settings)
        except Exception as e:
            logger.exception(f"Scheduler error: {e}")
            await query.edit_message_text(
                "Онбординг почти готов. Возникла ошибка при настройке расписания, но бот всё равно работает.\n"
                "Если что — ты всегда можешь получить главу командой /parsha."
            )
            return

        # отправляем приветственный текст
        parsha_name = get_current_parsha()
        try:
            text = await generate_parsha_text(
                settings,
                mode="onboarding_now",
                parsha_name=parsha_name
            )
            await query.edit_message_text(text)
        except Exception as e:
            logger.exception(f"OpenAI error: {e}")
            await query.edit_message_text(
                "Онбординг завершён! Но произошла ошибка при генерации текста.\n"
                "Попробуй команду /parsha немного позже."
            )
        return


# ---------- ОБРАБОТКА ТЕКСТА ДЛЯ ВВОДА TIMEZONE ----------

async def timezone_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in TIMEZONE_AWAIT_USERS:
        # это не ввод таймзоны - игнорируем
        return

    tz_text = (update.message.text or "").strip()
    settings = USER_SETTINGS.get(user_id)
    if not settings:
        TIMEZONE_AWAIT_USERS.discard(user_id)
        await update.message.reply_text("Сначала введи /start.")
        return

    try:
        ZoneInfo(tz_text)
        settings.timezone = tz_text
        TIMEZONE_AWAIT_USERS.discard(user_id)
    except Exception:
        if settings.language == Language.RU:
            await update.message.reply_text(
                "Не смог распознать часовой пояс. Попробуй еще раз, например: Europe/Berlin или America/New_York."
            )
        else:
            await update.message.reply_text(
                "I could not recognize this time zone. Please try again, e.g. Europe/Berlin or America/New_York."
            )
        return

    # удачно - продолжаем онбординг (выбор уровня)
    if settings.language == Language.RU:
        text = (
            "Отлично! Теперь выбери, насколько ты знаком(а) с недельными главами:\n\n"
            "1) «Мало интересовался, хочу понимать»\n"
            "2) «Слышал, знаю немного, но не углублялся»\n"
            "3) «Знаком с основами, хочу структурнее»\n\n"
            "Это можно изменить в любой момент 🙂"
        )
    else:
        text = (
            "Great! Now choose your familiarity level with the weekly Torah portion:\n\n"
            "1) “I have not really studied, I just want to understand the basics”\n"
            "2) “I have heard things, I know a bit but not deeply”\n"
            "3) “I know the basics and want more structure”\n\n"
            "You can change this anytime 🙂"
        )
    keyboard = [
        [
            InlineKeyboardButton("1️⃣", callback_data="level_1"),
            InlineKeyboardButton("2️⃣", callback_data="level_2"),
            InlineKeyboardButton("3️⃣", callback_data="level_3"),
        ]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


# ---------- MAIN ----------

def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN is not set")

    application = ApplicationBuilder().token(token).build()

    # команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("parsha", parsha_command))

    # кнопки онбординга
    application.add_handler(CallbackQueryHandler(button_handler))

    # текстовые сообщения - только для ввода timezone
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, timezone_text_handler))

    # запуск планировщика
    scheduler.start()

    logger.info("Bot started")
    application.run_polling()


if __name__ == "__main__":
    main()
