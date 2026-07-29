"""Автоматическая генерация дневных отчётов по расписанию.

Задача: для каждого бренда (профиля) в его собственное время по Самаре
собирать отчёт за предыдущий календарный день (Samara-time) и класть его
туда же, куда пишет ручной `POST /api/report` — в
`output/{date}_{sub1}__{profile_id}.json` и в Google Sheets.

Расписание у каждого бренда своё и живёт прямо в файле профиля
`cfg/profiles/<id>.json`:

    "schedule": { "enabled": false, "time": "09:00" }   // HH:MM Europe/Samara

На каждый бренд с `schedule.enabled=true` регистрируется отдельный cron-job
`report__<pid>`. Падение одного бренда не влияет на остальные.

Используем in-process AsyncIOScheduler: стартует вместе с uvicorn в
FastAPI lifespan, при изменении любого расписания job'ы перестраиваются на лету.
"""
from __future__ import annotations

import asyncio
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
JOB_PREFIX = "report__"          # один job на бренд: report__<pid>
WATCHER_JOB_ID = "config_watcher"
WATCHER_INTERVAL_SEC = 10

# profiles_provider() -> [(profile_id, config, sub1, schedule), ...]
ProfileTuple = Tuple[str, Dict[str, Any], str, Dict[str, Any]]


