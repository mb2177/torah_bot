import json
import os
import requests
from pathlib import Path
from openai import AsyncOpenAI

TORAH_FILE = Path("torah_ru_full.json")
PARSHA_FILE = Path("parsha_map.json")

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_parsha_text(parsha_name: str) -> str:
    torah = load_json(TORAH_FILE)
    parsha_map = load_json(PARSHA_FILE)

    if parsha_name not in parsha_map:
        raise ValueError(f"Parsha not found: {parsha_name}")

    parsha = parsha_map[parsha_name]
    title_ru = parsha["title_ru"]

    result = [f"📖 Недельная глава: {title_ru}", ""]

    for r in parsha["ranges"]:
        book = r["book"]
        from_chapter = r["from_chapter"]
        from_verse = r["from_verse"]
        to_chapter = r["to_chapter"]
        to_verse = r["to_verse"]

        for chapter_num in range(from_chapter, to_chapter + 1):
            chapter = torah["books"][book]["chapters"][str(chapter_num)]
            verses = chapter["verses"]

            start = from_verse if chapter_num == from_chapter else 1
            end = to_verse if chapter_num == to_chapter else max(map(int, verses.keys()))

            result.append(f"Глава {chapter_num}")
            result.append("")

            for verse_num in range(start, end + 1):
                verse = verses.get(str(verse_num))
                if verse:
                    result.append(f"{verse_num}. {verse}")

            result.append("")

    return "\n".join(result).strip()


def split_for_telegram(text: str, limit: int = 3900):
    chunks = []
    current = ""

    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current.strip())
            current = line
        else:
            current += "\n" + line

    if current.strip():
        chunks.append(current.strip())

    return chunks


def get_current_parsha_name() -> str:
    """
    Берет недельную главу через Hebcal.
    Пока используем English title, который совпадает с ключами parsha_map.json.
    """
    url = "https://www.hebcal.com/shabbat"
    params = {
        "cfg": "json",
        "geonameid": "292223",  # Dubai
        "M": "on"
    }

    data = requests.get(url, params=params, timeout=20).json()

    for item in data.get("items", []):
        if item.get("category") == "parashat":
            title = item.get("title", "")
            return title.replace("Parashat ", "").strip()

    raise ValueError("Current parsha not found")


async def ai_summary(parsha_text: str) -> str:
    prompt = f"""
Ты — преподаватель Торы.

На основе текста недельной главы ниже сделай краткое, точное объяснение на русском языке.

Правила:
- Не добавляй событий, которых нет в тексте.
- Не используй мидраши и дополнительные комментарии.
- Пиши просто, понятно и уважительно.
- Сначала дай краткий пересказ событий.
- Потом дай главный смысл главы.
- Не делай слишком длинно.

Текст главы:
{parsha_text[:25000]}
"""

    res = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )

    return res.choices[0].message.content.strip()


async def ai_questions(parsha_text: str) -> str:
    prompt = f"""
Ты — преподаватель Торы.

На основе текста недельной главы составь 5 глубоких вопросов для обсуждения с учителем.

Правила:
- Вопросы должны опираться только на текст главы.
- Не добавляй мистику, каббалу или выдуманные детали.
- Вопросы должны быть умными, но понятными.

Текст главы:
{parsha_text[:25000]}
"""

    res = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
    )

    return res.choices[0].message.content.strip()