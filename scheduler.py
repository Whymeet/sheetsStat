"""Автоматическая генерация дневного отчёта по расписанию.

Задача: каждый день в указанное время по Самаре собирать отчёт за
предыдущий календарный день (Samara-time) и класть его туда же, куда
пишет ручной `POST /api/report` — в `output/{date}_{sub1}.json` и в
Google Sheets.

Конфигурация живёт в `cfg/lt_vk_config.json` в блоке:

    "schedule": {
      "enabled": false,
      "time": "09:00",      // HH:MM по Europe/Samara (UTC+4)
      "sub1": "kub"
    }

Используем in-process AsyncIOScheduler: стартует вместе с uvicorn в
FastAPI lifespan, при сохранении конфига job перестраивается на лету.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from core import build_report


logger = logging.getLogger("sheetsstat.scheduler")

SAMARA_TZ = ZoneInfo("Europe/Samara")
JOB_ID = "daily_report"


class ReportScheduler:
    """Оборачивает AsyncIOScheduler, хранит статус последнего/следующего запуска."""

    def __init__(
        self,
        config_loader: Callable[[], Dict[str, Any]],
        output_dir: Path,
    ):
        self._config_loader = config_loader
        self._output_dir = output_dir
        self._scheduler = AsyncIOScheduler(timezone=SAMARA_TZ)
        self._last_run: Optional[Dict[str, Any]] = None

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
        self.reload()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def reload(self) -> None:
        """Перечитывает `schedule` из конфига и (пере)регистрирует job."""
        try:
            config = self._config_loader()
        except Exception as e:
            logger.warning("scheduler.reload: не смог загрузить конфиг: %s", e)
            self._remove_job()
            return

        sched_cfg = (config or {}).get("schedule") or {}
        enabled = bool(sched_cfg.get("enabled"))
        time_str = (sched_cfg.get("time") or "").strip()
        sub1 = (sched_cfg.get("sub1") or "kub").strip()

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
            kwargs={"sub1": sub1},
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info(
            "scheduler: job зарегистрирован — %02d:%02d Samara, sub1=%s",
            hour, minute, sub1,
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

    async def trigger_now(self, sub1: str) -> Dict[str, Any]:
        """Ручной триггер для тестирования/фронта. Не возвращает до конца работы."""
        return await self._run_job(sub1=sub1, manual=True)

    # ------------------------------------------------------------------

    def _remove_job(self) -> None:
        if self._scheduler.get_job(JOB_ID):
            self._scheduler.remove_job(JOB_ID)

    async def _run_job(self, sub1: str, manual: bool = False) -> Dict[str, Any]:
        target_day = _yesterday_samara()
        label = "manual" if manual else "cron"
        logger.info("scheduler[%s]: build_report for %s sub1=%s", label, target_day, sub1)

        started_at = datetime.now(SAMARA_TZ).isoformat(timespec="seconds")
        try:
            config = self._config_loader()
            report = build_report(config, target_day, sub1)
            out_file = self._output_dir / f"{target_day.isoformat()}_{sub1}.json"
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            finished_at = datetime.now(SAMARA_TZ).isoformat(timespec="seconds")
            self._last_run = {
                "started_at": started_at,
                "finished_at": finished_at,
                "date": target_day.isoformat(),
                "sub1": sub1,
                "trigger": label,
                "ok": True,
                "saved_to": out_file.name,
                "cabinet_count": report.get("cabinet_count"),
                "google_sheets_error": (report.get("google_sheets") or {}).get("error"),
            }
            logger.info("scheduler[%s]: ok, saved %s", label, out_file.name)
            return self._last_run
        except Exception as e:
            finished_at = datetime.now(SAMARA_TZ).isoformat(timespec="seconds")
            self._last_run = {
                "started_at": started_at,
                "finished_at": finished_at,
                "date": target_day.isoformat(),
                "sub1": sub1,
                "trigger": label,
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
            }
            logger.error("scheduler[%s]: build_report failed: %s", label, e, exc_info=True)
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
