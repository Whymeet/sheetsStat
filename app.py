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
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import db
from core import ReportCancelled, build_report
from scheduler import ReportScheduler, SAMARA_TZ


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

# То же, но для секций со списком объектов: (секция, список, поле-секрет).
# Элементы списка сопоставляются с диском по (base_url, login) — устойчиво к
# перестановке строк в UI.
SECRET_LIST_FIELDS: List[tuple] = [
    ("leadstech", "accounts", "password"),
]


def _account_key(acc: Dict[str, Any]) -> tuple:
    return ((acc.get("base_url") or "").strip(), (acc.get("login") or "").strip())


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


# Хранилище — SQLite (db.py). Функции ниже сохраняют старые файловые
# сигнатуры, чтобы роуты не менялись; «манифест» собирается на лету из
# app_state + порядка профилей.

def read_manifest() -> Dict[str, Any]:
    return {
        "version": 1,
        "active_id": db.get_active_id(),
        "order": db.list_profile_ids(),
        "schedule": db.get_default_schedule(),
    }


def write_manifest(m: Dict[str, Any]) -> None:
    # Порядок профилей живёт в profiles.sort_order (create — в конец,
    # delete — строка исчезает), здесь сохраняются только остальные поля.
    db.set_active_id(m.get("active_id"))
    if isinstance(m.get("schedule"), dict):
        db.set_default_schedule(m["schedule"])


def read_profile(pid: str) -> Dict[str, Any]:
    _validate_pid(pid)
    data = db.get_profile(pid)
    if data is None:
        raise HTTPException(status_code=404, detail=f"profile not found: {pid}")
    return data


def write_profile(pid: str, data: Dict[str, Any]) -> None:
    _validate_pid(pid)
    db.upsert_profile(pid, data)


def list_profile_ids() -> List[str]:
    return db.list_profile_ids()


