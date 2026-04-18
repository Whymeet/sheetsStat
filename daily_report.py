"""CLI-обёртка вокруг core.build_report.

Usage:
    python3 daily_report.py --date 2026-04-17 --sub1 kub

Пишет JSON в output/<date>_<sub1>.json и дублирует в stdout.
Основная логика живёт в core.py — её же использует и веб-морда (app.py).
"""
import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from config_loader import load_lt_vk_config
from core import build_report


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("daily_report")


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"Ожидал YYYY-MM-DD, получил {s!r}") from e


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", type=_parse_date, required=True, help="День отчёта, YYYY-MM-DD")
    parser.add_argument("--sub1", default="kub", help="sub1 / leadstechLabel (default: kub)")
    parser.add_argument("--config", default="cfg/lt_vk_config.json")
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()

    config = load_lt_vk_config(args.config)
    report = build_report(config, args.date, args.sub1)

    if report["cabinet_count"] == 0:
        logger.error(
            "В конфиге нет ни одного кабинета с leadstechLabel=%r. Доступные: %s",
            args.sub1, report.get("available_labels", []),
        )
        return 2

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{args.date.isoformat()}_{args.sub1}.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("✅ Сохранено: %s", out_file)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
