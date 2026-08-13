"""SQLite-хранилище sheetsStat: профили брендов + история прогонов.

Заменяет файловое хранение cfg/profiles/*.json + cfg/profiles.json (манифест)
и output/*.json (отчёты). Старые файлы после одноразовой миграции
(app.migrate_files_to_db) остаются на диске как read-only архив.

Схема (PRAGMA user_version=1):
- profiles(id, config JSON, sort_order, created_at, updated_at) — весь профиль
  одним JSON-блобом, с секретами и schedule внутри (код везде работает с dict,
  нормализация в колонки дала бы только рассинхрон).
- app_state(key, value) — 'active_id' и 'default_schedule' (бывший манифест).
- runs(profile_id, date, ...) PK (profile_id, date) — история прогонов, upsert:
  повторный прогон той же (даты, бренда) перезаписывает запись. Упавший прогон
  (report=None) обновляет статусные поля, но НЕ затирает последний удачный
  отчёт (COALESCE) — раньше упавший прогон просто не писал файл.
- eightconnect_raw(date, data) — дебаг-дампы сырого ответа 8connect
  (перезаписываются последним прогоном любого бренда за дату, как и файлы).

Конкурентность: соединение на операцию + WAL + busy_timeout. Пишут два
контекста (uvicorn threadpool и event loop планировщика), но прогоны и так
сериализованы scheduler._run_lock, а остальные записи — короткие транзакции.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("sheetsstat.db")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SHEETSSTAT_DB", "") or (BASE_DIR / "data" / "sheetsstat.db"))

_SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS profiles (
    id          TEXT PRIMARY KEY,
    config      TEXT NOT NULL CHECK (json_valid(config)),
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    profile_id          TEXT NOT NULL,
    date                TEXT NOT NULL,
    sub1                TEXT NOT NULL DEFAULT '',
    ok                  INTEGER NOT NULL,
    run_trigger         TEXT NOT NULL,
    error               TEXT,
    google_sheets_error TEXT,
    cabinet_count       INTEGER,
    started_at          TEXT,
    finished_at         TEXT,
    report              TEXT CHECK (report IS NULL OR json_valid(report)),
    PRIMARY KEY (profile_id, date)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS runs_by_date ON runs (date DESC);

CREATE TABLE IF NOT EXISTS eightconnect_raw (
    date       TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
) WITHOUT ROWID;
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Создаёт файл/схему при первом старте. Идемпотентно."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")  # персистится в файле БД
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < _SCHEMA_VERSION:
            conn.executescript(_DDL)
            conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            logger.info("db: схема инициализирована (user_version=%d), файл %s",
                        _SCHEMA_VERSION, DB_PATH)


def is_empty() -> bool:
    """True, пока в БД нет ни одного профиля — гейт одноразовой миграции."""
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0


# ============================== profiles ==============================

def list_profile_ids() -> List[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT id FROM profiles ORDER BY sort_order, id").fetchall()
    return [r["id"] for r in rows]


def get_profile(pid: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute("SELECT config FROM profiles WHERE id=?", (pid,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["config"])
    except json.JSONDecodeError:  # CHECK json_valid не должен такое пропустить
        logger.error("db: битый config у профиля %r", pid)
        return None


def upsert_profile(pid: str, cfg: Dict[str, Any]) -> None:
    payload = json.dumps(cfg, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO profiles (id, config, sort_order)
            VALUES (?, ?, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM profiles))
            ON CONFLICT(id) DO UPDATE SET
                config = excluded.config,
                updated_at = datetime('now')
            """,
            (pid, payload),
        )


def delete_profile(pid: str) -> None:
    """Удаляет профиль. Его runs остаются — история переживает удаление бренда."""
    with _connect() as conn:
        conn.execute("DELETE FROM profiles WHERE id=?", (pid,))


def _get_state(key: str) -> Optional[Any]:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row is not None else None


def _set_state(key: str, value: Any) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


def get_active_id() -> Optional[str]:
    return _get_state("active_id")


def set_active_id(pid: Optional[str]) -> None:
    _set_state("active_id", pid)


def get_default_schedule() -> Dict[str, Any]:
    return _get_state("default_schedule") or {"enabled": False, "time": "09:00"}


def set_default_schedule(schedule: Dict[str, Any]) -> None:
    _set_state("default_schedule", schedule)


def shared_sheet_brands(pid: str, spreadsheet_id: str) -> List[str]:
    """Имена ДРУГИХ профилей с тем же spreadsheet_id (обычно ошибка конфига)."""
    if not spreadsheet_id:
        return []
    out = []
    for other in list_profile_ids():
        if other == pid:
            continue
        ocfg = get_profile(other) or {}
        if (ocfg.get("google_sheets") or {}).get("spreadsheet_id") == spreadsheet_id:
            out.append(ocfg.get("name") or other)
    return out


# ============================== runs ==============================

