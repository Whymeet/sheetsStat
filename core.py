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
from yandex_metrika_client import YandexMetrikaClient, YandexMetrikaConfig


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


def _parse_goal_names(ym_cfg: Dict[str, Any]) -> List[str]:
    """Список целей: поддерживаем новый `goals: [..]` и старый `goal_name: ".."`."""
    raw = ym_cfg.get("goals")
    if isinstance(raw, str):
        # На случай если в JSON строка через запятую
        raw = [s.strip() for s in raw.split(",")]
    if not raw:
        single = (ym_cfg.get("goal_name") or "").strip()
        raw = [single] if single else []
    # Уникальные непустые, регистр сохраняем (Метрика сверяет case-insensitive у нас)
    seen: set = set()
    out: List[str] = []
    for name in raw:
        name = (name or "").strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def collect_yandex_metrika(config: Dict[str, Any], day: date) -> Dict[str, Any]:
    """Забирает у Metrika цифры по целям за день: достижения, целевые визиты, CR.

    Поддерживает список целей `yandex_metrika.goals: [..]`. Для каждой цели
    отдельная строка в результате — даже если цель не найдена или API упал
    (тогда 0/0/0 + error).
    """
    ym_cfg = config.get("yandex_metrika") or {}
    token = (ym_cfg.get("oauth_token") or "").strip()
    counter_id = ym_cfg.get("counter_id")
    attribution = (ym_cfg.get("attribution") or "LASTSIGN").strip()
    goal_names = _parse_goal_names(ym_cfg)

    base = {"goals": [], "counter_id": counter_id, "attribution": attribution}

    if not token or not counter_id:
        return {**base, "errors": [
            {"error": "yandex_metrika: задайте oauth_token и counter_id во вкладке «Настройки»"}
        ]}
    if not goal_names:
        return {**base, "errors": [{"error": "yandex_metrika: не задан ни один goal_name"}]}

    try:
        counter_id_int = int(counter_id)
    except (TypeError, ValueError):
        return {**base, "errors": [{"error": f"yandex_metrika: counter_id={counter_id!r} не число"}]}

    client = YandexMetrikaClient(YandexMetrikaConfig(
        oauth_token=token,
        counter_id=counter_id_int,
        attribution=attribution,
    ))

    # Один запрос на справочник целей — дальше по его результатам резолвим имена
    try:
        all_goals = client.list_goals()
    except Exception as e:
        logger.error("YandexMetrika list_goals error: %s", e, exc_info=True)
        return {**base, "errors": [{"error": f"list_goals: {e}"}]}

    name_to_id: Dict[str, int] = {}
    for g in all_goals:
        n = (g.get("name") or "").strip().lower()
        try:
            name_to_id[n] = int(g["id"])
        except (KeyError, TypeError, ValueError):
            continue

    goals_out: List[Dict[str, Any]] = []
    for name in goal_names:
        entry: Dict[str, Any] = {
            "goal_name": name,
            "goal_id": None,
            "reaches": 0,
            "visits": 0,
            "conversion_rate": 0.0,
        }
        goal_id = name_to_id.get(name.lower())
        if goal_id is None:
            entry["error"] = f"цель {name!r} не найдена на счётчике {counter_id_int}"
            goals_out.append(entry)
            continue

        entry["goal_id"] = goal_id
        try:
            stats = client.get_goal_stats(goal_id, day, day)
        except Exception as e:
            logger.error("YandexMetrika stats error (goal=%s): %s", name, e, exc_info=True)
            entry["error"] = f"goal_stats: {e}"
            goals_out.append(entry)
            continue

        entry["reaches"] = _to_int(stats.get("reaches"))
        entry["visits"] = _to_int(stats.get("visits"))
        entry["conversion_rate"] = round(_to_float(stats.get("conversion_rate")), 2)
        goals_out.append(entry)

    return {
        "goals": goals_out,
        "counter_id": counter_id_int,
        "attribution": attribution,
    }


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
    yandex_metrika = collect_yandex_metrika(config, day)
    leadstech = collect_leadstech(config, day, sub1)

    return {
        "date": day.isoformat(),
        "sub1": sub1,
        "cabinet_count": (
            len(ads_manager.get("cabinets", {})) + len(yandex.get("cabinets", {}))
        ),
        "ads_manager": ads_manager,
        "yandex": yandex,
        "yandex_metrika": yandex_metrika,
        "leadstech": leadstech,
    }
