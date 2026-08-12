"""HTTP-клиент к Ads Manager (vktest2 backend, VK Ads).

Берёт login/password пользователя, ходит в общий JWT-flow из `http_base`
и дёргает `GET /api/telegram/daily-stats` с query-параметрами date и label.

Никаких VK-токенов у sheetsStat нет — все кабинеты и токены VK хранятся на
стороне Ads Manager, привязанные к пользователю.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from http_base import JWTAuthClient, JWTClientConfig


# Оставляем алиас имени, чтобы не ломать публичный API.
AdsManagerConfig = JWTClientConfig


class AdsManagerAuthError(RuntimeError):
    pass


class AdsManagerClient(JWTAuthClient):
    LOGGER_NAME = "sheetsstat.ads_manager"
    AUTH_EXC_CLS = AdsManagerAuthError
    DEFAULT_TIMEOUT = 60

    def get_daily_stats(
        self,
        day: date,
        label: Optional[str] = None,
        all_accounts: bool = False,
    ) -> Dict[str, Any]:
        """
        GET /api/telegram/daily-stats?date=YYYY-MM-DD[&label=...][&all=true]

        all_accounts=True — сервер отдаёт ВСЕ кабинеты пользователя (игнорирует
        label и флаг include_in_daily_stats на стороне Ads Manager).

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
        if all_accounts:
            params["all"] = "true"

        resp = self._get("/api/telegram/daily-stats", params)
        if resp.status_code != 200:
            raise RuntimeError(f"daily-stats {resp.status_code}: {resp.text[:500]}")
        return resp.json()
