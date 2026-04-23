"""Клиент 8connect для дневных агрегатов по категориям.

Используем ровно два endpoint'а веб-панели 8connect:
    POST /api/auth/login        — JWT-авторизация
    POST /api/report/list       — таблица отчёта, фильтруем по категориям

Ответ /api/report/list — плоский список `[{cost, charge, client, count, ...}, ...]`;
суммируем на клиенте cost, charge, client (кол-во клиентов), count (кол-во SMS).
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

import requests
from requests.exceptions import ConnectionError, Timeout, RequestException


logger = logging.getLogger("sheetsstat.eightconnect")

# Веб-панель 8connect показывает имя схемы в формате "2260: kubyshka-form 12851".
# В JSON-ответе поле может называться по-разному, а может приходить как такая
# человеко-строка. Ловим ведущие цифры.
_SCHEME_ID_RE = re.compile(r"^\s*(\d+)\b")


def _extract_scheme_id(row: Dict[str, Any]) -> Optional[int]:
    """Вытаскиваем scheme-ID из строки отчёта, перебирая возможные ключи.

    Возвращает None для строк без ID (например, итоговой строки или строки
    с каким-то агрегатом вроде «не число руб.»).
    """
    for key in ("scheme_id", "schemes_id", "schemeId", "id"):
        v = row.get(key)
        if v is None or v == "":
            continue
        try:
            return int(float(v))
        except (TypeError, ValueError):
            continue

    for key in ("schemes", "scheme", "scheme_name", "name"):
        v = row.get(key)
        if isinstance(v, (int, float)):
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
        if isinstance(v, str):
            m = _SCHEME_ID_RE.match(v)
            if m:
                return int(m.group(1))

    return None


@dataclass
class EightConnectConfig:
    base_url: str = "https://8connect.ru"
    login: str = ""
    password: str = ""


class EightConnectAuthError(RuntimeError):
    """Сервер отклонил логин/пароль или сессию."""


class EightConnectClient:
    def __init__(self, cfg: EightConnectConfig):
        self.cfg = cfg
        self._token: Optional[str] = None

    @property
    def _login_url(self) -> str:
        return f"{self.cfg.base_url.rstrip('/')}/api/auth/login"

    @property
    def _report_list_url(self) -> str:
        return f"{self.cfg.base_url.rstrip('/')}/api/report/list"

    def _login(self) -> str:
        logger.info("8connect: авторизация для логина %s", self.cfg.login)
        resp = requests.post(
            self._login_url,
            json={"login": self.cfg.login, "password": self.cfg.password},
            timeout=15,
        )
        resp.raise_for_status()

        try:
            data = resp.json()
        except ValueError:
            raise EightConnectAuthError(f"8connect login: невалидный JSON в ответе: {resp.text[:200]}")

        token = self._extract_auth_token(data)
        if not token:
            raise EightConnectAuthError(
                f"8connect login: токен не получен. Ответ: {str(data)[:200]}"
            )

        logger.info("8connect: токен получен (длина %d)", len(token))
        return token

    @staticmethod
    def _extract_auth_token(data: Any) -> Optional[str]:
        if not isinstance(data, dict):
            return None
        token = data.get("auth_token") or data.get("token")
        if token:
            return token
        nested = data.get("data")
        if isinstance(nested, dict):
            return nested.get("auth_token") or nested.get("token")
        return None

    def _get_token(self, force_refresh: bool = False) -> str:
        if force_refresh or self._token is None:
            self._token = self._login()
        return self._token

    def _request_with_retry(
        self,
        url: str,
        payload: Dict[str, Any],
        *,
        max_retries: int = 2,
        retry_delay: float = 2.0,
    ) -> requests.Response:
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                return requests.post(url, json=payload, timeout=30)
            except (ConnectionError, Timeout, RequestException) as exc:
                last_exc = exc
                if attempt < max_retries:
                    delay = retry_delay * (2 ** attempt)
                    logger.warning(
                        "8connect: сетевая ошибка (попытка %d/%d): %s. Повтор через %.1f сек...",
                        attempt + 1, max_retries + 1, exc, delay,
                    )
                    time.sleep(delay)
        raise last_exc if last_exc is not None else RuntimeError(
            "8connect: _request_with_retry вышел из цикла без исключения"
        )

    @staticmethod
    def _build_payload(
        token: str,
        day: date,
        scheme_ids: List[int],
        category_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Собираем payload в формате веб-панели 8connect.

        Фильтруем по `scheme` (пользовательские схемы 1006/2260/...), при
        необходимости дополнительно по `category`. Остальные фильтры обязаны
        присутствовать, иначе API 400.
        """
        date_str = day.strftime("%Y-%m-%d")
        category_ids = list(category_ids or [])
        filters = [
            {"value": [f"{date_str} 00:00:00", f"{date_str} 23:59:59"],
             "name": "Дата создания", "key": "date"},
            {"value": "",  "name": "Источник",     "key": "utm"},
            {"value": "",  "name": "Саб",           "key": "sub"},
            {"value": [int(c) for c in category_ids], "name": "Категория", "key": "category"},
            {"value": [int(s) for s in scheme_ids],   "name": "Схема",     "key": "scheme"},
            {"value": [],  "name": "Оффер",         "key": "offers"},
            {"value": [],  "name": "Шаблоны",       "key": "templates"},
            {"value": [],  "name": "Операторы",     "key": "operators"},
            {"value": [],  "name": "SCORE 1 PD - Модель дефолта", "key": "score1"},
            {"value": [],  "name": "SCORE 2 8conn_resp_sms",       "key": "response_sms_8conn"},
            {"value": [],  "name": "SCORE 3 AF (Antifraud)",       "key": "af_score_1f"},
            {"value": [],  "name": "SCORE 4 RM1 ",                 "key": "rm_score_1_1f"},
            {"value": [],  "name": "SCORE 5 RM2",                  "key": "rm_score_2_1f"},
            {"value": [],  "name": "SCORE 6 Resp_sms",             "key": "response_sms"},
            {"value": [],  "name": "SCORE 7 - GL MFO",             "key": "score2"},
            {"value": [],  "name": "SCORE 9 - 8conn_def",          "key": "response_8conn_def"},
            {"value": "",  "name": "UTM Source",   "key": "UtmSource"},
            {"value": "",  "name": "UTM Medium",   "key": "UtmMedium"},
            {"value": "",  "name": "UTM Campaign", "key": "UtmCampaign"},
            {"value": "",  "name": "UTM Term",     "key": "UtmTerm"},
            {"value": "",  "name": "UTM Content",  "key": "UtmContent"},
            {"value": "",  "name": "Текст",        "key": "text"},
            {"value": [],  "name": "Альфа",        "key": "alphas"},
            {"value": [],  "name": "Прокладка",    "key": "vitrinas"},
            {"value": [],  "name": "Каналы",       "key": "channels"},
        ]
        return {"type": "scheme", "filters": filters, "token": token}

    def _post_report_list(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        resp = self._request_with_retry(self._report_list_url, payload)

        if resp.status_code != 200:
            body = (resp.text or "").strip().replace("\n", " ")[:200]
            raise RuntimeError(f"8connect report/list HTTP {resp.status_code}: {body}")

        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(f"8connect report/list: невалидный JSON: {resp.text[:200]}")

        # Особые маркеры протухшей сессии — апи возвращает литералы, а не объект.
        if data == "auth_error" or data is False:
            raise EightConnectAuthError(f"8connect report/list: сессия невалидна (ответ {data!r})")

        if not isinstance(data, list):
            raise RuntimeError(f"8connect report/list: ожидали список, получили {type(data).__name__}: {str(data)[:200]}")

        return data

    def get_totals(
        self,
        day: date,
        scheme_ids: List[int],
        category_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Забираем список и суммируем по всем строкам, отфильтрованным API.

        Фильтр по API: `scheme` = scheme_ids (обязательно), `category`
        = category_ids (опционально). Клиент суммирует все строки, которые
        сервер вернул — веб-панель показывает так же.

        Суммируем: cost, charge, client (кол-во клиентов), count (кол-во SMS).

        Один retry при `auth_error`/`false` — с перелогином, на случай если
        токен протух.
        """
        scheme_ids = [int(s) for s in (scheme_ids or [])]
        category_ids = list(category_ids or [])

        token = self._get_token()
        payload = self._build_payload(token, day, scheme_ids, category_ids)

        try:
            rows = self._post_report_list(payload)
        except EightConnectAuthError:
            logger.info("8connect: перелогин (сессия протухла), повтор запроса")
            token = self._get_token(force_refresh=True)
            payload = self._build_payload(token, day, scheme_ids, category_ids)
            rows = self._post_report_list(payload)

        per_row: List[Dict[str, Any]] = []
        cost_sum = 0.0
        charge_sum = 0.0
        clients_sum = 0
        count_sum = 0

        for r in rows:
            sid = _extract_scheme_id(r)
            row_cost = _to_float(r.get("cost"))
            row_charge = _to_float(r.get("charge"))
            row_clients = _to_int(r.get("client"))
            row_count = _to_int(r.get("count"))

            cost_sum += row_cost
            charge_sum += row_charge
            clients_sum += row_clients
            count_sum += row_count

            per_row.append({
                "scheme_id": sid,
                "scheme_name": r.get("scheme_name"),
                "cost": round(row_cost, 2),
                "charge": round(row_charge, 2),
                "clients": row_clients,
                "count": row_count,
            })

        logger.info(
            "8connect: day=%s schemes=%s categories=%s → %d строк, "
            "cost=%.2f charge=%.2f clients=%d count=%d",
            day, scheme_ids, category_ids, len(rows),
            cost_sum, charge_sum, clients_sum, count_sum,
        )

        return {
            "cost": round(cost_sum, 2),
            "charge": round(charge_sum, 2),
            "clients": clients_sum,
            "count": count_sum,
            "schemes": per_row,
            "rows_received": len(rows),
            "raw": rows,
        }


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v)) if v is not None else default
    except (TypeError, ValueError):
        return default


def build_eightconnect_client(config: Dict[str, Any]) -> EightConnectClient:
    ec_cfg = config.get("eightconnect") or {}
    base_url = ec_cfg.get("base_url") or "https://8connect.ru"
    login = ec_cfg.get("login") or os.getenv("EIGHTCONNECT_LOGIN") or ""
    password = ec_cfg.get("password") or os.getenv("EIGHTCONNECT_PASSWORD") or ""

    if not login or not password:
        raise RuntimeError(
            "Не заданы 8connect login/password (ни в cfg.eightconnect, ни в env)"
        )

    return EightConnectClient(EightConnectConfig(
        base_url=base_url, login=login, password=password,
    ))
