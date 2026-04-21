"""HTTP-клиент к Yandex Metrika API.

Использует OAuth-токен Яндекса (один и тот же токен видит все сервисы,
включая Metrika). Предоставляет:
  - list_goals / find_goal_id       — Management API v1
  - get_goal_stats                  — Reporting API v1 /stat/v1/data

Возвращает по цели три числа: reaches (достижения), visits (целевые
визиты), conversion_rate (%). Формат полностью соответствует тому,
что пользователь видит в UI Метрики на /stat/conversion_rate.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

import requests
from requests.exceptions import ConnectionError as ReqConnectionError, SSLError, Timeout


logger = logging.getLogger("sheetsstat.yandex_metrika")


# Сетевые флейки, на которых имеет смысл ретраить: SSL EOF, обрыв TCP, таймаут.
_NET_FLAKES = (SSLError, ReqConnectionError, Timeout)


@dataclass
class YandexMetrikaConfig:
    oauth_token: str
    counter_id: int
    attribution: str = "LASTSIGN"
    accuracy: str = "full"


class YandexMetrikaError(RuntimeError):
    pass


class YandexMetrikaAuthError(YandexMetrikaError):
    pass


class YandexMetrikaClient:
    BASE = "https://api-metrika.yandex.net"

    def __init__(self, cfg: YandexMetrikaConfig, timeout: int = 30):
        self.cfg = cfg
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"OAuth {self.cfg.oauth_token}"}

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self.BASE + path
        backoff = 1.0
        last_net_exc: Optional[Exception] = None
        for attempt in range(1, 6):
            try:
                resp = requests.get(
                    url, headers=self._headers(), params=params or {}, timeout=self.timeout,
                )
            except _NET_FLAKES as exc:
                last_net_exc = exc
                logger.warning(
                    "Metrika net error на %s (попытка %d/5): %s — sleep %.1fs",
                    path, attempt, type(exc).__name__, backoff,
                )
                time.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code in (401, 403):
                raise YandexMetrikaAuthError(
                    f"Metrika {resp.status_code}: проверь oauth_token. body={resp.text[:300]}"
                )

            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                logger.warning(
                    "Metrika %s на %s (попытка %d/5), sleep %.1fs",
                    resp.status_code, path, attempt, backoff,
                )
                time.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code != 200:
                raise YandexMetrikaError(
                    f"Metrika {resp.status_code} на {path}: {resp.text[:500]}"
                )
            return resp.json()

        if last_net_exc is not None:
            raise YandexMetrikaError(
                f"Metrika {path}: исчерпаны ретраи по сетевым ошибкам ({type(last_net_exc).__name__}: {last_net_exc})"
            )
        raise YandexMetrikaError(f"Metrika {path}: исчерпаны ретраи по 429/5xx")

    # ------------------------------------------------------------------

    def list_goals(self) -> List[Dict[str, Any]]:
        data = self._get(f"/management/v1/counter/{self.cfg.counter_id}/goals")
        return data.get("goals", []) or []

    def find_goal_id(self, goal_name: str) -> Optional[int]:
        target = goal_name.strip().lower()
        for g in self.list_goals():
            name = (g.get("name") or "").strip().lower()
            if name == target:
                try:
                    return int(g["id"])
                except (KeyError, TypeError, ValueError):
                    continue
        return None

    def get_counter_totals(
        self,
        date_from: date,
        date_to: date,
    ) -> Dict[str, float]:
        """Общие показатели счётчика за период: визиты, просмотры, посетители.

        Соответствует дашборду «Общие показатели» в UI Метрики.
        """
        params = {
            "ids": str(self.cfg.counter_id),
            "metrics": "ym:s:visits,ym:s:pageviews,ym:s:users",
            "date1": date_from.isoformat(),
            "date2": date_to.isoformat(),
            "attribution": self.cfg.attribution,
            "accuracy": self.cfg.accuracy,
            "limit": 1,
        }
        logger.info(
            "Metrika: /stat/v1/data totals counter=%s period=%s..%s",
            self.cfg.counter_id, params["date1"], params["date2"],
        )
        payload = self._get("/stat/v1/data", params)

        totals = payload.get("totals") or []
        row = totals[0] if totals and isinstance(totals[0], list) else totals
        if len(row) < 3:
            raise YandexMetrikaError(
                f"Metrika: неожиданная структура totals (counter-wide) = {totals!r}"
            )
        visits, pageviews, users = row[:3]
        return {
            "visits": float(visits or 0),
            "pageviews": float(pageviews or 0),
            "users": float(users or 0),
        }

    def get_goal_stats(
        self,
        goal_id: int,
        date_from: date,
        date_to: date,
    ) -> Dict[str, float]:
        """Возвращает {reaches, visits, conversion_rate} по цели за период."""
        metric_prefix = f"ym:s:goal{goal_id}"
        metrics = [
            f"{metric_prefix}reaches",
            f"{metric_prefix}visits",
            f"{metric_prefix}conversionRate",
        ]
        params = {
            "ids": str(self.cfg.counter_id),
            "metrics": ",".join(metrics),
            "date1": date_from.isoformat(),
            "date2": date_to.isoformat(),
            "attribution": self.cfg.attribution,
            "accuracy": self.cfg.accuracy,
            "limit": 1,
        }
        logger.info(
            "Metrika: /stat/v1/data counter=%s goal=%s period=%s..%s attr=%s",
            self.cfg.counter_id, goal_id,
            params["date1"], params["date2"], self.cfg.attribution,
        )
        payload = self._get("/stat/v1/data", params)

        # totals может быть:
        #   - плоским списком [reaches, visits, cr] (когда нет dimensions),
        #   - вложенным [[reaches, visits, cr]] (при группировке).
        totals = payload.get("totals") or []
        if totals and isinstance(totals[0], list):
            row = totals[0]
        else:
            row = totals
        if len(row) < 3:
            raise YandexMetrikaError(
                f"Metrika: неожиданная структура ответа totals={totals!r}"
            )
        reaches, visits, cr = row[:3]
        return {
            "reaches": float(reaches or 0),
            "visits": float(visits or 0),
            "conversion_rate": float(cr or 0),
        }
