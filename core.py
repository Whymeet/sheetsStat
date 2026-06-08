"""Сборка дневного отчёта: Ads Manager (HTTP) + LeadsTech (HTTP).

sheetsStat не хранит VK-токены и список кабинетов — всё лежит в Ads Manager,
мы туда ходим с login/password пользователя через JWT.

LeadsTech — отдельный внешний сервис, логин/пароль на него хранится у нас.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from ads_manager_client import AdsManagerClient, AdsManagerConfig
from eightconnect_client import build_eightconnect_client
from leadstech_client import build_leadstech_client
from sheets_writer import write_daily_report
from yandex_client import YandexAdsManagerClient, YandexAdsManagerConfig
from yandex_metrika_client import YandexMetrikaClient, YandexMetrikaConfig


logger = logging.getLogger("sheetsstat.core")

# sheetsStat и Ads Manager (vktest2) живут на одном домене — дефолтный URL прод.
# Можно переопределить через env ADS_MANAGER_BASE_URL (например локально для dev).
DEFAULT_ADS_MANAGER_BASE_URL = os.getenv("ADS_MANAGER_BASE_URL", "https://kybyshka-dev.ru")

_UNSET: Any = object()


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


def _collect_ads_stats(
    section: str,
    section_cfg: Dict[str, Any],
    day: date,
    *,
    client_factory,
    missing_creds_msg: str,
    log_prefix: str,
    label: Optional[str] = None,
    require_base_url: bool = False,
    server_label_override: Any = _UNSET,
) -> Dict[str, Any]:
    """Общий цикл сбора дневных расходов из Ads Manager-подобного API.

    VK и Yandex ходят в одинаковый endpoint `/api/telegram/daily-stats` с
    одинаковой формой ответа — тело обработки отличается только тем,
    прокидывается ли `label` в запрос и какое поле попадает в
    `server_label` результата.

    Возвращает унифицированный словарь:
        {"cabinets": {...}, "total": float, "server_date": ..., "errors": [...]}
    плюс `server_label`, если передан или получен с сервера.
    """
    base_url = (section_cfg.get("base_url") or "").strip()
    if not base_url and not require_base_url:
        base_url = DEFAULT_ADS_MANAGER_BASE_URL
    username = section_cfg.get("username")
    password = section_cfg.get("password")

    if not username or not password or (require_base_url and not base_url):
        return {
            "cabinets": {},
            "total": 0.0,
            "errors": [{"error": missing_creds_msg}],
        }

    client = client_factory(base_url, username, password)

    try:
        raw = client.get_daily_stats(day, label=label) if label is not None else client.get_daily_stats(day)
    except Exception as e:
        logger.error("%s error: %s", log_prefix, e, exc_info=True)
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
    }
    if server_label_override is not _UNSET:
        result["server_label"] = server_label_override
    else:
        result["server_label"] = raw.get("label")
    if errors:
        result["errors"] = errors
    return result


def collect_ads_manager(
    config: Dict[str, Any],
    day: date,
    label: str,
) -> Dict[str, Any]:
    """Расход VK Ads за день по label (sub1) через Ads Manager."""
    return _collect_ads_stats(
        section="ads_manager",
        section_cfg=config.get("ads_manager") or {},
        day=day,
        client_factory=lambda base, user, pwd: AdsManagerClient(
            AdsManagerConfig(base_url=base, username=user, password=pwd)
        ),
        missing_creds_msg="ads_manager: нужно задать username и password (зайди во вкладку «Настройки»)",
        log_prefix="AdsManager",
        label=label,
    )


def collect_yandex(config: Dict[str, Any], day: date) -> Dict[str, Any]:
    """Расход Yandex Direct за день по всем активным кабинетам пользователя."""
    return _collect_ads_stats(
        section="yandex",
        section_cfg=config.get("yandex") or {},
        day=day,
        client_factory=lambda base, user, pwd: YandexAdsManagerClient(
            YandexAdsManagerConfig(base_url=base, username=user, password=pwd)
        ),
        missing_creds_msg="yandex: задайте base_url, username и password во вкладке «Настройки»",
        log_prefix="YandexAdsManager",
        require_base_url=True,
        server_label_override=None,
    )


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

    errors: List[Dict[str, str]] = []

    # Общие показатели счётчика (визиты/просмотры/посетители) — дашборд «Общие показатели»
    try:
        totals = client.get_counter_totals(day, day)
    except Exception as e:
        logger.error("YandexMetrika totals error: %s", e, exc_info=True)
        totals = {"visits": 0, "pageviews": 0, "users": 0}
        errors.append({"error": f"counter_totals: {e}"})

    # Один запрос на справочник целей — дальше по его результатам резолвим имена
    try:
        all_goals = client.list_goals()
    except Exception as e:
        logger.error("YandexMetrika list_goals error: %s", e, exc_info=True)
        return {**base, "visits": _to_int(totals["visits"]),
                "pageviews": _to_int(totals["pageviews"]),
                "users": _to_int(totals["users"]),
                "errors": errors + [{"error": f"list_goals: {e}"}]}

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

    result: Dict[str, Any] = {
        "goals": goals_out,
        "counter_id": counter_id_int,
        "attribution": attribution,
        "visits": _to_int(totals["visits"]),
        "pageviews": _to_int(totals["pageviews"]),
        "users": _to_int(totals["users"]),
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


_OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def _save_eightconnect_raw(day: date, raw_rows: List[Dict[str, Any]]) -> None:
    """Кладём сырой ответ 8connect в output/8connect_{date}.json для дебага."""
    try:
        _OUTPUT_DIR.mkdir(exist_ok=True)
        path = _OUTPUT_DIR / f"8connect_{day.isoformat()}.json"
        path.write_text(
            json.dumps(raw_rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("8connect: сырой ответ сохранён в %s", path)
    except Exception as e:
        logger.warning("8connect: не удалось сохранить сырой ответ: %s", e)


def collect_eightconnect(config: Dict[str, Any], day: date) -> Dict[str, Any]:
    """Дневной агрегат 8connect по списку схем.

    Фильтр на API: `scheme` = scheme_ids (обязательно), `category`
    = category_ids (опционально, по умолчанию пусто). Все строки,
    возвращённые API, суммируются: cost, charge, clients, count.
    Сырой ответ сохраняется в `output/8connect_{date}.json`.

    Результат:
        {"cost": float, "charge": float, "clients": int, "count": int,
         "schemes": [...], "scheme_ids": [...], "category_ids": [...],
         "errors": [...] (опционально)}
    """
    ec_cfg = config.get("eightconnect") or {}
    login = (ec_cfg.get("login") or "").strip()
    password = (ec_cfg.get("password") or "").strip()
    # Пустой список scheme_ids = «не фильтровать по схемам» (только категории).
    # Раньше здесь стоял `or [1006,...]`: пустой [] (ложный в Python) молча
    # подменялся дефолтными схемами, из-за чего профили с category-only фильтром
    # фильтровались по чужим схемам и давали 0.
    scheme_ids = list(ec_cfg.get("scheme_ids") or [])
    category_ids = list(ec_cfg.get("category_ids") or [])

    if not login or not password:
        return {
            "cost": 0.0,
            "charge": 0.0,
            "clients": 0,
            "count": 0,
            "schemes": [],
            "scheme_ids": scheme_ids,
            "category_ids": category_ids,
            "errors": [{"error": "eightconnect: задайте login и password во вкладке «Настройки»"}],
        }

    try:
        client = build_eightconnect_client(config)
        raw = client.get_totals(day, scheme_ids, category_ids)
    except Exception as e:
        logger.error("8connect error: %s", e, exc_info=True)
        return {
            "cost": 0.0,
            "charge": 0.0,
            "clients": 0,
            "count": 0,
            "schemes": [],
            "scheme_ids": scheme_ids,
            "category_ids": category_ids,
            "errors": [{"error": str(e)}],
        }

    raw_rows = raw.get("raw") or []
    if raw_rows:
        _save_eightconnect_raw(day, raw_rows)

    return {
        "cost": round(_to_float(raw.get("cost")), 2),
        "charge": round(_to_float(raw.get("charge")), 2),
        "clients": _to_int(raw.get("clients")),
        "count": _to_int(raw.get("count")),
        "schemes": raw.get("schemes") or [],
        "scheme_ids": scheme_ids,
        "category_ids": category_ids,
        "rows_received": raw.get("rows_received", 0),
    }


def build_report(config: Dict[str, Any], day: date, sub1: str) -> Dict[str, Any]:
    """Полный отчёт за день: VK (Ads Manager) + Yandex Direct + LeadsTech + 8connect."""
    ads_manager = collect_ads_manager(config, day, label=sub1)
    yandex = collect_yandex(config, day)
    yandex_metrika = collect_yandex_metrika(config, day)
    leadstech = collect_leadstech(config, day, sub1)
    eightconnect = collect_eightconnect(config, day)

    result: Dict[str, Any] = {
        "date": day.isoformat(),
        "sub1": sub1,
        "cabinet_count": (
            len(ads_manager.get("cabinets", {})) + len(yandex.get("cabinets", {}))
        ),
        "ads_manager": ads_manager,
        "yandex": yandex,
        "yandex_metrika": yandex_metrika,
        "leadstech": leadstech,
        "eightconnect": eightconnect,
    }

    # Запись в Google Sheets (опционально, под флагом google_sheets.enabled)
    try:
        gs_summary = write_daily_report(config, day, result)
        if gs_summary:
            result["google_sheets"] = gs_summary
    except Exception as e:
        logger.error("Google Sheets write error: %s", e, exc_info=True)
        result["google_sheets"] = {"enabled": True, "error": str(e)}

    return result
