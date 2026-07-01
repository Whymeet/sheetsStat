"""Базовый HTTP-клиент с JWT-авторизацией для Ads Manager'ов.

VK Ads Manager (`ads_manager_client.py`) и Yandex Ads Manager
(`yandex_client.py`) имеют идентичный flow: `POST /api/auth/login` с
`{username,password}` → `access_token` → `Authorization: Bearer <token>`
на каждом последующем запросе, с авто-relogin на 401.

Общая механика живёт здесь, а конкретные клиенты просто наследуются
и добавляют свои `get_*` методы над `self._get`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Type

import requests

# Транзиентные сетевые сбои, на которых имеет смысл повторить запрос
# (в отличие от 4xx/5xx — там ретрай не поможет). Сюда попадают
# ReadTimeout/ConnectTimeout и обрывы соединения.
RETRYABLE_EXC = (requests.exceptions.Timeout, requests.exceptions.ConnectionError)


@dataclass
class JWTClientConfig:
    base_url: str
    username: str
    password: str


class JWTAuthError(RuntimeError):
    """Базовая ошибка авторизации. Наследники могут переопределить."""


class JWTAuthClient:
    """HTTP-клиент с JWT-авторизацией и авто-relogin на 401.

    Наследники задают `LOGGER_NAME`, опционально `AUTH_EXC_CLS` и добавляют
    публичные методы поверх `self._get` / `self._api`.
    """

    LOGGER_NAME: str = "sheetsstat.jwt_client"
    AUTH_EXC_CLS: Type[RuntimeError] = JWTAuthError
    DEFAULT_TIMEOUT: int = 60
    # Сколько раз повторить запрос после транзиентного таймаута/обрыва
    # (0 = без повторов). Пауза между попытками — RETRY_BACKOFF секунд.
    MAX_RETRIES: int = 1
    RETRY_BACKOFF: float = 1.5

    def __init__(self, cfg: JWTClientConfig, timeout: Optional[int] = None):
        self.cfg = cfg
        self.timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        self._token: Optional[str] = None
        self._logger = logging.getLogger(self.LOGGER_NAME)

    def _api(self, path: str) -> str:
        return self.cfg.base_url.rstrip("/") + path

    def _login(self) -> str:
        url = self._api("/api/auth/login")
        self._logger.info("POST %s as %s", url, self.cfg.username)
        resp = requests.post(
            url,
            json={"username": self.cfg.username, "password": self.cfg.password},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise self.AUTH_EXC_CLS(f"login {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise self.AUTH_EXC_CLS(f"access_token отсутствует в ответе: {data}")
        return token

    def _ensure_token(self) -> str:
        if self._token is None:
            self._token = self._login()
        return self._token

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    def _get(self, path: str, params: Dict[str, Any]) -> requests.Response:
        url = self._api(path)
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                resp = requests.get(
                    url,
                    headers=self._auth_headers(),
                    params=params,
                    timeout=self.timeout,
                )
                if resp.status_code == 401:
                    self._logger.info("401 → повторный логин")
                    self._token = None
                    resp = requests.get(
                        url,
                        headers=self._auth_headers(),
                        params=params,
                        timeout=self.timeout,
                    )
                return resp
            except RETRYABLE_EXC as e:
                if attempt >= self.MAX_RETRIES:
                    raise
                wait = self.RETRY_BACKOFF * (attempt + 1)
                self._logger.warning(
                    "GET %s: %s — попытка %d/%d, повтор через %.1fs",
                    url, e, attempt + 1, self.MAX_RETRIES, wait,
                )
                time.sleep(wait)