def _empty_profile_body(name: str, sub1: str = "") -> Dict[str, Any]:
    return {
        "name": name,
        "sub1": sub1,
        "leadstech": {"base_url": "https://api.leads.tech", "login": "", "password": "",
                      "accounts": [],
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


_RUN_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+)__([a-z0-9-]+)\.json$")
_EIGHTCONNECT_FILE_RE = re.compile(r"^8connect_(\d{4}-\d{2}-\d{2})\.json$")


def _collect_file_profiles() -> tuple[List[tuple[str, Dict[str, Any]]], Optional[str], Dict[str, Any]]:
    """Снимок профилей из файлового хранилища (или legacy, или дефолт)."""
    default_schedule = {"enabled": False, "time": "09:00"}

    if MANIFEST_PATH.exists():
        try:
            m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            m = {}
        if isinstance(m.get("schedule"), dict):
            default_schedule = {"enabled": bool(m["schedule"].get("enabled", False)),
                                "time": m["schedule"].get("time") or "09:00"}
        # порядок: манифест + glob-хвост (как старый list_profile_ids)
        order = [pid for pid in (m.get("order") or [])
                 if (PROFILES_DIR / f"{pid}.json").exists()]
        if PROFILES_DIR.exists():
            for p in sorted(PROFILES_DIR.glob("*.json")):
                if _PROFILE_ID_RE.match(p.stem) and p.stem not in order:
                    order.append(p.stem)
        profiles: List[tuple[str, Dict[str, Any]]] = []
        for pid in order:
            try:
                cfg = json.loads((PROFILES_DIR / f"{pid}.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Миграция в БД: профиль %r НЕ перенесён — битый файл (%s); "
                               "файл остался на диске, восстанови и удали data/sheetsstat.db "
                               "для повторной миграции", pid, e)
                continue
            if not isinstance(cfg.get("schedule"), dict):
                cfg["schedule"] = dict(default_schedule)  # бывший seed
            profiles.append((pid, cfg))
        ids = [pid for pid, _ in profiles]
        active_id = m.get("active_id") if m.get("active_id") in ids else (ids[0] if ids else None)
        return profiles, active_id, default_schedule

    # Манифеста нет: либо совсем legacy lt_vk_config.json, либо чистая установка.
    old = None
    if CONFIG_PATH.exists():
        try:
            old = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = None
    if old:
        sched = old.get("schedule") or {}
        default_schedule = {"enabled": bool(sched.get("enabled", False)),
                            "time": (sched.get("time") or "09:00")}
        name = "Основной"
        pid = _gen_id(name, set())
        body = {k: v for k, v in old.items() if k != "schedule"}
        body["name"] = name
        body["sub1"] = (sched.get("sub1") or "kub").strip() or "kub"
        body["schedule"] = dict(default_schedule)
        logger.info("Миграция в БД: legacy lt_vk_config.json → профиль %r", pid)
        return [(pid, body)], pid, default_schedule

    pid = _gen_id("Основной", set())
    logger.info("Миграция в БД: файлов нет, создаю дефолтный профиль %r", pid)
    return [(pid, _empty_profile_body("Основной"))], pid, default_schedule


def _collect_file_runs() -> tuple[List[Dict[str, Any]], List[tuple[str, Any]]]:
    """Снимок прогонов из output/*.json + дампов 8connect.

    rglob покрывает и подкаталоги-аномалии (sub1 со слэшем когда-то раскладывал
    отчёты по подпапкам). При дублях (pid, date) побеждает файл с бóльшим mtime.
    """
    runs_by_key: Dict[tuple, Dict[str, Any]] = {}
    eightconnect: Dict[str, Any] = {}
    skipped = 0
    if not OUTPUT_DIR.exists():
        return [], []
    files = sorted(OUTPUT_DIR.rglob("*.json"), key=lambda p: p.stat().st_mtime)
    for p in files:
        rel = p.relative_to(OUTPUT_DIR).as_posix()
        m_run = _RUN_FILE_RE.match(rel)
        m_ec = _EIGHTCONNECT_FILE_RE.match(rel)
        if not m_run and not m_ec:
            logger.info("Миграция в БД: %r не похож на отчёт/дамп — пропуск", rel)
            skipped += 1
            continue
        try:
            content = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Миграция в БД: %r пропущен — битый JSON (%s)", rel, e)
            skipped += 1
            continue
        if m_ec:
            eightconnect[m_ec.group(1)] = content
            continue
        day, sub1, pid = m_run.groups()
        mtime_iso = datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")
        runs_by_key[(pid, day)] = {
            "profile_id": pid,
            "date": day,
            "sub1": sub1,
            "ok": True,
            "trigger": "import",
            "google_sheets_error": (content.get("google_sheets") or {}).get("error")
                                   if isinstance(content, dict) else None,
            "cabinet_count": content.get("cabinet_count") if isinstance(content, dict) else None,
            "started_at": mtime_iso,
            "finished_at": mtime_iso,
            "report": content,
        }
    if skipped:
        logger.info("Миграция в БД: пропущено файлов: %d", skipped)
    return list(runs_by_key.values()), sorted(eightconnect.items())


def migrate_files_to_db() -> None:
    """Одноразовый перенос файлового хранилища в SQLite.

    Гейт — пустая таблица profiles. Файлы НЕ изменяются и НЕ удаляются
    (архив; для повторной миграции достаточно удалить data/sheetsstat.db).
    """
    if not db.is_empty():
        return
    profiles, active_id, default_schedule = _collect_file_profiles()
    runs, eightconnect = _collect_file_runs()
    db.import_snapshot(profiles, active_id, default_schedule, runs, eightconnect)
    logger.info(
        "Миграция в БД: перенесено профилей=%d (active=%r), прогонов=%d, "
        "8connect-дампов=%d → %s; исходные файлы остались как архив",
        len(profiles), active_id, len(runs), len(eightconnect), db.DB_PATH,
    )


def _effective_schedule(cfg: Dict[str, Any], default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Расписание бренда; страховка на случай профиля без блока schedule
    (например, после ручной правки БД) — дефолт из app_state."""
    sched = cfg.get("schedule")
    if isinstance(sched, dict):
        return {"enabled": bool(sched.get("enabled", False)), "time": sched.get("time") or "09:00"}
    md = default if default is not None else db.get_default_schedule()
    return {"enabled": bool(md.get("enabled", False)), "time": md.get("time") or "09:00"}


def _mask_secrets(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Возвращает копию конфига с пустыми строками вместо чувствительных полей."""
    masked = json.loads(json.dumps(cfg))  # глубокая копия через JSON
    for section, field in SECRET_FIELDS:
        if isinstance(masked.get(section), dict) and masked[section].get(field):
            masked[section][field] = ""
    for section, list_field, field in SECRET_LIST_FIELDS:
        items = (masked.get(section) or {}).get(list_field) if isinstance(masked.get(section), dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get(field):
                item[field] = ""
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

    for section, list_field, field in SECRET_LIST_FIELDS:
        section_in = incoming.get(section)
        section_on_disk = on_disk.get(section) if isinstance(on_disk, dict) else None
        if not isinstance(section_in, dict) or not isinstance(section_on_disk, dict):
            continue
        items_in = section_in.get(list_field)
        items_on_disk = section_on_disk.get(list_field)
        if not isinstance(items_in, list):
            continue
        by_key = {
            _account_key(it): it.get(field)
            for it in (items_on_disk if isinstance(items_on_disk, list) else [])
            if isinstance(it, dict) and it.get(field)
        }
        # Первое сохранение после появления accounts[]: на диске ещё legacy-креды
        # в самой секции — отдаём их строке с тем же логином.
        legacy_login = (section_on_disk.get("login") or "").strip()
        legacy_secret = section_on_disk.get(field)
        for item in items_in:
            if not isinstance(item, dict) or item.get(field):
                continue
            preserved = by_key.get(_account_key(item))
            if not preserved and legacy_secret and (item.get("login") or "").strip() == legacy_login:
                preserved = legacy_secret
            if preserved:
                item[field] = preserved
    return incoming


# ---------- Models ----------

class LeadsTechAccount(BaseModel):
    """Один аккаунт LeadsTech. Стата со всех аккаунтов складывается."""

    name: str = ""
    login: str = ""
    password: str = ""
    base_url: str = ""      # пусто → общий leadstech.base_url
    sub1: str = ""          # пусто → общий sub1 бренда
    enabled: bool = True


class LeadsTechSettings(BaseModel):
    base_url: str = "https://api.leads.tech"
    # login/password — legacy «один аккаунт»: читаются, только если accounts пуст
    login: str = ""
    password: str = ""
    accounts: List[LeadsTechAccount] = Field(default_factory=list)
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
    # Какое число цели пишем в столбец G «Заявки»:
    #   "visits"  — Целевые визиты (по умолчанию, как было),
    #   "reaches" — Достижения.
    zayavki_metric: str = "visits"

    @field_validator("zayavki_metric", mode="before")
    @classmethod
    def _normalize_zayavki_metric(cls, v: Any) -> str:
        return v if v in ("reaches", "visits") else "visits"


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
    # Переопределение подписей агрегатных колонок листа для нестандартных
    # шапок (ключ — метрика из metrics.METRICS, значение — подпись в строке 2).
    column_labels: Dict[str, str] = {}
    # Переопределение «названий внутри системы» (как метрика зовётся в нашем
    # UI: блок метрик отчёта, человекочитаемые формулы). Не влияет на таблицу.
    metric_names: Dict[str, str] = {}
    # Семантический движок метрик / генерация листов:
    # managed_formulas — формулы строк генерятся из реестра (metrics.py), а не
    # клонируются из строки 33 (недостающая месячная вкладка создаётся из
    # реестра всегда); cabinet_coeffs — {имя кабинета: коэффициент} для
    # бэкенд-расчёта Затрат и генерации листов (строка 1 живого листа главнее
    # при записи в существующие); manual_cabinets — ручные колонки расходов в
    # кабинетной зоне (AVITO, Google): бэкенд читает, никогда не пишет;
    # cabinets — имена кабинетов для шапки генерируемого листа; share_with —
    # кому расшарить таблицу, созданную с нуля.
    managed_formulas: bool = False
    cabinet_coeffs: Dict[str, float] = {}
    # Ручные поля расходов кабинетной зоны: [{label: подпись колонки в листе,
    # name: название в системе}]. Legacy-строки нормализуются валидатором.
    # Управляются из вкладки «Метрики» (+/✕); значения вводят люди в листе,
    # бэкенд читает и суммирует в «Затраты» с коэффициентом 1.
    manual_cabinets: List[Any] = []
    cabinets: List[str] = []

    @field_validator("manual_cabinets", mode="before")
    @classmethod
    def _normalize_manual_cabinets(cls, v):
        out = []
        for item in (v or []):
            if isinstance(item, str):
                label = item.strip()
                if label:
                    out.append({"label": label, "name": label, "target": "zatraty"})
            elif isinstance(item, dict):
                label = str(item.get("label") or "").strip()
                if label:
                    target = str(item.get("target") or "").strip()
                    out.append({"label": label,
                                "name": str(item.get("name") or "").strip() or label,
                                "target": target if target in ("zatraty", "prihod") else "zatraty"})
        return out
    share_with: List[str] = []
    # Область расходов кабинетов на листе (редактируется в UI). Пусто = AP..EZ.
    # Начало не может залезать в агрегатную зону — при чтении клампится до
    # первой колонки после реестра (AL, sheets_writer.cabinet_bounds).
    cabinet_start_col: str = ""
    cabinet_max_col: str = ""

    @field_validator("cabinet_start_col", "cabinet_max_col", mode="before")
    @classmethod
    def _normalize_col_letter(cls, v):
        s = str(v or "").strip().upper()
        if s and not (s.isalpha() and s.isascii() and len(s) <= 3):
            raise ValueError(f"колонка должна быть буквами A..ZZZ, получено {v!r}")
        return s

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


# Токены отмены активных ручных прогонов: token -> отменён ли.
# Кнопка «Отменить» в UI дёргает POST /api/report/cancel — сборка обрывается
# между стадиями, отменённый день никуда не записывается.
_CANCEL_TOKENS: Dict[str, bool] = {}


class ReportRequest(BaseModel):
    profile_id: str
    date: str  # YYYY-MM-DD
    sub1: Optional[str] = None  # None → берём sub1 профиля
    cancel_token: str = ""



class RunNowRequest(BaseModel):
    profile_id: Optional[str] = None  # None → прогнать все бренды


# ---------- App ----------


def _profiles_provider() -> List[tuple]:
    """[(profile_id, config, sub1, schedule), ...] для планировщика."""
    out: List[tuple] = []
    md = db.get_default_schedule()
    for pid in list_profile_ids():
        cfg = db.get_profile(pid)
        if cfg is None:
            continue
        sub1 = (cfg.get("sub1") or "").strip()
        schedule = _effective_schedule(cfg, md)
        out.append((pid, cfg, sub1, schedule))
    return out


scheduler: ReportScheduler = ReportScheduler(
    profiles_provider=_profiles_provider,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    migrate_files_to_db()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="sheetsStat", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """Статика отдаётся с Cache-Control: no-cache — браузер перепроверяет
    файл по ETag при каждом запросе (304, дёшево) и после деплоя сразу
    получает свежие app.js/app.css без Ctrl-F5."""
    response = await call_next(request)
    p = request.url.path
    if p == "/" or p.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/health")
def health():
    return {"ok": True}


# ---------- Profiles ----------

# Два бренда на одной таблице — почти всегда ошибка (копия бренда пишет в
# боевую таблицу оригинала); должно быть громко видно.
_shared_sheet_brands = db.shared_sheet_brands


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
        cfg = db.get_profile(pid) or {}
        st = sched_status.get(pid) or {}
        profiles.append({
            "id": pid,
            "name": cfg.get("name") or pid,
            "sub1": cfg.get("sub1") or "",
            "is_active": pid == active,
            "schedule": _effective_schedule(cfg, m.get("schedule") or {}),
            "sheets_enabled": bool((cfg.get("google_sheets") or {}).get("enabled")),
            "shared_sheet_with": _shared_sheet_brands(
                pid, (cfg.get("google_sheets") or {}).get("spreadsheet_id") or ""),
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

    db.delete_profile(pid)  # runs бренда остаются — история переживает удаление

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
    lt = data.get("leadstech") or {}
    lt_logins = [a.get("login") for a in (lt.get("accounts") or []) if a.get("login")]
    if not lt_logins and lt.get("login"):
        lt_logins = [lt["login"]]
    logger.info(
        "Profile %r saved: sub1=%s, LT logins=%s, AdsManager user=%s, Yandex user=%s, "
        "Metrika counter=%s token=%s, 8connect login=%s schemes=%s, Sheets enabled=%s id=%s",
        pid, data.get("sub1"),
        ", ".join(lt_logins) or "—", data["ads_manager"]["username"],
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


@app.get("/api/metrics")
def get_metrics_registry(profile_id: Optional[str] = None):
    """Полный семантический реестр метрик (metrics.METRICS) для UI.

    kind: base_service — запрашиваются из сервисов; computed — считаются по
    формуле; manual_cabinet — ручные кабинетные поля бренда (не из реестра).
    С `profile_id` подмешиваются per-brand «названия внутри системы»
    (`google_sheets.metric_names`): поле `system_name` и рендер формул.
    """
    import metrics as metrics_mod

    names_override: Dict[str, str] = {}
    gs_cfg: Dict[str, Any] = {}
    if profile_id:
        cfg = db.get_profile(profile_id) or {}
        gs_cfg = cfg.get("google_sheets") or {}
        names_override = gs_cfg.get("metric_names") or {}
    defaults = metrics_mod.default_system_names()
    out = [
        {
            "key": m.key,
            "kind": m.kind,
            "col": m.col,
            "label": m.label,
            "system_name": (names_override.get(m.key) or "").strip()
                           or defaults.get(m.key, m.key),
            "system_name_default": defaults.get(m.key, m.key),
            "occurrence": m.occurrence,
            "source": m.source,
            "formula": metrics_mod.human_formula(m.key, names_override),
            "expr": m.expr,
            "description": m.description,
        }
        for m in metrics_mod.METRICS.values()
        if m.kind != "date"
    ]
    if profile_id:
        # динамические ручные поля расходов бренда (кабинетная зона, коэф 1)
        from sheets_writer import manual_cabinet_entries
        for e in manual_cabinet_entries(gs_cfg):
            out.append({
                "key": f"manual_cabinet:{e['label']}",
                "kind": "manual_cabinet",
                "col": None,
                "label": e["label"],
                "system_name": e["name"],
                "system_name_default": e["label"],
                "occurrence": 1,
                "source": None,
                "formula": None,
                "expr": None,
                "target": e["target"],
                "description": ("Ручное доходное поле — суммируется в «Приход»"
                                if e["target"] == "prihod"
                                else "Ручное поле расходов — суммируется в «Затраты» (коэф 1)"),
            })
    return {"metrics": out}


@app.get("/api/sheets/columns")
def get_sheets_columns():
    """Legacy-алиас: 12 записываемых колонок (совместимость старого UI)."""
    from sheets_writer import AGG_COLUMNS, AGG_COLUMN_DESCRIPTIONS
    return {
        "columns": [
            {
                "key": key,
                "label": label,
                "occurrence": occurrence,
                "legacy_col": legacy_col,
                "description": AGG_COLUMN_DESCRIPTIONS.get(key, ""),
            }
            for key, (label, occurrence, legacy_col) in AGG_COLUMNS.items()
        ]
    }


class SheetsCreateRequest(BaseModel):
    force: bool = False  # создать, даже если spreadsheet_id уже заполнен


@app.post("/api/profiles/{pid}/sheets/create")
def create_profile_spreadsheet(pid: str, req: Optional[SheetsCreateRequest] = None):
    """Создаёт таблицу бренда с нуля из реестра метрик и прописывает её id."""
    import gspread
    import sheet_builder

    cfg = read_profile(pid)
    gs_cfg = cfg.get("google_sheets") or {}
    if gs_cfg.get("spreadsheet_id") and not (req and req.force):
        raise HTTPException(
            status_code=400,
            detail="у бренда уже есть spreadsheet_id — очисти его или передай force",
        )
    sa_path = gs_cfg.get("service_account_json_path") or "cfg/service_account.json"
    try:
        gc = gspread.service_account(filename=sa_path)
        result = sheet_builder.create_brand_spreadsheet(cfg, cfg.get("name") or pid, gc)
    except Exception as e:
        logger.error("sheets/create %r: %s", pid, e, exc_info=True)
        if "storage quota" in str(e).lower():
            sa_email = ""
            try:
                sa_email = json.loads(Path(sa_path).read_text())["client_email"]
            except Exception:
                pass
            raise HTTPException(status_code=500, detail=(
                "у сервисного аккаунта Google нулевая квота Drive — он не может "
                "создавать файлы. Создай пустую таблицу в своём Google Drive, "
                f"дай доступ редактора {sa_email or 'сервисному аккаунту'}, вставь "
                "ссылку в поле Spreadsheet ID и нажми «Разметить таблицу»"
            ))
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    # созданная таблица — managed: формулы из реестра
    cfg["google_sheets"]["spreadsheet_id"] = result["spreadsheet_id"]
    cfg["google_sheets"]["managed_formulas"] = True
    write_profile(pid, cfg)
    logger.info("Профиль %r: создана таблица %s", pid, result["spreadsheet_id"])
    return result


class SheetsInitRequest(BaseModel):
    # Ссылка/ID из поля формы: сохраняется в конфиг ДО разметки, чтобы кнопка
    # работала без отдельного «Сохранить настройки» (и размечалась именно
    # вставленная таблица, а не старая из БД).
    spreadsheet_id: str = ""


@app.post("/api/profiles/{pid}/sheets/init")
def init_profile_spreadsheet(pid: str, req: Optional[SheetsInitRequest] = None):
    """Размечает таблицу бренда: создаёт вкладку текущего месяца со всеми
    колонками/формулами/датами из реестра метрик и включает managed-режим.
    Путь для пустой таблицы, созданной пользователем и расшаренной на
    сервисный аккаунт (сам аккаунт файлы создавать не может — нулевая квота
    Drive). `spreadsheet_id` в теле (ссылка или ID) сохраняется в конфиг."""
    import gspread
    from datetime import date as _date

    import sheet_builder

    cfg = read_profile(pid)
    gs_cfg = cfg.setdefault("google_sheets", {})
    incoming = _extract_spreadsheet_id((req.spreadsheet_id if req else "") or "")
    if incoming:
        gs_cfg["spreadsheet_id"] = incoming
    if not gs_cfg.get("spreadsheet_id"):
        raise HTTPException(status_code=400, detail="сначала укажи Spreadsheet ID")
    try:
        gc = gspread.service_account(
            filename=gs_cfg.get("service_account_json_path") or "cfg/service_account.json")
        sh = gc.open_by_key(gs_cfg["spreadsheet_id"])
        ws, created = sheet_builder.ensure_month_worksheet(sh, _date.today(), cfg)
    except Exception as e:
        logger.error("sheets/init %r: %s", pid, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    cfg["google_sheets"]["managed_formulas"] = True
    write_profile(pid, cfg)
    logger.info("Профиль %r: таблица размечена (вкладка %r, created=%s)",
                pid, ws.title, created)
    return {
        "worksheet": ws.title,
        "created": created,
        "url": f"https://docs.google.com/spreadsheets/d/{gs_cfg['spreadsheet_id']}/edit",
    }


class VerifyRequest(BaseModel):
    profile_id: str
    tabs: List[str] = []


@app.post("/api/sheets/verify")
def verify_sheets(req: VerifyRequest):
    """Сверка бэкенд-формул реестра с фактическими значениями листа."""
    import gspread
    from datetime import date as _date

    import verify as verify_mod
    from sheets_writer import RU_MONTHS

    cfg = read_profile(req.profile_id)
    gs_cfg = cfg.get("google_sheets") or {}
    if not gs_cfg.get("spreadsheet_id"):
        raise HTTPException(status_code=400, detail="у бренда нет spreadsheet_id")
    tabs = req.tabs or [f"{RU_MONTHS[_date.today().month - 1]} {_date.today().year % 100:02d}"]
    try:
        gc = gspread.service_account(
            filename=gs_cfg.get("service_account_json_path") or "cfg/service_account.json")
        sh = gc.open_by_key(gs_cfg["spreadsheet_id"])
        results = []
        for tab in tabs:
            try:
                ws = sh.worksheet(tab)
            except Exception:
                results.append({"worksheet": tab, "error": "вкладка не найдена"})
                continue
            results.append(verify_mod.verify_worksheet(ws, cfg))
        return {"results": results}
    except Exception as e:
        logger.error("sheets/verify %r: %s", req.profile_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/api/schedule")
def get_schedule():
    return scheduler.status()


@app.post("/api/schedule/run-now")
async def schedule_run_now(req: Optional[RunNowRequest] = None):
    pid = (req.profile_id if req else None) or None
    return await scheduler.trigger_now(pid)


class CancelRequest(BaseModel):
    cancel_token: str


@app.post("/api/report/cancel")
def cancel_report(req: CancelRequest):
    """Отменяет активный ручной прогон: сборка обрывается между стадиями,
    день не записывается ни в БД, ни в Sheets."""
    token = (req.cancel_token or "").strip()
    if token in _CANCEL_TOKENS:
        _CANCEL_TOKENS[token] = True
        return {"ok": True}
    return {"ok": False, "detail": "нет активного прогона с таким токеном"}


@app.post("/api/report")
def run_report(req: ReportRequest):
    try:
        day = datetime.strptime(req.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="date должен быть в формате YYYY-MM-DD")

    config = read_profile(req.profile_id)  # 404 если профиля нет
    sub1 = (req.sub1 or "").strip() or (config.get("sub1") or "").strip()

    token = (req.cancel_token or "").strip()
    cancel_check = None
    if token:
        _CANCEL_TOKENS[token] = False

        def cancel_check():
            if _CANCEL_TOKENS.get(token):
                raise ReportCancelled()

    started_at = datetime.now(SAMARA_TZ).isoformat(timespec="seconds")
    try:
        report = build_report(config, day, sub1, cancel_check=cancel_check)
    except ReportCancelled:
        _CANCEL_TOKENS.pop(token, None)
        logger.info("Report %s/%s отменён пользователем — ничего не записано",
                    req.profile_id, req.date)
        return {"cancelled": True, "date": req.date, "profile_id": req.profile_id}
    except Exception as e:
        logger.error("Report error: %s", e, exc_info=True)
        # Упавший ручной прогон тоже попадает в историю/last_run;
        # report=None не затирает последний удачный отчёт за эту дату.
        db.save_run(
            req.profile_id, day.isoformat(), sub1,
            ok=False, trigger="manual", error=f"{type(e).__name__}: {e}",
            started_at=started_at,
            finished_at=datetime.now(SAMARA_TZ).isoformat(timespec="seconds"),
            report=None,
        )
        _CANCEL_TOKENS.pop(token, None)
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    # Вкладка месяца создана этим прогоном → бренд переводится на managed.
    if (report.get("google_sheets") or {}).get("created"):
        try:
            db.promote_to_managed(req.profile_id)
        except Exception as e:
            logger.error("promote_to_managed %r: %s", req.profile_id, e, exc_info=True)

    # Если Ads Manager вернул 0 кабинетов — это warning, но ответ всё равно
    # сохраняем (удобно видеть, что LeadsTech всё равно отдал статистику).
    if report["cabinet_count"] == 0:
        errs = (report.get("ads_manager") or {}).get("errors", [])
        if errs:
            report["warning"] = f"Ads Manager: {errs[0].get('error')}"
        else:
            report["warning"] = f"Нет кабинетов с label={sub1!r} у пользователя Ads Manager."

    # Общая таблица с другим брендом — почти всегда ошибка конфигурации
    shared = _shared_sheet_brands(
        req.profile_id, (config.get("google_sheets") or {}).get("spreadsheet_id") or "")
    if shared:
        gs = report.get("google_sheets")
        msg = f"эту таблицу также использует бренд: {', '.join(shared)} — проверь spreadsheet_id"
        if isinstance(gs, dict):
            gs.setdefault("header_warnings", []).append(msg)
        report["warning"] = (report.get("warning") + " · " if report.get("warning") else "") + msg

    # Сохраняем в БД: upsert по (profile_id, date) — повторный прогон
    # перезаписывает запись (sub1 у профилей может совпадать, ключ — по pid).
    db.save_run(
        req.profile_id, day.isoformat(), sub1,
        ok=True, trigger="manual",
        google_sheets_error=(report.get("google_sheets") or {}).get("error"),
        cabinet_count=report.get("cabinet_count"),
        started_at=started_at,
        finished_at=datetime.now(SAMARA_TZ).isoformat(timespec="seconds"),
        report=report,
    )
    report["_saved"] = {"profile_id": req.profile_id, "date": day.isoformat()}
    _CANCEL_TOKENS.pop(token, None)

    return report


@app.get("/api/reports")
def list_reports(profile_id: Optional[str] = None):
    return {"items": db.list_runs(profile_id=profile_id or None)}


@app.get("/api/reports/{pid}/{run_date}")
def get_report(pid: str, run_date: str):
    _validate_pid(pid)
    try:
        datetime.strptime(run_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="дата должна быть YYYY-MM-DD")
    report = db.get_run_report(pid, run_date)
    if report is None:
        raise HTTPException(status_code=404, detail="report not found")
    return report


# ---------- Static (должен идти ПОСЛЕ api-routes) ----------

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
