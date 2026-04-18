import os
import sys
import json
from typing import Any, Dict

import requests


BASE_URL = "https://api.leads.tech"

# ⚙️ Настройки авторизации — можно переопределить через переменные окружения
LEADSTECH_LOGIN = os.getenv("LEADSTECH_LOGIN", "12newakk8web@leads.tech")
LEADSTECH_PASSWORD = os.getenv("LEADSTECH_PASSWORD", "mcjhx4xx!")


def login(login: str, password: str) -> str:
    """
    Авторизация в LeadsTech.
    Делает POST /v1/front/authorization/login и возвращает jsonAccessWebToken.
    """
    url = f"{BASE_URL}/v1/front/authorization/login"
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json",
    }
    payload = {
        "login": login,
        "password": password,
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        print("Ошибка HTTP при авторизации:", e, file=sys.stderr)
        print("Тело ответа:", resp.text, file=sys.stderr)
        raise

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Leadstech login error: {data}")

    token = data.get("data", {}).get("jsonAccessWebToken")
    if not token:
        raise RuntimeError(f"jsonAccessWebToken not found in response: {data}")

    return token


def get_stat_by_subid(token: str) -> Dict[str, Any]:
    """
    Запрос статистики по сабам:
    GET /v1/front/stat/by-subid?page=1&pageSize=500&dateStart=16-11-2025&dateEnd=23-11-2025&
        sub1=matv&strictSubs=0&untilCurrentTime=0&limitLowerDay=0&limitUpperDay=0&subs[]=sub4
    """
    url = f"{BASE_URL}/v1/front/stat/by-subid"

    params = {
        "page": 1,
        "pageSize": 500,
        "dateStart": "16-11-2025",
        "dateEnd": "23-11-2025",
        "sub1": "matv",
        "strictSubs": 0,
        "untilCurrentTime": 0,
        "limitLowerDay": 0,
        "limitUpperDay": 0,
        "subs[]": "sub4",
    }

    headers = {
        "X-Auth-Token": token,
        "Accept": "application/json",
    }

    resp = requests.get(url, headers=headers, params=params, timeout=30)
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        print("Ошибка HTTP при запросе статы:", e, file=sys.stderr)
        print("Тело ответа:", resp.text, file=sys.stderr)
        raise

    return resp.json()


def save_json(data: Dict[str, Any], filename: str) -> None:
    """
    Сохраняет словарь data в JSON-файл с красивым форматированием.
    """
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Данные сохранены в файл: {filename}")
    except OSError as e:
        print(f"Ошибка при сохранении файла {filename}: {e}", file=sys.stderr)
        raise


def main() -> None:
    print("Авторизация в LeadsTech...")
    token = login(LEADSTECH_LOGIN, LEADSTECH_PASSWORD)
    print("Токен получен (длина):", len(token))

    print("Запрашиваю стату по сабам (by-subid)...")
    data = get_stat_by_subid(token)

    # Красиво выводим JSON в консоль, чтобы посмотреть структуру
    print(json.dumps(data, ensure_ascii=False, indent=2))

    # Имя файла можно поменять как удобно
    output_filename = "leads_stat_matv_sub4_16-23-11-2025.json"
    save_json(data, output_filename)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Ошибка выполнения скрипта:", exc, file=sys.stderr)
        sys.exit(1)
