"""HTTP-клиент к Ads Manager (vktest2 backend).

Берёт login/password пользователя, логинится на `POST /api/auth/login`,
хранит access_token в памяти клиента и дёргает `GET /api/telegram/daily-stats`
с query-параметрами date и label.

Никаких VK-токенов у sheetsStat нет — все кабинеты и токены VK хранятся на
стороне Ads Manager, привязанные к пользователю.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Optional

import requests


logger = logging.getLogger("sheetsstat.ads_manager")


@dataclass
class AdsManagerConfig:
    base_url: str              # например "https://kybyshka-dev.ru" или "http://localhost:8000"
    username: str
    password: str


class AdsManagerAuthError(RuntimeError):
    pass


class AdsManagerClient:
    def __init__(self, cfg: AdsManagerConfig, timeout: int = 60):
        self.cfg = cfg
        self.timeout = timeout
        self._token: Optional[str] = None

    def _api(self, path: str) -> str:
        return self.cfg.base_url.rstrip("/") + path

    def _login(self) -> str:
        url = self._api("/api/auth/login")
        logger.info("AdsManager: POST %s as %s", url, self.cfg.username)
        resp = requests.post(
            url,
            json={"username": self.cfg.username, "password": self.cfg.password},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise AdsManagerAuthError(
                f"login {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise AdsManagerAuthError(f"access_token отсутствует в ответе: {data}")
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
        # Если токен протух — один раз перелогинимся и повторим
        if resp.status_code == 401:
            logger.info("AdsManager: 401 → повторный логин")
            self._token = None
            resp = requests.get(
                self._api(path),
                headers=self._auth_headers(),
                params=params,
                timeout=self.timeout,
            )
        return resp

    # ------------------------------------------------------------------

    def get_daily_stats(
        self,
        day: date,
        label: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        GET /api/telegram/daily-stats?date=YYYY-MM-DD[&label=...]

        Возвращает:
            {
              "date": "YYYY-MM-DD",
              "label": "<str|null>",
              "accounts": [{account_id, account_name, label, spent, error?}, ...],
              "total_spent": <float>
            }
        """
        params: Dict[str, Any] = {"date": day.isoformat()}
        if label:
            params["label"] = label

        resp = self._get("/api/telegram/daily-stats", params)
        if resp.status_code != 200:
            raise RuntimeError(
                f"daily-stats {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json()
