import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

import requests
from requests.exceptions import ConnectionError, Timeout, RequestException

logger = logging.getLogger("lt_vk_matcher.leadstech")


@dataclass
class LeadstechClientConfig:
    base_url: str
    login: str
    password: str
    page_size: int = 500
    strictSubs: int = 0
    untilCurrentTime: int = 0
    limitLowerDay: int = 0
    limitUpperDay: int = 0
    banner_sub_fields: List[str] = None

    def __post_init__(self):
        if self.banner_sub_fields is None:
            self.banner_sub_fields = ["sub4", "sub5"]


class LeadstechClient:
    """
    Клиент для LeadsTech:
    - авторизация: POST /v1/front/authorization/login
    - статистика по сабам: GET  /v1/front/stat/by-subid

    Один клиент на все кабинеты:
    - логинимся один раз (получаем jsonAccessWebToken)
    - для разных кабинетов меняем только sub1 в запросе
    """

    def __init__(self, cfg: LeadstechClientConfig):
        self.cfg = cfg
        self._token: Optional[str] = None

    # ----------------- URL-ы -----------------

    @property
    def _login_url(self) -> str:
        return f"{self.cfg.base_url.rstrip('/')}/v1/front/authorization/login"

    @property
    def _by_subid_url(self) -> str:
        return f"{self.cfg.base_url.rstrip('/')}/v1/front/stat/by-subid"

    # ----------------- Авторизация -----------------

    def _login(self) -> str:
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
        }
        payload = {
            "login": self.cfg.login,
            "password": self.cfg.password,
        }

        logger.info("Leadstech: авторизация для логина %s", self.cfg.login)

        resp = requests.post(self._login_url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()

        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"Leadstech login error: {data}")

        token = data.get("data", {}).get("jsonAccessWebToken")
        if not token:
            raise RuntimeError(f"jsonAccessWebToken not found in response: {data}")

        logger.info("Leadstech: токен получен (длина %d)", len(token))
        return token

    def _get_token(self) -> str:
        if self._token is None:
            self._token = self._login()
        return self._token

    # ----------------- Retry helper -----------------

    def _request_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        **kwargs
    ) -> requests.Response:
        """
        Выполняет HTTP-запрос с повторными попытками при сетевых ошибках.

        Args:
            method: HTTP метод ('get', 'post')
            url: URL для запроса
            max_retries: максимальное количество повторных попыток
            retry_delay: базовая задержка между попытками (в секундах)
            **kwargs: параметры для requests (headers, params, timeout и т.д.)
        """
        last_exception = None

        for attempt in range(max_retries + 1):
            try:
                if method.lower() == 'get':
                    resp = requests.get(url, **kwargs)
                elif method.lower() == 'post':
                    resp = requests.post(url, **kwargs)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")

                return resp

            except (ConnectionError, Timeout, RequestException) as exc:
                last_exception = exc

                if attempt < max_retries:
                    # Экспоненциальная задержка: 2s, 4s, 8s...
                    delay = retry_delay * (2 ** attempt)
                    logger.warning(
                        "Leadstech: ошибка подключения (попытка %d/%d): %s. Повтор через %.1f сек...",
                        attempt + 1,
                        max_retries + 1,
                        exc,
                        delay
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "Leadstech: не удалось выполнить запрос после %d попыток",
                        max_retries + 1
                    )
                    raise

        # Этот код не должен выполняться, но на всякий случай
        raise last_exception

    def get_summary_by_sub1(
        self,
        date_from: date,
        date_to: date,
        sub1_value: str,
    ) -> Dict[str, Any]:
        """
        Один запрос `GET /v1/front/stat/by-subid?subs[]=sub1&sub1=<value>` —
        LeadsTech сам возвращает готовый агрегат в поле `data.summary`.

        Пагинацию и мерж rows делать НЕ нужно: summary уже суммарен по всему
        результату запроса.

        Возвращает `data.summary` как есть — там поля clicks / uniques / hosts /
        conversions / approved / rejected / inprogress / sumwebmaster / CR / AR / ...
        """
        token = self._get_token()
        headers = {
            "X-Auth-Token": token,
            "Accept": "application/json",
        }
        # summary одинаков на любой странице, поэтому делаем всего один запрос
        # с дефолтным pageSize из конфига (LeadsTech не принимает слишком
        # маленький pageSize — отдаёт 400). rows из ответа мы просто игнорируем.
        params: Dict[str, Any] = {
            "page": 1,
            "pageSize": self.cfg.page_size,
            "dateStart": date_from.strftime("%d-%m-%Y"),
            "dateEnd": date_to.strftime("%d-%m-%Y"),
            "sub1": sub1_value,
            "strictSubs": self.cfg.strictSubs,
            "untilCurrentTime": self.cfg.untilCurrentTime,
            "limitLowerDay": self.cfg.limitLowerDay,
            "limitUpperDay": self.cfg.limitUpperDay,
            "subs[]": "sub1",
        }

        logger.info(
            "Leadstech: summary-запрос (sub1=%s, %s..%s)",
            sub1_value, params["dateStart"], params["dateEnd"],
        )

        resp = self._request_with_retry(
            method="get",
            url=self._by_subid_url,
            headers=headers,
            params=params,
            timeout=30,
            max_retries=3,
            retry_delay=2.0,
        )

        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            logger.error(
                "Leadstech: ошибка summary-запроса: %s, body=%s",
                exc, resp.text,
            )
            raise

        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        summary = (data or {}).get("summary") or {}

        logger.info(
            "Leadstech: summary получен: clicks=%s uniques=%s hosts=%s sum=%s",
            summary.get("clicks"), summary.get("uniques"),
            summary.get("hosts"), summary.get("sumwebmaster"),
        )
        return summary

    @staticmethod
    def _extract_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Ищем массив строк статистики в ответе Leadstech.
        Часто это: {"success":true,"data":{"rows":[...]}}
        """
        data = payload.get("data")
        if isinstance(data, dict):
            if isinstance(data.get("rows"), list):
                return data["rows"]
            for key in ("items", "list", "stats"):
                if isinstance(data.get(key), list):
                    return data[key]

        if isinstance(payload, list):
            return payload

        if isinstance(payload.get("rows"), list):
            return payload["rows"]

        raise ValueError(f"Не удалось вытащить rows из ответа Leadstech: {payload}")


# --------- Хелпер: один общий клиент из конфига ---------


def build_leadstech_client(config: Dict[str, Any]) -> LeadstechClient:
    lt_cfg = config.get("leadstech", {})

    base_url = lt_cfg.get("base_url", "https://api.leads.tech")

    # либо из конфига, либо из env, если в конфиге пусто
    login = lt_cfg.get("login") or os.getenv("LEADSTECH_LOGIN")
    password = lt_cfg.get("password") or os.getenv("LEADSTECH_PASSWORD")

    if not login or not password:
        raise RuntimeError(
            "Не заданы Leadstech login/password (ни в cfg.leadstech, ни в env)"
        )

    # Поддержка как старого формата (banner_sub_field), так и нового (banner_sub_fields)
    banner_sub_fields = lt_cfg.get("banner_sub_fields")
    if not banner_sub_fields:
        # Для обратной совместимости: если задано старое поле banner_sub_field
        old_field = lt_cfg.get("banner_sub_field")
        if old_field:
            banner_sub_fields = [old_field, "sub5"] if old_field == "sub4" else [old_field]
        else:
            banner_sub_fields = ["sub4", "sub5"]

    client_cfg = LeadstechClientConfig(
        base_url=base_url,
        login=login,
        password=password,
        page_size=int(lt_cfg.get("page_size", 500)),
        strictSubs=int(lt_cfg.get("strictSubs", 0)),
        untilCurrentTime=int(lt_cfg.get("untilCurrentTime", 0)),
        limitLowerDay=int(lt_cfg.get("limitLowerDay", 0)),
        limitUpperDay=int(lt_cfg.get("limitUpperDay", 0)),
        banner_sub_fields=banner_sub_fields,
    )

    return LeadstechClient(client_cfg)
