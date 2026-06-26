"""Мини-веб-морда для sheetsStat.

Профили (бренды): несколько независимых наборов настроек под разные
Google-таблицы. Каждый профиль — отдельный файл cfg/profiles/<id>.json
(все источники + name + sub1 + своё расписание `schedule`). Глобальный
манифест cfg/profiles.json хранит порядок, активный профиль и дефолтное
время для новых брендов.

API:
    GET    /api/profiles                — список брендов + активный + статусы
    POST   /api/profiles                — создать бренд (пустой или копией)
    PUT    /api/profiles/{id}            — переименовать
    DELETE /api/profiles/{id}            — удалить (нельзя последний)
    POST   /api/profiles/{id}/activate   — сделать активным
    GET    /api/profiles/{id}/config     — конфиг бренда (секреты маскируются)
    POST   /api/profiles/{id}/config     — сохранить конфиг бренда
    GET    /api/profiles/{id}/schedule   — расписание бренда {enabled, time}
    POST   /api/profiles/{id}/schedule   — сохранить расписание бренда
    GET    /api/schedule-settings        — legacy: дефолт времени для новых брендов
    POST   /api/schedule-settings        — legacy: сохранить дефолт времени
    POST   /api/report                   — отчёт {profile_id, date, sub1?}
    GET    /api/reports                  — список сохранённых отчётов
    GET    /api/reports/{name}           — содержимое конкретного отчёта
    GET    /api/schedule                 — статус планировщика (per-brand)
    POST   /api/schedule/run-now         — ручной прогон {profile_id?} (без — все)

Статика отдаётся из папки static/.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from core import build_report
from scheduler import ReportScheduler


_SPREADSHEET_URL_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _extract_spreadsheet_id(value: str) -> str:
    """Принимает либо чистый ID, либо полный URL Google Sheets."""
    value = (value or "").strip()
    m = _SPREADSHEET_URL_RE.search(value)
    return m.group(1) if m else value


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sheetsstat.web")


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "cfg" / "lt_vk_config.json"  # legacy: источник миграции + CLI
PROFILES_DIR = BASE_DIR / "cfg" / "profiles"
MANIFEST_PATH = BASE_DIR / "cfg" / "profiles.json"
OUTPUT_DIR = BASE_DIR / "output"
STATIC_DIR = BASE_DIR / "static"

OUTPUT_DIR.mkdir(exist_ok=True)

_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Транслитерация кириллицы для генерации id профиля из его имени.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


# Чувствительные поля в конфиге: маскируются в GET .../config и при
# POST .../config c пустой строкой заменяются значениями с диска, чтобы
# фронт не затирал креды при редактировании не-секретных полей.
SECRET_FIELDS: List[tuple] = [
    ("leadstech", "password"),
    ("ads_manager", "password"),
    ("yandex", "password"),
    ("yandex_metrika", "oauth_token"),
    ("eightconnect", "password"),
]


# ---------- Profiles storage ----------

def _slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = "".join(_TRANSLIT.get(ch, ch) for ch in s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def _gen_id(name: str, existing: set) -> str:
    base = _slugify(name) or "profile"
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def _validate_pid(pid: str) -> str:
    if not pid or not _PROFILE_ID_RE.match(pid):
        raise HTTPException(status_code=400, detail="bad profile id")
    return pid


def _profile_file(pid: str) -> Path:
    return PROFILES_DIR / f"{pid}.json"


def _default_manifest() -> Dict[str, Any]:
    return {"version": 1, "active_id": None, "order": [],
            "schedule": {"enabled": False, "time": "09:00"}}


def read_manifest() -> Dict[str, Any]:
    if not MANIFEST_PATH.exists():
        return _default_manifest()
    try:
        m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_manifest()
    m.setdefault("version", 1)
    m.setdefault("order", [])
    m.setdefault("schedule", {"enabled": False, "time": "09:00"})
    if "active_id" not in m:
        m["active_id"] = m["order"][0] if m["order"] else None
    return m


def write_manifest(m: Dict[str, Any]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    if MANIFEST_PATH.exists():
        shutil.copy2(MANIFEST_PATH, MANIFEST_PATH.with_suffix(".json.bak"))
    MANIFEST_PATH.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_profile_safe(pid: str) -> Optional[Dict[str, Any]]:
    path = _profile_file(pid)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_profile(pid: str) -> Dict[str, Any]:
    _validate_pid(pid)
    data = _read_profile_safe(pid)
    if data is None:
        raise HTTPException(status_code=404, detail=f"profile not found: {pid}")
    return data


def write_profile(pid: str, data: Dict[str, Any]) -> None:
    _validate_pid(pid)
    path = _profile_file(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_profile_ids() -> List[str]:
    """Порядок из манифеста; самовосстановление по содержимому каталога."""
    m = read_manifest()
    order = [pid for pid in m.get("order", []) if _profile_file(pid).exists()]
    if PROFILES_DIR.exists():
        for p in sorted(PROFILES_DIR.glob("*.json")):
            pid = p.stem
            if _PROFILE_ID_RE.match(pid) and pid not in order:
                order.append(pid)
    return order


def _empty_profile_body(name: str, sub1: str = "") -> Dict[str, Any]:
    return {
        "name": name,
        "sub1": sub1,
        "leadstech": {"base_url": "https://api.leads.tech", "login": "", "password": "",
                      "page_size": 500, "strictSubs": 0, "untilCurrentTime": 0,
                      "limitLowerDay": 0, "limitUpperDay": 0,
                      "banner_sub_fields": ["sub4", "sub5"]},
        "ads_manager": {"base_url": "https://kybyshka-dev.ru", "username": "", "password": ""},
        "yandex": {"base_url": "", "username": "", "password": ""},
        "yandex_metrika": {"oauth_token": "", "counter_id": 0,
                           "goals": ["Zayvka"], "attribution": "LASTSIGN"},
        "eightconnect": {"base_url": "https://8connect.ru", "login": "", "password": "",
                         "category_ids": [], "scheme_ids": [1006, 2260, 2805, 2809, 612]},
        "google_sheets": {"enabled": False, "spreadsheet_id": "",
                          "service_account_json_path": "cfg/service_account.json"},
        "schedule": {"enabled": False, "time": "09:00"},
        "analysis": {"lookback_days": 7},
    }


def ensure_profiles_migrated() -> None:
    """Идемпотентно: при первом старте создаёт профили из legacy-конфига."""
    if MANIFEST_PATH.exists():
        return
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    old = None
    if CONFIG_PATH.exists():
        try:
            old = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = None

    if old:
        sched = old.get("schedule") or {}
        sub1 = (sched.get("sub1") or "kub").strip() or "kub"
        sched_block = {"enabled": bool(sched.get("enabled", False)),
                       "time": (sched.get("time") or "09:00")}
        name = "Основной"
        pid = _gen_id(name, set())
        body = {k: v for k, v in old.items() if k != "schedule"}
        body["name"] = name
        body["sub1"] = sub1
        body["schedule"] = dict(sched_block)
        write_profile(pid, body)
        write_manifest({
            "version": 1, "active_id": pid, "order": [pid],
            "schedule": sched_block,  # дефолт времени для новых брендов
        })
        logger.info("Профили: мигрировал lt_vk_config.json → профиль %r", pid)
    else:
        pid = _gen_id("Основной", set())
        write_profile(pid, _empty_profile_body("Основной"))
        write_manifest({
            "version": 1, "active_id": pid, "order": [pid],
            "schedule": {"enabled": False, "time": "09:00"},
        })
        logger.info("Профили: создан дефолтный профиль %r", pid)


def _effective_schedule(cfg: Dict[str, Any], default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Расписание бренда. Если в профиле его нет (не мигрирован) — берём дефолт
    из манифеста, чтобы старое общее время не потерялось до записи seed'а."""
    sched = cfg.get("schedule")
    if isinstance(sched, dict):
        return {"enabled": bool(sched.get("enabled", False)), "time": sched.get("time") or "09:00"}
    md = default if default is not None else (read_manifest().get("schedule") or {})
    return {"enabled": bool(md.get("enabled", False)), "time": md.get("time") or "09:00"}


