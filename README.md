# TorahBot MVP

Telegram bot that:
- automatically detects the weekly parsha via Hebcal
- fetches the Torah text from Sefaria
- sends you a Sunday reminder
- shows buttons:
  - Full parsha
  - Short summary
  - Meaning & lesson
  - Rashi commentary
  - Questions

## Setup

1. Create a Telegram bot via @BotFather and get `TELEGRAM_BOT_TOKEN`.
2. Create an OpenAI API key and set `OPENAI_API_KEY`.
3. Copy `.env.example` to `.env` locally or add variables in Railway.
4. Install dependencies:

```bash
pip install -r requirements.txt
```

5. Run locally:

```bash
python bot.py
```

6. In Telegram, send `/start`.
7. Test immediately with `/send_now`.

## Railway

Add these variables in Railway:
- TELEGRAM_BOT_TOKEN
- OPENAI_API_KEY
- OPENAI_MODEL
- SCHEDULE_TZ
- SCHEDULE_DAY_OF_WEEK
- SCHEDULE_HOUR
- SCHEDULE_MINUTE
- ISRAEL
- TORAH_LANG
- ADMIN_USER_ID

Deploy with the included `Procfile`.

## Important

Sefaria reliably provides Hebrew and English text. If you need the full Torah text in Russian 1:1, connect a trusted Russian source separately. AI should not generate the full Torah text.
