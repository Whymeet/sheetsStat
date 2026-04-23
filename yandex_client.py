"""HTTP-клиент к ads_manager_Yandex (FastAPI multi-user).

Идёт по тому же JWT-flow, что и VK Ads Manager (см. `http_base`), дёргает
`GET /api/telegram/daily-stats?date=YYYY-MM-DD`.

Никаких Yandex Direct OAuth-токенов у sheetsStat нет — всё хранится на
стороне ads_manager_Yandex, привязанным к пользователю.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict

from http_base import JWTAuthClient, JWTClientConfig


YandexAdsManagerConfig = JWTClientConfig


class YandexAdsManagerAuthError(RuntimeError):
    pass


class YandexAdsManagerClient(JWTAuthClient):
    LOGGER_NAME = "sheetsstat.yandex"
    AUTH_EXC_CLS = YandexAdsManagerAuthError
    DEFAULT_TIMEOUT = 90

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
            raise RuntimeError(f"daily-stats {resp.status_code}: {resp.text[:500]}")
        return resp.json()
