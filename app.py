"""Мини-веб-морда для sheetsStat.

Два экрана: «Отчёт» (выбор даты + sub1 → VK spent + LT aggregate) и
«Настройки» (редактировать cfg/lt_vk_config.json — LeadsTech логин/пароль,
список кабинетов).

API:
    GET  /api/config            — отдать текущий конфиг (без пароля в ответе? — TODO)
    POST /api/config            — сохранить конфиг
    POST /api/report            — сгенерировать отчёт {date, sub1}
    GET  /api/reports           — список сохранённых отчётов
    GET  /api/reports/{name}    — содержимое конкретного отчёта

Статика отдаётся из папки static/.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import re

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator


_SPREADSHEET_URL_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")


def _extract_spreadsheet_id(value: str) -> str:
    """Принимает либо чистый ID, либо полный URL Google Sheets."""
    value = (value or "").strip()
    m = _SPREADSHEET_URL_RE.search(value)
    return m.group(1) if m else value

from config_loader import load_lt_vk_config
from core import build_report


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sheetsstat.web")


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "cfg" / "lt_vk_config.json"
OUTPUT_DIR = BASE_DIR / "output"
STATIC_DIR = BASE_DIR / "static"

OUTPUT_DIR.mkdir(exist_ok=True)


# ---------- Models ----------

class LeadsTechSettings(BaseModel):
    base_url: str = "https://api.leads.tech"
    login: str
    password: str
    page_size: int = 500
    strictSubs: int = 0
    untilCurrentTime: int = 0
    limitLowerDay: int = 0
    limitUpperDay: int = 0
    banner_sub_fields: List[str] = Field(default_factory=lambda: ["sub4", "sub5"])


class AdsManagerSettings(BaseModel):
    base_url: str = "https://kybyshka-dev.ru"
    username: str = ""
    password: str = ""


class YandexSettings(BaseModel):
    base_url: str = ""
    username: str = ""
    password: str = ""


class YandexMetrikaSettings(BaseModel):
    oauth_token: str = ""
    counter_id: int = 0
    goals: List[str] = Field(default_factory=lambda: ["Zayvka"])
    attribution: str = "LASTSIGN"


class EightConnectSettings(BaseModel):
    base_url: str = "https://8connect.ru"
    login: str = ""
    password: str = ""
    scheme_ids: List[int] = Field(default_factory=lambda: [2260, 2805, 2809, 612])


class GoogleSheetsSettings(BaseModel):
    enabled: bool = False
    spreadsheet_id: str = ""
    service_account_json_path: str = "cfg/service_account.json"

    @field_validator("spreadsheet_id", mode="before")
    @classmethod
    def _normalize_spreadsheet_id(cls, v):
        return _extract_spreadsheet_id(v or "")


class ConfigPayload(BaseModel):
    leadstech: LeadsTechSettings
    ads_manager: AdsManagerSettings = Field(default_factory=AdsManagerSettings)
    yandex: YandexSettings = Field(default_factory=YandexSettings)
    yandex_metrika: YandexMetrikaSettings = Field(default_factory=YandexMetrikaSettings)
    eightconnect: EightConnectSettings = Field(default_factory=EightConnectSettings)
    google_sheets: GoogleSheetsSettings = Field(default_factory=GoogleSheetsSettings)
    analysis: Dict[str, Any] = Field(default_factory=lambda: {"lookback_days": 7})


class ReportRequest(BaseModel):
    date: str  # YYYY-MM-DD
    sub1: str = "kub"


# ---------- App ----------

app = FastAPI(title="sheetsStat", version="0.1.0")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/config")
def get_config():
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail=f"Config not found: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


@app.post("/api/config")
def save_config(payload: ConfigPayload):
    # Бэкап до записи
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        backup = CONFIG_PATH.with_suffix(".json.bak")
        shutil.copy2(CONFIG_PATH, backup)

    data = payload.model_dump()
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    ym = data.get("yandex_metrika") or {}
    ec = data.get("eightconnect") or {}
    gs = data.get("google_sheets") or {}
    logger.info(
        "Config saved: LT login=%s, AdsManager base=%s user=%s, Yandex base=%s user=%s, "
        "Metrika counter=%s goals=%s token=%s, 8connect login=%s schemes=%s, "
        "Sheets enabled=%s id=%s",
        data["leadstech"]["login"],
        data["ads_manager"]["base_url"],
        data["ads_manager"]["username"],
        data.get("yandex", {}).get("base_url", ""),
        data.get("yandex", {}).get("username", ""),
        ym.get("counter_id", 0),
        ym.get("goals") or [],
        "set" if ym.get("oauth_token") else "empty",
        ec.get("login") or "",
        ec.get("scheme_ids") or [],
        gs.get("enabled", False),
        gs.get("spreadsheet_id", ""),
    )
    return {"ok": True}


@app.post("/api/report")
def run_report(req: ReportRequest):
    try:
        day = datetime.strptime(req.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="date должен быть в формате YYYY-MM-DD")

    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=400, detail="Конфиг не настроен. Зайди во вкладку «Настройки».")

    config = load_lt_vk_config(str(CONFIG_PATH))

    try:
        report = build_report(config, day, req.sub1)
    except Exception as e:
        logger.error("Report error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    # Если Ads Manager вернул 0 кабинетов — это warning, но ответ всё равно
    # сохраняем (удобно видеть, что LeadsTech всё равно отдал статистику).
    if report["cabinet_count"] == 0:
        errs = (report.get("ads_manager") or {}).get("errors", [])
        if errs:
            report["warning"] = f"Ads Manager: {errs[0].get('error')}"
        else:
            report["warning"] = f"Нет кабинетов с label={req.sub1!r} у пользователя Ads Manager."

    # Сохраняем в output/
    out_file = OUTPUT_DIR / f"{day.isoformat()}_{req.sub1}.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["_saved_to"] = out_file.name

    return report


@app.get("/api/reports")
def list_reports():
    items = []
    for p in sorted(OUTPUT_DIR.glob("*.json"), reverse=True):
        items.append({
            "name": p.name,
            "size": p.stat().st_size,
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
        })
    return {"items": items}


@app.get("/api/reports/{name}")
def get_report(name: str):
    # Защита: только *.json из output/, без path traversal
    if "/" in name or "\\" in name or not name.endswith(".json"):
        raise HTTPException(status_code=400, detail="bad filename")
    p = OUTPUT_DIR / name
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="report not found")
    return json.loads(p.read_text(encoding="utf-8"))


# ---------- Static (должен идти ПОСЛЕ api-routes) ----------

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