def ensure_profile_schedules_seeded() -> None:
    """Идемпотентно: у профилей без блока `schedule` засевает его из манифеста.

    Нужно для профилей, созданных до перехода на расписание-на-бренд (когда
    время было общим в манифесте). Запускается один раз при старте. Ошибка
    записи (например, нет прав на файл) не валит старт — расписание всё равно
    подхватится из манифеста через _effective_schedule.
    """
    manifest_sched = read_manifest().get("schedule") or {"enabled": False, "time": "09:00"}
    for pid in list_profile_ids():
        cfg = _read_profile_safe(pid)
        if cfg is None or isinstance(cfg.get("schedule"), dict):
            continue
        cfg["schedule"] = {"enabled": bool(manifest_sched.get("enabled", False)),
                           "time": manifest_sched.get("time") or "09:00"}
        try:
            write_profile(pid, cfg)
            logger.info("Профиль %r: засеял schedule из манифеста (%s)", pid, cfg["schedule"])
        except OSError as e:
            logger.warning("Профиль %r: не смог записать seed расписания (%s) — "
                           "расписание берётся из манифеста на лету", pid, e)


def _mask_secrets(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Возвращает копию конфига с пустыми строками вместо чувствительных полей."""
    masked = json.loads(json.dumps(cfg))  # глубокая копия через JSON
    for section, field in SECRET_FIELDS:
        if isinstance(masked.get(section), dict) and masked[section].get(field):
            masked[section][field] = ""
    return masked


def _merge_preserved_secrets(incoming: Dict[str, Any], on_disk: Dict[str, Any]) -> Dict[str, Any]:
    """Если во входящем конфиге чувствительное поле пустое — берём значение с диска."""
    for section, field in SECRET_FIELDS:
        section_in = incoming.get(section)
        section_on_disk = on_disk.get(section) if isinstance(on_disk, dict) else None
        if not isinstance(section_in, dict) or not isinstance(section_on_disk, dict):
            continue
        if not section_in.get(field):
            preserved = section_on_disk.get(field)
            if preserved:
                section_in[field] = preserved
    return incoming


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
    category_ids: List[int] = Field(default_factory=list)
    scheme_ids: List[int] = Field(default_factory=lambda: [1006, 2260, 2805, 2809, 612])


class GoogleSheetsSettings(BaseModel):
    enabled: bool = False
    spreadsheet_id: str = ""
    service_account_json_path: str = "cfg/service_account.json"

    @field_validator("spreadsheet_id", mode="before")
    @classmethod
    def _normalize_spreadsheet_id(cls, v):
        return _extract_spreadsheet_id(v or "")


class ProfileSchedule(BaseModel):
    """Расписание одного бренда (Samara-time, за прошлый день)."""

    enabled: bool = False
    time: str = "09:00"  # HH:MM, Europe/Samara

    @field_validator("time")
    @classmethod
    def _check_time(cls, v: str) -> str:
        v = (v or "").strip()
        if not _HHMM_RE.match(v):
            raise ValueError("time должен быть в формате HH:MM (24-ч), Europe/Samara")
        return v


# Алиас для обратной совместимости: общий «дефолт времени для новых брендов».
ScheduleGlobal = ProfileSchedule


class ConfigPayload(BaseModel):
    """Конфиг одного профиля (бренда). Расписание — своё, живёт в файле профиля."""

    name: Optional[str] = None
    sub1: str = ""
    leadstech: LeadsTechSettings
    ads_manager: AdsManagerSettings = Field(default_factory=AdsManagerSettings)
    yandex: YandexSettings = Field(default_factory=YandexSettings)
    yandex_metrika: YandexMetrikaSettings = Field(default_factory=YandexMetrikaSettings)
    eightconnect: EightConnectSettings = Field(default_factory=EightConnectSettings)
    google_sheets: GoogleSheetsSettings = Field(default_factory=GoogleSheetsSettings)
    schedule: ProfileSchedule = Field(default_factory=ProfileSchedule)
    analysis: Dict[str, Any] = Field(default_factory=lambda: {"lookback_days": 7})


class ProfileCreate(BaseModel):
    name: str
    copy_from: Optional[str] = None


class ProfileRename(BaseModel):
    name: str


class ReportRequest(BaseModel):
    profile_id: str
    date: str  # YYYY-MM-DD
    sub1: Optional[str] = None  # None → берём sub1 профиля


class RunNowRequest(BaseModel):
    profile_id: Optional[str] = None  # None → прогнать все бренды


# ---------- App ----------


def _profiles_provider() -> List[tuple]:
    """[(profile_id, config, sub1, schedule), ...] для планировщика."""
    out: List[tuple] = []
    md = read_manifest().get("schedule") or {}
    for pid in list_profile_ids():
        cfg = _read_profile_safe(pid)
        if cfg is None:
            continue
        sub1 = (cfg.get("sub1") or "").strip()
        schedule = _effective_schedule(cfg, md)
        out.append((pid, cfg, sub1, schedule))
    return out


scheduler: ReportScheduler = ReportScheduler(
    profiles_provider=_profiles_provider,
    output_dir=OUTPUT_DIR,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_profiles_migrated()
    ensure_profile_schedules_seeded()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="sheetsStat", version="0.1.0", lifespan=lifespan)


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------- Profiles ----------

@app.get("/api/profiles")
def list_profiles():
    m = read_manifest()
    ids = list_profile_ids()
    active = m.get("active_id")
    if active not in ids:
        active = ids[0] if ids else None
    sched_status = scheduler.status().get("profiles", {})
    profiles = []
    for pid in ids:
        cfg = _read_profile_safe(pid) or {}
        st = sched_status.get(pid) or {}
        profiles.append({
            "id": pid,
            "name": cfg.get("name") or pid,
            "sub1": cfg.get("sub1") or "",
            "is_active": pid == active,
            "schedule": _effective_schedule(cfg, m.get("schedule") or {}),
            "sheets_enabled": bool((cfg.get("google_sheets") or {}).get("enabled")),
            "next_run": st.get("next_run"),
            "last_run": st.get("last_run"),
        })
    return {
        "active_id": active,
        "default_schedule": m.get("schedule") or {"enabled": False, "time": "09:00"},
        "profiles": profiles,
    }


@app.post("/api/profiles")
def create_profile(body: ProfileCreate):
    name = (body.name or "").strip() or "Новый профиль"
    m = read_manifest()
    existing = set(list_profile_ids())
    pid = _gen_id(name, existing)

    if body.copy_from:
        src = read_profile(body.copy_from)  # 404 если нет
        data = json.loads(json.dumps(src))  # глубокая копия (с реальными секретами)
        data["name"] = name
        data.setdefault("sub1", "")
        data.setdefault("schedule", {"enabled": False, "time": "09:00"})
    else:
        data = _empty_profile_body(name)

    write_profile(pid, data)
    order = [x for x in m.get("order", []) if x != pid]
    order.append(pid)
    m["order"] = order
    m["active_id"] = pid  # новый профиль сразу активен
    write_manifest(m)
    logger.info("Профиль создан: %r (copy_from=%r)", pid, body.copy_from)
    return {"id": pid, "name": name, "active_id": pid}


@app.put("/api/profiles/{pid}")
def rename_profile(pid: str, body: ProfileRename):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="имя не может быть пустым")
    data = read_profile(pid)
    data["name"] = name
    write_profile(pid, data)
    return {"id": pid, "name": name}


@app.delete("/api/profiles/{pid}")
def delete_profile(pid: str):
    _validate_pid(pid)
    m = read_manifest()
    ids = list_profile_ids()
    if pid not in ids:
        raise HTTPException(status_code=404, detail="profile not found")
    if len(ids) <= 1:
        raise HTTPException(status_code=400, detail="нельзя удалить последний профиль")

    path = _profile_file(pid)
    bak = path.with_suffix(".json.bak")
    if path.exists():
        path.unlink()
    if bak.exists():
        bak.unlink()

    order = [x for x in m.get("order", []) if x != pid]
    m["order"] = order
    if m.get("active_id") == pid:
        m["active_id"] = order[0] if order else None
    write_manifest(m)
    scheduler.reload()
    logger.info("Профиль удалён: %r, активный → %r", pid, m["active_id"])
    return {"ok": True, "active_id": m["active_id"]}


@app.post("/api/profiles/{pid}/activate")
def activate_profile(pid: str):
    _validate_pid(pid)
    if pid not in list_profile_ids():
        raise HTTPException(status_code=404, detail="profile not found")
    m = read_manifest()
    m["active_id"] = pid
    write_manifest(m)
    return {"active_id": pid}


@app.get("/api/profiles/{pid}/config")
def get_profile_config(pid: str):
    return _mask_secrets(read_profile(pid))


@app.post("/api/profiles/{pid}/config")
def save_profile_config(pid: str, payload: ConfigPayload):
    on_disk = read_profile(pid)  # 404 если профиля нет
    data = _merge_preserved_secrets(payload.model_dump(), on_disk)
    if not data.get("name"):
        data["name"] = on_disk.get("name") or pid
    write_profile(pid, data)
    ym = data.get("yandex_metrika") or {}
    ec = data.get("eightconnect") or {}
    gs = data.get("google_sheets") or {}
    logger.info(
        "Profile %r saved: sub1=%s, LT login=%s, AdsManager user=%s, Yandex user=%s, "
        "Metrika counter=%s token=%s, 8connect login=%s schemes=%s, Sheets enabled=%s id=%s",
        pid, data.get("sub1"),
        data["leadstech"]["login"], data["ads_manager"]["username"],
        data.get("yandex", {}).get("username", ""),
        ym.get("counter_id", 0), "set" if ym.get("oauth_token") else "empty",
        ec.get("login") or "", ec.get("scheme_ids") or [],
        gs.get("enabled", False), gs.get("spreadsheet_id", ""),
    )
    scheduler.reload()
    return {"ok": True}


# ---------- Schedule (своё у каждого бренда) ----------

@app.get("/api/profiles/{pid}/schedule")
def get_profile_schedule(pid: str):
    cfg = read_profile(pid)  # 404 если нет
    return _effective_schedule(cfg)


@app.post("/api/profiles/{pid}/schedule")
def save_profile_schedule(pid: str, s: ProfileSchedule):
    cfg = read_profile(pid)  # 404 если нет
    cfg["schedule"] = {"enabled": s.enabled, "time": s.time}
    write_profile(pid, cfg)
    scheduler.reload()
    logger.info("Профиль %r: расписание enabled=%s time=%s", pid, s.enabled, s.time)
    return {"ok": True, "schedule": cfg["schedule"]}


# Legacy: дефолтное время для новых брендов (в манифесте). UI больше им не управляет.
@app.get("/api/schedule-settings")
def get_schedule_settings():
    return read_manifest().get("schedule") or {"enabled": False, "time": "09:00"}


@app.post("/api/schedule-settings")
def save_schedule_settings(s: ScheduleGlobal):
    m = read_manifest()
    m["schedule"] = {"enabled": s.enabled, "time": s.time}
    write_manifest(m)
    logger.info("Дефолт расписания сохранён: enabled=%s time=%s", s.enabled, s.time)
    return {"ok": True}


@app.get("/api/schedule")
def get_schedule():
    return scheduler.status()


@app.post("/api/schedule/run-now")
async def schedule_run_now(req: Optional[RunNowRequest] = None):
    pid = (req.profile_id if req else None) or None
    return await scheduler.trigger_now(pid)


@app.post("/api/report")
def run_report(req: ReportRequest):
    try:
        day = datetime.strptime(req.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="date должен быть в формате YYYY-MM-DD")

    config = read_profile(req.profile_id)  # 404 если профиля нет
    sub1 = (req.sub1 or "").strip() or (config.get("sub1") or "").strip()

    try:
        report = build_report(config, day, sub1)
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
            report["warning"] = f"Нет кабинетов с label={sub1!r} у пользователя Ads Manager."

    # Сохраняем в output/ (profile_id в имени — sub1 у профилей может совпадать)
    out_file = OUTPUT_DIR / f"{day.isoformat()}_{sub1}__{req.profile_id}.json"
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