def save_run(
    profile_id: str,
    date: str,
    sub1: str,
    *,
    ok: bool,
    trigger: str,
    error: Optional[str] = None,
    google_sheets_error: Optional[str] = None,
    cabinet_count: Optional[int] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    report: Optional[Dict[str, Any]] = None,
) -> None:
    """Единая точка записи прогона (app POST /api/report и scheduler).

    Upsert по (profile_id, date): повторный прогон перезаписывает запись.
    report=None (упавший прогон) не затирает последний удачный отчёт.
    """
    report_json = json.dumps(report, ensure_ascii=False) if report is not None else None
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO runs (profile_id, date, sub1, ok, run_trigger, error,
                              google_sheets_error, cabinet_count, started_at,
                              finished_at, report)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id, date) DO UPDATE SET
                sub1 = excluded.sub1,
                ok = excluded.ok,
                run_trigger = excluded.run_trigger,
                error = excluded.error,
                google_sheets_error = excluded.google_sheets_error,
                cabinet_count = excluded.cabinet_count,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                report = COALESCE(excluded.report, runs.report)
            """,
            (profile_id, date, sub1 or "", int(bool(ok)), trigger, error,
             google_sheets_error, cabinet_count, started_at, finished_at,
             report_json),
        )


def _run_meta(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "profile_id": row["profile_id"],
        "date": row["date"],
        "sub1": row["sub1"],
        "ok": bool(row["ok"]),
        "trigger": row["run_trigger"],
        "error": row["error"],
        "google_sheets_error": row["google_sheets_error"],
        "cabinet_count": row["cabinet_count"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "has_report": bool(row["has_report"]),
    }


_RUN_META_COLS = (
    "profile_id, date, sub1, ok, run_trigger, error, google_sheets_error, "
    "cabinet_count, started_at, finished_at, (report IS NOT NULL) AS has_report"
)


def list_runs(profile_id: Optional[str] = None, limit: int = 1000) -> List[Dict[str, Any]]:
    """Метаданные прогонов (без тел отчётов), новые сверху."""
    with _connect() as conn:
        if profile_id:
            rows = conn.execute(
                f"SELECT {_RUN_META_COLS} FROM runs WHERE profile_id=? "
                "ORDER BY date DESC, finished_at DESC LIMIT ?",
                (profile_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_RUN_META_COLS} FROM runs "
                "ORDER BY date DESC, finished_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_run_meta(r) for r in rows]


def get_run_report(profile_id: str, date: str) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT report FROM runs WHERE profile_id=? AND date=?",
            (profile_id, date),
        ).fetchone()
    if row is None or row["report"] is None:
        return None
    return json.loads(row["report"])


def get_last_runs() -> Dict[str, Dict[str, Any]]:
    """Последняя попытка по каждому бренду (форма — как бывший scheduler._last_runs)."""
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_RUN_META_COLS} FROM runs
            WHERE (profile_id, COALESCE(finished_at, '')) IN (
                SELECT profile_id, MAX(COALESCE(finished_at, ''))
                FROM runs GROUP BY profile_id
            )
            """
        ).fetchall()
    return {r["profile_id"]: _run_meta(r) for r in rows}


# ============================== 8connect raw ==============================

def save_eightconnect_raw(date: str, rows: Any) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO eightconnect_raw (date, data) VALUES (?, ?) "
            "ON CONFLICT(date) DO UPDATE SET data = excluded.data, "
            "updated_at = datetime('now')",
            (date, json.dumps(rows, ensure_ascii=False)),
        )


# ============================== одноразовый импорт ==============================

def import_snapshot(
    profiles: List[Tuple[str, Dict[str, Any]]],
    active_id: Optional[str],
    default_schedule: Dict[str, Any],
    runs: List[Dict[str, Any]],
    eightconnect: List[Tuple[str, Any]],
) -> None:
    """Одноразовый импорт файлового снапшота ОДНОЙ транзакцией.

    Крэш посередине → rollback → полный повтор при следующем старте
    (гейт is_empty() останется True).
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for order, (pid, cfg) in enumerate(profiles):
            conn.execute(
                "INSERT INTO profiles (id, config, sort_order) VALUES (?, ?, ?)",
                (pid, json.dumps(cfg, ensure_ascii=False), order),
            )
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES ('active_id', ?)",
            (json.dumps(active_id),),
        )
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES ('default_schedule', ?)",
            (json.dumps(default_schedule, ensure_ascii=False),),
        )
        for r in runs:
            conn.execute(
                """
                INSERT INTO runs (profile_id, date, sub1, ok, run_trigger, error,
                                  google_sheets_error, cabinet_count, started_at,
                                  finished_at, report)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (r["profile_id"], r["date"], r.get("sub1") or "",
                 int(bool(r.get("ok", True))), r.get("trigger", "import"),
                 r.get("error"), r.get("google_sheets_error"),
                 r.get("cabinet_count"), r.get("started_at"), r.get("finished_at"),
                 json.dumps(r["report"], ensure_ascii=False)
                 if r.get("report") is not None else None),
            )
        for date, rows in eightconnect:
            conn.execute(
                "INSERT INTO eightconnect_raw (date, data) VALUES (?, ?)",
                (date, json.dumps(rows, ensure_ascii=False)),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