class ReportScheduler:
    """Оборачивает AsyncIOScheduler: по одному cron-job на бренд, статусы per-brand."""

    def __init__(
        self,
        profiles_provider: Callable[[], List[ProfileTuple]],
        output_dir: Path,
    ):
        self._profiles_provider = profiles_provider
        self._output_dir = output_dir
        self._scheduler = AsyncIOScheduler(timezone=SAMARA_TZ)
        # last_run по каждому бренду: {pid: run_info}
        self._last_runs: Dict[str, Dict[str, Any]] = {}
        # снапшот расписаний {pid: schedule} для watcher'а
        self._last_snapshot: Optional[Dict[str, Dict[str, Any]]] = None
        # build_report ушёл в отдельный поток, поэтому бренды с одинаковым временем
        # (или ручной прогон поверх cron'а) могли бы собираться параллельно. Сборку
        # держим строго по одному: sheets_writer кэширует шаблон формул в модульном
        # словаре, да и внешние API незачем дёргать в несколько потоков.
        self._run_lock = asyncio.Lock()

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
        """Перечитывает per-brand расписания и (пере)регистрирует cron-job'ы."""
        try:
            profiles = self._profiles_provider() or []
        except Exception as e:
            logger.warning("scheduler.reload: не смог прочитать профили: %s", e)
            return

        # Желаемый набор job'ов: pid -> (hour, minute)
        desired: Dict[str, Tuple[int, int]] = {}
        snapshot: Dict[str, Dict[str, Any]] = {}
        for pid, _config, _sub1, schedule in profiles:
            schedule = schedule or {}
            snapshot[pid] = dict(schedule)
            if not bool(schedule.get("enabled")):
                continue
            hour, minute = _parse_hhmm((schedule.get("time") or "").strip())
            if hour is None:
                logger.warning(
                    "scheduler: профиль %r — некорректное time=%r, job не создан",
                    pid, schedule.get("time"),
                )
                continue
            desired[pid] = (hour, minute)

        self._last_snapshot = snapshot

        # Удаляем job'ы брендов, которых больше нет в desired (выключены/удалены).
        for job in self._scheduler.get_jobs():
            if job.id.startswith(JOB_PREFIX) and job.id[len(JOB_PREFIX):] not in desired:
                self._scheduler.remove_job(job.id)

        # Добавляем/обновляем job'ы активных брендов.
        for pid, (hour, minute) in desired.items():
            self._scheduler.add_job(
                self._run_profile_job,
                trigger=CronTrigger(hour=hour, minute=minute, timezone=SAMARA_TZ),
                id=f"{JOB_PREFIX}{pid}",
                args=[pid],
                replace_existing=True,
                coalesce=True,
                misfire_grace_time=3600,
            )

        logger.info(
            "scheduler: активных расписаний — %d (%s)",
            len(desired),
            ", ".join(f"{pid}@{h:02d}:{m:02d}" for pid, (h, m) in desired.items()) or "нет",
        )

    def status(self) -> Dict[str, Any]:
        """Статус по каждому бренду + сводка для шапки."""
        per_profile: Dict[str, Any] = {}
        next_runs: List[datetime] = []
        for job in self._scheduler.get_jobs():
            if not job.id.startswith(JOB_PREFIX):
                continue
            pid = job.id[len(JOB_PREFIX):]
            nrt = job.next_run_time
            if nrt:
                next_runs.append(nrt)
            per_profile[pid] = {
                "enabled": True,
                "next_run": nrt.astimezone(SAMARA_TZ).isoformat(timespec="minutes") if nrt else None,
                "last_run": self._last_runs.get(pid),
            }
        # Бренды без активного job'а, но с историей последнего запуска.
        for pid, lr in self._last_runs.items():
            per_profile.setdefault(pid, {"enabled": False, "next_run": None, "last_run": lr})

        next_run_iso = None
        if next_runs:
            next_run_iso = min(next_runs).astimezone(SAMARA_TZ).isoformat(timespec="minutes")

        return {
            "enabled": bool(next_runs),
            "next_run": next_run_iso,
            "timezone": "Europe/Samara",
            "profiles": per_profile,
        }

    async def trigger_now(self, pid: Optional[str] = None) -> Dict[str, Any]:
        """Ручной триггер. С pid — один бренд, без — все. Ждёт завершения работы."""
        try:
            profiles = self._profiles_provider() or []
        except Exception as e:
            logger.error("scheduler.trigger_now: не смог получить профили: %s", e, exc_info=True)
            profiles = []

        target_day = _yesterday_samara()

        if pid is not None:
            match = next((t for t in profiles if t[0] == pid), None)
            if match is None:
                return {"date": target_day.isoformat(), "trigger": "manual",
                        "ok": False, "results": [], "error": f"profile not found: {pid}"}
            res = await self._run_one(match[0], match[1], match[2], target_day, manual=True)
            return {
                "date": target_day.isoformat(),
                "trigger": "manual",
                "ok": bool(res.get("ok")),
                "count": 1,
                "results": [res],
            }

        results: List[Dict[str, Any]] = []
        for p_pid, config, sub1, _schedule in profiles:
            results.append(await self._run_one(p_pid, config, sub1, target_day, manual=True))
        return {
            "date": target_day.isoformat(),
            "trigger": "manual",
            "ok": bool(results) and all(r["ok"] for r in results),
            "count": len(results),
            "results": results,
        }

    # ------------------------------------------------------------------

    def _watch_config(self) -> None:
        try:
            profiles = self._profiles_provider() or []
        except Exception as e:
            logger.warning("scheduler.watcher: не смог прочитать профили: %s", e)
            return
        snapshot = {pid: dict(schedule or {}) for pid, _c, _s, schedule in profiles}
        if snapshot == self._last_snapshot:
            return
        logger.info("scheduler: обнаружено изменение расписаний, перерегистрирую job'ы")
        self.reload()

    async def _run_profile_job(self, pid: str) -> Optional[Dict[str, Any]]:
        """Cron-entrypoint одного бренда: тянет свежий конфиг и собирает отчёт."""
        try:
            profiles = self._profiles_provider() or []
        except Exception as e:
            logger.error("scheduler: профиль %r — не смог получить конфиг: %s", pid, e, exc_info=True)
            return None
        match = next((t for t in profiles if t[0] == pid), None)
        if match is None:
            logger.warning("scheduler: профиль %r исчез к моменту запуска job'а", pid)
            return None
        return await self._run_one(match[0], match[1], match[2], _yesterday_samara(), manual=False)

    async def _run_one(
        self, pid: str, config: Dict[str, Any], sub1: str, target_day: date, manual: bool,
    ) -> Dict[str, Any]:
        """Собирает один бренд, пишет output + Sheets, сохраняет last_run[pid]."""
        label = "manual" if manual else "cron"
        started_at = datetime.now(SAMARA_TZ).isoformat(timespec="seconds")
        try:
            # build_report — синхронный и ходит по сети минуты; на event loop'е он
            # заморозил бы весь uvicorn (морда ловит NetworkError, healthcheck падает).
            async with self._run_lock:
                report = await asyncio.to_thread(build_report, config, target_day, sub1)
            out_file = self._output_dir / f"{target_day.isoformat()}_{sub1}__{pid}.json"
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            result = {
                "profile_id": pid,
                "sub1": sub1,
                "ok": True,
                "saved_to": out_file.name,
                "cabinet_count": report.get("cabinet_count"),
                "google_sheets_error": (report.get("google_sheets") or {}).get("error"),
            }
            logger.info("scheduler[%s]: профиль %r ok, saved %s", label, pid, out_file.name)
        except Exception as e:
            result = {
                "profile_id": pid,
                "sub1": sub1,
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
            }
            logger.error("scheduler[%s]: профиль %r упал: %s", label, pid, e, exc_info=True)

        result["started_at"] = started_at
        result["finished_at"] = datetime.now(SAMARA_TZ).isoformat(timespec="seconds")
        result["date"] = target_day.isoformat()
        result["trigger"] = label
        self._last_runs[pid] = result
        return result


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
