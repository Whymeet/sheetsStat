import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List

import requests
import time 

logger = logging.getLogger("lt_vk_matcher.vk_ads")


def _dget(dct: Dict[str, Any], dotted: str, default=0.0) -> float:
    cur: Any = dct
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    try:
        return float(cur)
    except (TypeError, ValueError):
        return default


@dataclass
class VkAdsConfig:
    base_url: str
    api_token: str


class VkAdsClient:
    def __init__(self, cfg: VkAdsConfig):
        self.cfg = cfg

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.api_token}"}

    def get_banners_stats_day(
        self,
        date_from: date,
        date_to: date,
        banner_ids: List[int],
        metrics: str = "base",
    ) -> List[Dict[str, Any]]:
        """
        Запрос /statistics/banners/day.json с поддержкой ретраев при 429 Too Many Requests.
        """
        url = self.cfg.base_url.rstrip("/") + "/statistics/banners/day.json"

        params: Dict[str, Any] = {
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "metrics": metrics,
        }

        if banner_ids:
            params["id"] = ",".join(map(str, banner_ids))

        logger.info(
            "VK Ads: запрашиваем статистику по объявлениям %s (период %s..%s)",
            params.get("id", "[ALL]"),
            params["date_from"],
            params["date_to"],
        )

        max_retries = 5
        backoff = 1.0  # стартовая пауза между повторами (сек)

        for attempt in range(1, max_retries + 1):
            resp = requests.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=30,
            )

            # Ловим rate limit
            if resp.status_code == 429:
                logger.warning(
                    "VK Ads: 429 Too Many Requests для пачки id=%s, попытка %d/%d, "
                    "делаем паузу %.1f сек",
                    params.get("id", "[ALL]"),
                    attempt,
                    max_retries,
                    backoff,
                )
                time.sleep(backoff)
                backoff *= 2  # экспоненциально увеличиваем паузу
                continue

            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                logger.error(
                    "VK Ads: ошибка при запросе статистики баннеров: %s, body=%s",
                    exc,
                    resp.text,
                )
                raise

            payload = resp.json()
            items = payload.get("items", [])
            logger.info(
                "VK Ads: получено %d объявлений в статистике для пачки id=%s",
                len(items),
                params.get("id", "[ALL]"),
            )
            return items

        # Если все попытки упёрлись в 429 — логируем и возвращаем пусто, чтобы не ронять весь матчинг
        logger.error(
            "VK Ads: не удалось получить статистику по баннерам (пачка id=%s) "
            "после %d попыток из-за 429 Too Many Requests",
            params.get("id", "[ALL]"),
            max_retries,
        )
        return []

    def get_spent_by_banner(
        self,
        date_from: date,
        date_to: date,
        banner_ids: List[int],
    ) -> Dict[int, float]:
        """
        Возвращает словарь {banner_id: spent} за указанный период.

        Для избежания 414 (Request-URI Too Large) бьём список баннеров
        на пачки и запрашиваем статистику по частям.
        """
        if not banner_ids:
            return {}

        spent_by_id: Dict[int, float] = {}

        # Размер пачки — можно подправить при желании
        chunk_size = 150

        total_ids = len(banner_ids)
        logger.info(
            "VK Ads: считаем траты по %d объявлениям (пачками по %d)",
            total_ids,
            chunk_size,
        )

        for start in range(0, total_ids, chunk_size):
            chunk = banner_ids[start:start + chunk_size]

            logger.info(
                "VK Ads: запрос статистики по объявлениям %s..%s "
                "(индексы %d..%d из %d)",
                chunk[0],
                chunk[-1],
                start,
                min(start + chunk_size, total_ids) - 1,
                total_ids,
            )

            items = self.get_banners_stats_day(
                date_from,
                date_to,
                chunk,
                metrics="base",
            )

            for item in items:
                bid = item.get("id")
                if bid is None:
                    continue

                total_base = item.get("total", {}).get("base", {})
                spent = _dget(total_base, "spent", 0.0)

                try:
                    banner_id_int = int(bid)
                except (TypeError, ValueError):
                    logger.warning("VK Ads: id не int: %r", bid)
                    continue

                spent_by_id[banner_id_int] = spent

        logger.info("VK Ads: собраны траты по %d объявлениям", len(spent_by_id))
        return spent_by_id

    def list_all_banner_ids(
        self,
        statuses: str = "active,blocked,deleted",
        page_limit: int = 250,
    ) -> List[int]:
        """
        Возвращает id всех объявлений кабинета (активных/заблокированных/удалённых),
        пагинирует по 250 через /banners.json?limit=...&offset=...&_status=...

        Удалённые тоже включаем — они могли тратить деньги в прошлом дне.
        """
        url = self.cfg.base_url.rstrip("/") + "/banners.json"
        ids: List[int] = []
        offset = 0

        while True:
            params = {
                "limit": page_limit,
                "offset": offset,
                "_status": statuses,
                "fields": "id",
            }

            for attempt in range(1, 6):
                resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning("VK Ads: 429 на /banners.json, sleep %ds", wait)
                    time.sleep(wait)
                    continue
                break

            resp.raise_for_status()
            payload = resp.json()
            items = payload.get("items", [])

            for b in items:
                bid = b.get("id")
                try:
                    ids.append(int(bid))
                except (TypeError, ValueError):
                    logger.warning("VK Ads: пропускаем баннер с невалидным id: %r", bid)

            logger.info(
                "VK Ads: /banners.json offset=%d → +%d (всего собрано %d)",
                offset, len(items), len(ids),
            )

            if len(items) < page_limit:
                break
            offset += page_limit

        return ids

    def _v3_base(self) -> str:
        """`.../api/v2` → `.../api/v3`. Если base_url уже другой — возвращаем как есть."""
        base = self.cfg.base_url.rstrip("/")
        if base.endswith("/api/v2"):
            return base[: -len("/api/v2")] + "/api/v3"
        return base

    def get_cabinet_spent_day(self, day: date) -> float:
        """
        Итоговый расход кабинета за один день — одним запросом к
        `/api/v3/statistics/users/day.json`. Токен относится к одному
        рекламному аккаунту, поэтому из ответа достаточно взять
        `total.base.spent` (или сумму по `items[].total.base.spent` на случай,
        если токен агентский и видит нескольких клиентов).
        """
        url = self._v3_base().rstrip("/") + "/statistics/users/day.json"
        params = {
            "date_from": day.isoformat(),
            "date_to": day.isoformat(),
            "fields": "base",
        }

        logger.info("VK Ads v3: users/day %s (без id — берём всех доступных)", day.isoformat())

        for attempt in range(1, 6):
            resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
            if resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning("VK Ads v3: 429, sleep %ds (попытка %d/5)", wait, attempt)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        else:
            logger.error("VK Ads v3: 429 упёрлось после 5 попыток, spent=0 для дня %s", day)
            return 0.0

        payload = resp.json()

        # Верхнеуровневый total в v3 — сумма по всем items. Для токена одного
        # кабинета items=[{ ... spent ... }] и total.base.spent = items[0].total.base.spent.
        top_spent = _dget(payload.get("total", {}).get("base", {}), "spent", 0.0)

        if top_spent > 0:
            logger.info("VK Ads v3: total.base.spent=%.2f", top_spent)
            return float(top_spent)

        # Если top-level total пустой — суммируем по items вручную (страховка).
        total = 0.0
        for item in payload.get("items", []):
            total += _dget(item.get("total", {}).get("base", {}), "spent", 0.0)
        logger.info(
            "VK Ads v3: сумма по %d item(s) = %.2f",
            len(payload.get("items", [])), total,
        )
        return float(total)
