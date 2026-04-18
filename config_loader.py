import json
import os


def load_lt_vk_config(path: str = os.path.join("cfg", "lt_vk_config.json")) -> dict:
    """
    Загружает конфиг для проекта Leadstech ↔ VK Ads.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Файл конфигурации не найден: {path}. "
            f"Создай cfg/lt_vk_config.json на основе примера."
        )

    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Ошибка парсинга JSON в {path}: {e}")

    return config
