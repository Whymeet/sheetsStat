"""Сборка дневного отчёта: Ads Manager (HTTP) + LeadsTech (HTTP).

sheetsStat не хранит VK-токены и список кабинетов — всё лежит в Ads Manager,
мы туда ходим с login/password пользователя через JWT.

LeadsTech — отдельный внешний сервис, логин/пароль на него хранится у нас.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any, Dict, List

from ads_manager_client import AdsManagerClient, AdsManagerConfig
from leadstech_client import build_leadstech_client
from yandex_client import YandexAdsManagerClient, YandexAdsManagerConfig


logger = logging.getLogger("sheetsstat.core")

# sheetsStat и Ads Manager (vktest2) живут на одном домене — дефолтный URL прод.
# Можно переопределить через env ADS_MANAGER_BASE_URL (например локально для dev).
DEFAULT_ADS_MANAGER_BASE_URL = os.getenv("ADS_MANAGER_BASE_URL", "https://kybyshka-dev.ru")


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value)) if value is not None else default
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def collect_ads_manager(
    config: Dict[str, Any],
    day: date,
    label: str,
) -> Dict[str, Any]:
    """
    Идём в Ads Manager `/api/telegram/daily-stats?date=...&label=...`,
    получаем список кабинетов пользователя и расход за день, плюс total.
    """
    am_cfg = config.get("ads_manager") or {}
    base_url = (am_cfg.get("base_url") or "").strip() or DEFAULT_ADS_MANAGER_BASE_URL
    username = am_cfg.get("username")
    password = am_cfg.get("password")

    if not username or not password:
        return {
            "cabinets": {},
            "total": 0.0,
            "errors": [{"error": "ads_manager: нужно задать username и password (зайди во вкладку «Настройки»)"}],
        }

    client = AdsManagerClient(AdsManagerConfig(
        base_url=base_url, username=username, password=password,
    ))

    try:
        raw = client.get_daily_stats(day, label=label)
    except Exception as e:
        logger.error("AdsManager error: %s", e, exc_info=True)
        return {"cabinets": {}, "total": 0.0, "errors": [{"error": str(e)}]}

    per_cabinet: Dict[str, Any] = {}
    errors: List[Dict[str, str]] = []
    for acc in raw.get("accounts", []):
        name = acc.get("account_name") or f"<id={acc.get('account_id')}>"
        per_cabinet[name] = acc.get("spent")
        if acc.get("error"):
            errors.append({"cabinet": name, "error": acc["error"]})

    result: Dict[str, Any] = {
        "cabinets": per_cabinet,
        "total": round(_to_float(raw.get("total_spent")), 2),
        "server_date": raw.get("date"),
        "server_label": raw.get("label"),
    }
    if errors:
        result["errors"] = errors
    return result


def collect_yandex(
    config: Dict[str, Any],
    day: date,
    label: str,
) -> Dict[str, Any]:
    """
    Идём в ads_manager_Yandex `/api/telegram/daily-stats?date=...`,
    получаем расход с НДС по всем активным кабинетам Yandex Direct
    у указанного пользователя.
    """
    y_cfg = config.get("yandex") or {}
    base_url = (y_cfg.get("base_url") or "").strip()
    username = y_cfg.get("username")
    password = y_cfg.get("password")

    if not base_url or not username or not password:
        return {
            "cabinets": {},
            "total": 0.0,
            "errors": [{"error": "yandex: задайте base_url, username и password во вкладке «Настройки»"}],
        }

    client = YandexAdsManagerClient(YandexAdsManagerConfig(
        base_url=base_url, username=username, password=password,
    ))

    try:
        raw = client.get_daily_stats(day)
    except Exception as e:
        logger.error("YandexAdsManager error: %s", e, exc_info=True)
        return {"cabinets": {}, "total": 0.0, "errors": [{"error": str(e)}]}

    per_cabinet: Dict[str, Any] = {}
    errors: List[Dict[str, str]] = []
    for acc in raw.get("accounts", []):
        name = acc.get("account_name") or f"<id={acc.get('account_id')}>"
        per_cabinet[name] = acc.get("spent")
        if acc.get("error"):
            errors.append({"cabinet": name, "error": acc["error"]})

    result: Dict[str, Any] = {
        "cabinets": per_cabinet,
        "total": round(_to_float(raw.get("total_spent")), 2),
        "server_date": raw.get("date"),
        "server_label": label,
    }
    if errors:
        result["errors"] = errors
    return result


def collect_leadstech(config: Dict[str, Any], day: date, sub1: str) -> Dict[str, Any]:
    """Агрегат LeadsTech за день по sub1.

    Берём готовый `data.summary` из ответа LeadsTech одним запросом — там уже
    просуммировано по всем строкам, соответствующим фильтру sub1. Маппинг:
      - «клики» в отчёте = summary.uniques (уникальные клики)
      - «хосты»           = summary.hosts
      - «сумма»           = summary.sumwebmaster
    Плюс кладём несколько полезных полей (raw clicks, conversions, approved,
    rejected, CR, AR) — пригодятся в будущем без правок API.
    """
    lt_client = build_leadstech_client(config)
    summary = lt_client.get_summary_by_sub1(
        date_from=day,
        date_to=day,
        sub1_value=sub1,
    )

    return {
        "clicks": _to_int(summary.get("uniques")),       # по требованию: «клики» = uniques
        "hosts": _to_int(summary.get("hosts")),
        "sum": round(_to_float(summary.get("sumwebmaster")), 2),
        "raw_clicks": _to_int(summary.get("clicks")),
        "conversions": _to_int(summary.get("conversions")),
        "approved": _to_int(summary.get("approved")),
        "rejected": _to_int(summary.get("rejected")),
        "inprogress": _to_int(summary.get("inprogress")),
        "CR": _to_float(summary.get("CR")),
        "AR": _to_float(summary.get("AR")),
    }


def build_report(config: Dict[str, Any], day: date, sub1: str) -> Dict[str, Any]:
    """Полный отчёт за день: VK (Ads Manager) + Yandex Direct + LeadsTech."""
    ads_manager = collect_ads_manager(config, day, label=sub1)
    yandex = collect_yandex(config, day, label=sub1)
    leadstech = collect_leadstech(config, day, sub1)

    return {
        "date": day.isoformat(),
        "sub1": sub1,
        "cabinet_count": (
            len(ads_manager.get("cabinets", {})) + len(yandex.get("cabinets", {}))
        ),
        "ads_manager": ads_manager,
        "yandex": yandex,
        "leadstech": leadstech,
    }
