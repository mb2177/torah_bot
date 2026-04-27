import json
from pathlib import Path

DATA_PATH = Path(__file__).parent / "torah_ru_parshiot.json"

def load_torah_ru():
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))

def get_parsha_ru(parsha_title: str):
    data = load_torah_ru()
    aliases = data.get("aliases", {})
    key = aliases.get(parsha_title.strip(), parsha_title.strip())
    return data["parshiot"].get(key)

if __name__ == "__main__":
    item = get_parsha_ru("Ki Tisa")
    print(item["title_ru"], item["reference_ru"])
    print(item["text_ru"][:1500])
