"""Автоматическая генерация дневных отчётов по расписанию.

Задача: каждый день в одно общее время по Самаре собирать отчёты за
предыдущий календарный день (Samara-time) по ВСЕМ профилям и класть их
туда же, куда пишет ручной `POST /api/report` — в
`output/{date}_{sub1}__{profile_id}.json` и в Google Sheets.

Общее расписание живёт в манифесте `cfg/profiles.json`:

    "schedule": { "enabled": false, "time": "09:00" }   // HH:MM Europe/Samara

В заданное время по очереди прогоняются все профили (каждый со своим
конфигом/таблицей/sub1). Падение одного профиля не валит остальные.

Используем in-process AsyncIOScheduler: стартует вместе с uvicorn в
FastAPI lifespan, при изменении расписания job перестраивается на лету.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core import build_report


logger = logging.getLogger("sheetsstat.scheduler")

SAMARA_TZ = ZoneInfo("Europe/Samara")
JOB_ID = "daily_report"
WATCHER_JOB_ID = "config_watcher"
WATCHER_INTERVAL_SEC = 10


class ReportScheduler:
    """Оборачивает AsyncIOScheduler, хранит статус последнего/следующего запуска."""

    def __init__(
        self,
        schedule_provider: Callable[[], Dict[str, Any]],
        profiles_provider: Callable[[], List[Tuple[str, Dict[str, Any], str]]],
        output_dir: Path,
    ):
        # schedule_provider() -> {"enabled": bool, "time": "HH:MM"}
        # profiles_provider() -> [(profile_id, config, sub1), ...]
        self._schedule_provider = schedule_provider
        self._profiles_provider = profiles_provider
        self._output_dir = output_dir
        self._scheduler = AsyncIOScheduler(timezone=SAMARA_TZ)
        self._last_run: Optional[Dict[str, Any]] = None
        self._last_schedule_snapshot: Optional[Dict[str, Any]] = None

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
        self.reload()
        self._scheduler.add_job(
            self._watch_config,
            trigger=IntervalTrigger(seconds=WATCHER_INTERVAL_SEC),
            id=WATCHER_JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def reload(self) -> None:
        """Перечитывает общее расписание и (пере)регистрирует job."""
        try:
            sched_cfg = self._schedule_provider() or {}
        except Exception as e:
            logger.warning("scheduler.reload: не смог прочитать расписание: %s", e)
            self._remove_job()
            return

        self._last_schedule_snapshot = dict(sched_cfg)
        enabled = bool(sched_cfg.get("enabled"))
        time_str = (sched_cfg.get("time") or "").strip()

        if not enabled:
            logger.info("scheduler: выключен (schedule.enabled = false)")
            self._remove_job()
            return

        hour, minute = _parse_hhmm(time_str)
        if hour is None:
            logger.warning("scheduler: некорректное schedule.time=%r, job не создан", time_str)
            self._remove_job()
            return

        trigger = CronTrigger(hour=hour, minute=minute, timezone=SAMARA_TZ)
        self._scheduler.add_job(
            self._run_job,
            trigger=trigger,
            id=JOB_ID,
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info(
            "scheduler: job зарегистрирован — %02d:%02d Samara, все профили",
            hour, minute,
        )

    def status(self) -> Dict[str, Any]:
        job = self._scheduler.get_job(JOB_ID)
        next_run_iso = None
        if job and job.next_run_time:
            next_run_iso = job.next_run_time.astimezone(SAMARA_TZ).isoformat(timespec="minutes")
        return {
            "enabled": job is not None,
            "next_run": next_run_iso,
            "timezone": "Europe/Samara",
            "last_run": self._last_run,
        }

    async def trigger_now(self) -> Dict[str, Any]:
        """Ручной триггер для тестирования/фронта. Не возвращает до конца работы."""
        return await self._run_job(manual=True)

    # ------------------------------------------------------------------

    def _remove_job(self) -> None:
        if self._scheduler.get_job(JOB_ID):
            self._scheduler.remove_job(JOB_ID)

    def _watch_config(self) -> None:
        try:
            sched_cfg = self._schedule_provider() or {}
        except Exception as e:
            logger.warning("scheduler.watcher: не смог прочитать расписание: %s", e)
            return
        if sched_cfg == self._last_schedule_snapshot:
            return
        logger.info(
            "scheduler: обнаружено изменение расписания (%s -> %s), перерегистрирую job",
            self._last_schedule_snapshot, sched_cfg,
        )
        self.reload()

    async def _run_job(self, manual: bool = False) -> Dict[str, Any]:
        target_day = _yesterday_samara()
        label = "manual" if manual else "cron"
        started_at = datetime.now(SAMARA_TZ).isoformat(timespec="seconds")

        try:
            profiles = self._profiles_provider()
        except Exception as e:
            logger.error("scheduler[%s]: не смог получить профили: %s", label, e, exc_info=True)
            profiles = []

        logger.info("scheduler[%s]: прогон %d профилей за %s", label, len(profiles), target_day)

        results: List[Dict[str, Any]] = []
        for pid, config, sub1 in profiles:
            try:
                report = build_report(config, target_day, sub1)
                out_file = self._output_dir / f"{target_day.isoformat()}_{sub1}__{pid}.json"
                out_file.parent.mkdir(parents=True, exist_ok=True)
                out_file.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                results.append({
                    "profile_id": pid,
                    "sub1": sub1,
                    "ok": True,
                    "saved_to": out_file.name,
                    "cabinet_count": report.get("cabinet_count"),
                    "google_sheets_error": (report.get("google_sheets") or {}).get("error"),
                })
                logger.info("scheduler[%s]: профиль %r ok, saved %s", label, pid, out_file.name)
            except Exception as e:
                results.append({
                    "profile_id": pid,
                    "sub1": sub1,
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                })
                logger.error("scheduler[%s]: профиль %r упал: %s", label, pid, e, exc_info=True)

        finished_at = datetime.now(SAMARA_TZ).isoformat(timespec="seconds")
        self._last_run = {
            "started_at": started_at,
            "finished_at": finished_at,
            "date": target_day.isoformat(),
            "trigger": label,
            "ok": bool(results) and all(r["ok"] for r in results),
            "count": len(results),
            "results": results,
        }
        return self._last_run


def _yesterday_samara() -> date:
    today_samara = datetime.now(SAMARA_TZ).date()
    return today_samara - timedelta(days=1)


def _parse_hhmm(value: str) -> tuple:
    """Возвращает (hour, minute) или (None, None) при невалидном вводе."""
    if not value or ":" not in value:
        return (None, None)
    try:
        h_str, m_str = value.split(":", 1)
        h = int(h_str)
        m = int(m_str)
    except (TypeError, ValueError):
        return (None, None)
    if 0 <= h <= 23 and 0 <= m <= 59:
        return (h, m)
    return (None, None)
