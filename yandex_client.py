"""HTTP-клиент к ads_manager_Yandex (FastAPI multi-user).

Логинится через `POST /api/auth/login` (тот же JWT-flow, что у VK
Ads Manager), хранит access_token в памяти и дёргает
`GET /api/telegram/daily-stats?date=YYYY-MM-DD`.

Никаких Yandex Direct OAuth-токенов у sheetsStat нет — всё хранится
на стороне ads_manager_Yandex привязанным к пользователю.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Optional

import requests


logger = logging.getLogger("sheetsstat.yandex")


@dataclass
class YandexAdsManagerConfig:
    base_url: str
    username: str
    password: str


class YandexAdsManagerAuthError(RuntimeError):
    pass


class YandexAdsManagerClient:
    def __init__(self, cfg: YandexAdsManagerConfig, timeout: int = 90):
        self.cfg = cfg
        self.timeout = timeout
        self._token: Optional[str] = None

    def _api(self, path: str) -> str:
        return self.cfg.base_url.rstrip("/") + path

    def _login(self) -> str:
        url = self._api("/api/auth/login")
        logger.info("YandexAdsManager: POST %s as %s", url, self.cfg.username)
        resp = requests.post(
            url,
            json={"username": self.cfg.username, "password": self.cfg.password},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise YandexAdsManagerAuthError(
                f"login {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise YandexAdsManagerAuthError(f"access_token отсутствует в ответе: {data}")
        return token

    def _ensure_token(self) -> str:
        if self._token is None:
            self._token = self._login()
        return self._token

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    def _get(self, path: str, params: Dict[str, Any]) -> requests.Response:
        resp = requests.get(
            self._api(path),
            headers=self._auth_headers(),
            params=params,
            timeout=self.timeout,
        )
        if resp.status_code == 401:
            logger.info("YandexAdsManager: 401 → повторный логин")
            self._token = None
            resp = requests.get(
                self._api(path),
                headers=self._auth_headers(),
                params=params,
                timeout=self.timeout,
            )
        return resp

    # ------------------------------------------------------------------

    def get_daily_stats(self, day: date) -> Dict[str, Any]:
        """
        GET /api/telegram/daily-stats?date=YYYY-MM-DD

        Возвращает:
            {
              "date": "YYYY-MM-DD",
              "accounts": [{account_id, account_name, spent, error?}, ...],
              "total_spent": <float>
            }
        """
        params: Dict[str, Any] = {"date": day.isoformat()}
        resp = self._get("/api/telegram/daily-stats", params)
        if resp.status_code != 200:
            raise RuntimeError(
                f"daily-stats {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json()
