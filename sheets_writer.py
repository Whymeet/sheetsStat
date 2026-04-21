"""Запись дневного отчёта build_report в Google Sheets.

Таблица — «Копия kubyshka-zaim.ru». На каждый месяц — отдельная вкладка
(«Апрель 26», «Май 26», …). Столбец A — даты `dd.mm.yyyy`.

На строке даты пишем агрегаты в фиксированных колонках:
    C  — Приход            = leadstech.sum (sumwebmaster)
    E  — Клики ЛТ          = leadstech.clicks
    F  — Метрика визиты    = yandex_metrika.visits
    G  — Заявки с сайта    = visits у цели Zayvka
    AI — Переходы уники    = leadstech.hosts

В `AR2:CL2` лежат названия рекламных кабинетов. По каждому spent'у из отчёта
ищем свою колонку и пишем spent в `{col}{date_row}`. Кабинеты, которых нет
в шапке, добавляем стопкой начиная с A37 (A=имя, B=spent) — «чтобы не пропали».
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger("sheetsstat.sheets_writer")


RU_MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

COL_PRIHOD    = "C"
COL_ZATRATY   = "D"
COL_CLICKS_LT = "E"
COL_METRIKA_V = "F"
COL_ZAYAVKI   = "G"
COL_PEREHODY  = "AI"

# 8connect: расход пишем в AC (уже слагаемое формулы D без коэффициента),
# доход/приход — в AE. AC входит в ZATRATY_PLAIN_COLS ниже, так что формула
# в D автоматически подхватит расход 8connect.
COL_8CONN_COST   = "AC"
COL_8CONN_CHARGE = "AE"

# Коэффициенты (НДС/комиссии) по колонкам кабинетов для формулы «Затраты».
# Колонки без коэффициента (AC, BC) — суммируются как есть (множитель 1).
ZATRATY_COEFFS: Dict[str, float] = {
    "AR": 1.062, "AS": 1.16,  "AT": 1.16,  "AU": 0.99,  "AV": 1.048,
    "AW": 0.99,  "AX": 0.9,   "AY": 1.048, "AZ": 1.16,  "BA": 0.972,
    "BB": 0.99,  "BD": 1.16,  "BE": 0.99,  "BF": 0.99,  "BG": 1.048,
    "BH": 1.16,  "BJ": 0.9,   "BK": 0.9,   "BL": 0.9,   "BM": 0.9,
    "BN": 0.99,  "BO": 0.99,  "BP": 0.99,  "BQ": 0.99,  "BR": 0.99,
    "BS": 0.99,  "BT": 0.954, "BU": 0.954, "BV": 0.954, "BW": 0.954,
    "BX": 0.954, "BY": 0.954, "BZ": 1.048, "CA": 0.954, "CB": 0.954,
    "CC": 0.954, "CD": 0.954, "CE": 0.954, "CF": 0.954, "CG": 1.048,
    "CH": 1.048, "CI": 1.16,  "CJ": 1.048, "CK": 1.16,  "CL": 1.16,
}
ZATRATY_PLAIN_COLS: Tuple[str, ...] = ("AC", "BC")

CABINET_HEADER_RANGE = "AR2:CL2"
FALLBACK_START_ROW   = 37
FALLBACK_NAME_COL    = "A"
FALLBACK_SPENT_COL   = "B"

TARGET_GOAL_FOR_ZAYAVKI = "Zayvka"

_WS_SPACE_RE = re.compile(r"\s+")


def _norm(name: str) -> str:
    """Нормализация имени кабинета для матчинга."""
    return _WS_SPACE_RE.sub(" ", (name or "").strip()).lower()


def _col_letter(index_1based: int) -> str:
    """1-based → A, B, …, Z, AA, AB, … (Google Sheets)."""
    n = index_1based
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _col_index(letter: str) -> int:
    """A→1, Z→26, AA→27 …"""
    n = 0
    for c in letter.upper():
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n


def _find_header_column(headers: Dict[str, str], report_name: str) -> Optional[str]:
    """Вернуть букву колонки для данного имени кабинета.

    Сначала exact-match по нормализованной карте, потом prefix-match с
    разделителем-пробелом или дефисом.
    """
    rn = _norm(report_name)
    if not rn:
        return None

    # 1) exact
    if rn in headers:
        return headers[rn]

    # 2) header — префикс report'а (с разделителем)
    best: Tuple[int, Optional[str]] = (0, None)  # (len(header), column)
    for hn, col in headers.items():
        if not hn:
            continue
        if (
            rn.startswith(hn + " ")
            or rn.startswith(hn + "-")
            or rn.startswith(hn + " -")
        ):
            if len(hn) > best[0]:
                best = (len(hn), col)
    if best[1] is not None:
        return best[1]

    # 3) report — префикс header'а (обратное направление, на всякий случай)
    for hn, col in headers.items():
        if hn.startswith(rn + " ") or hn.startswith(rn + "-"):
            return col

    return None


def _find_goal_visits(yandex_metrika: Dict[str, Any], goal_name: str) -> int:
    target = _norm(goal_name)
    for g in (yandex_metrika or {}).get("goals", []) or []:
        if _norm(g.get("goal_name", "")) == target:
            try:
                return int(g.get("visits") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _pick_worksheet(spreadsheet: Any, day: date) -> Tuple[Optional[Any], str]:
    """Вернуть (worksheet, title). Если не нашли — (None, искомое имя)."""
    title = f"{RU_MONTHS[day.month - 1]} {day.year % 100:02d}"
    try:
        return spreadsheet.worksheet(title), title
    except Exception:
        # Попробуем без ведущего нуля в году (редкий случай, year>=2100)
        return None, title


def _find_date_row(ws: Any, day: date) -> Optional[int]:
    needle = day.strftime("%d.%m.%Y")
    values = ws.col_values(1)  # столбец A целиком
    for idx, v in enumerate(values, start=1):
        if (v or "").strip() == needle:
            return idx
    return None


def _collect_cabinets(report: Dict[str, Any]) -> List[Tuple[str, float, str]]:
    """Плоский список (name, spent, source) из VK и Yandex Direct."""
    out: List[Tuple[str, float, str]] = []
    for name, spent in ((report.get("ads_manager") or {}).get("cabinets") or {}).items():
        try:
            out.append((name, float(spent or 0), "vk"))
        except (TypeError, ValueError):
            logger.warning("sheets_writer: VK кабинет %r — spent=%r не число, пропуск", name, spent)
    for name, spent in ((report.get("yandex") or {}).get("cabinets") or {}).items():
        try:
            out.append((name, float(spent or 0), "yandex"))
        except (TypeError, ValueError):
            logger.warning("sheets_writer: Yandex кабинет %r — spent=%r не число, пропуск", name, spent)
    return out


def _build_zatraty_formula(row: int) -> str:
    """Собирает формулу «Затрат» для заданной строки.

    Формат коэффициента — с запятой в качестве десятичного разделителя,
    т.к. таблица в русской локали (USER_ENTERED парсит по локали).
    """
    parts: List[str] = [f"{col}{row}" for col in ZATRATY_PLAIN_COLS]
    for col, k in ZATRATY_COEFFS.items():
        parts.append(f"{col}{row}*{f'{k:g}'.replace('.', ',')}")
    return "=" + "+".join(parts)


def _find_first_empty_fallback_row(ws: Any) -> int:
    """Первая пустая строка от FALLBACK_START_ROW в колонке A."""
    values = ws.col_values(_col_index(FALLBACK_NAME_COL))
    row = FALLBACK_START_ROW
    while row - 1 < len(values) and (values[row - 1] or "").strip():
        row += 1
    return row


def write_daily_report(
    config: Dict[str, Any],
    day: date,
    report: Dict[str, Any],
    *,
    spreadsheet: Any = None,
) -> Dict[str, Any]:
    """Пишет отчёт в Google Sheets. Возвращает сводку для API-ответа.

    Структура результата:
        {
          "enabled": True,
          "worksheet": "Апрель 26",
          "date_row": 20,
          "matched": [{"name", "spent", "column", "source"}, ...],
          "unmatched": [{"name", "spent", "source", "row"}, ...],
          "fixed": {"prihod": .., "clicks_lt": .., ...},
          "error": "... (если что-то сломалось)",
        }
    """
    gs_cfg = config.get("google_sheets") or {}
    if not gs_cfg.get("enabled", False):
        return {"enabled": False, "reason": "google_sheets.enabled = false"}

    if spreadsheet is None:
        # Ленивый импорт: хелпер живёт в matcher_main, а туда тянутся тяжёлые
        # зависимости — импортируем только если запись реально нужна.
        try:
            from matcher_main import build_gsheets_spreadsheet
        except ImportError as e:
            return {"enabled": True, "error": f"import matcher_main failed: {e}"}
        spreadsheet = build_gsheets_spreadsheet(config)
        if spreadsheet is None:
            return {"enabled": True, "error": "не удалось открыть spreadsheet (см. логи)"}

    ws, title = _pick_worksheet(spreadsheet, day)
    if ws is None:
        return {"enabled": True, "error": f"вкладка {title!r} не найдена"}

    date_row = _find_date_row(ws, day)
    if date_row is None:
        return {
            "enabled": True,
            "worksheet": title,
            "error": f"дата {day.strftime('%d.%m.%Y')} не найдена в столбце A",
        }

    leadstech = report.get("leadstech") or {}
    ym = report.get("yandex_metrika") or {}
    ec = report.get("eightconnect") or {}
    zayavki_visits = _find_goal_visits(ym, TARGET_GOAL_FOR_ZAYAVKI)

    fixed = {
        "prihod":              float(leadstech.get("sum") or 0),
        "clicks_lt":           int(leadstech.get("clicks") or 0),
        "metrika_v":           int(ym.get("visits") or 0),
        "zayavki":             int(zayavki_visits or 0),
        "perehody":            int(leadstech.get("hosts") or 0),
        "eightconnect_cost":   round(float(ec.get("cost") or 0), 2),
        "eightconnect_charge": round(float(ec.get("charge") or 0), 2),
    }

    batch: List[Dict[str, Any]] = [
        # AC/AE — сырые значения 8connect; пишем ДО формул в C и D,
        # чтобы batch_update пересчитал формулы на актуальных числах.
        {"range": f"{COL_8CONN_COST}{date_row}",  "values": [[fixed["eightconnect_cost"]]]},
        {"range": f"{COL_8CONN_CHARGE}{date_row}","values": [[fixed["eightconnect_charge"]]]},
        # C — «Приход» как формула-ссылка на AE (приход 8connect).
        {"range": f"{COL_PRIHOD}{date_row}",      "values": [[f"={COL_8CONN_CHARGE}{date_row}"]]},
        {"range": f"{COL_ZATRATY}{date_row}",     "values": [[_build_zatraty_formula(date_row)]]},
        {"range": f"{COL_CLICKS_LT}{date_row}",   "values": [[fixed["clicks_lt"]]]},
        {"range": f"{COL_METRIKA_V}{date_row}",   "values": [[fixed["metrika_v"]]]},
        {"range": f"{COL_ZAYAVKI}{date_row}",     "values": [[fixed["zayavki"]]]},
        {"range": f"{COL_PEREHODY}{date_row}",    "values": [[fixed["perehody"]]]},
    ]

    # --- Матчинг кабинетов ---
    header_row = ws.get(CABINET_HEADER_RANGE)  # [[h1, h2, ...]]
    header_cells: List[str] = header_row[0] if header_row else []
    headers_map: Dict[str, str] = {}
    start_col_idx = _col_index("AR")
    for offset, cell in enumerate(header_cells):
        norm = _norm(cell)
        if not norm:
            continue
        headers_map.setdefault(norm, _col_letter(start_col_idx + offset))

    cabinets = _collect_cabinets(report)

    # Суммируем spent по тем отчётным именам, которые матчатся в ту же колонку
    by_col_spent: Dict[str, float] = {}
    matched: List[Dict[str, Any]] = []
    unmatched: List[Tuple[str, float, str]] = []

    for name, spent, source in cabinets:
        col = _find_header_column(headers_map, name)
        if col is None:
            unmatched.append((name, spent, source))
            continue
        by_col_spent[col] = by_col_spent.get(col, 0.0) + spent
        matched.append({"name": name, "spent": spent, "column": col, "source": source})

    for col, total_spent in by_col_spent.items():
        batch.append({"range": f"{col}{date_row}", "values": [[round(total_spent, 2)]]})

    # --- Fallback: дописываем unmatched начиная с первой пустой строки ≥37 ---
    fallback_rows: List[Dict[str, Any]] = []
    if unmatched:
        start_row = _find_first_empty_fallback_row(ws)
        for idx, (name, spent, source) in enumerate(unmatched):
            row = start_row + idx
            batch.append({"range": f"{FALLBACK_NAME_COL}{row}",  "values": [[name]]})
            batch.append({"range": f"{FALLBACK_SPENT_COL}{row}", "values": [[round(spent, 2)]]})
            fallback_rows.append({"name": name, "spent": spent, "source": source, "row": row})

    if batch:
        ws.batch_update(batch, value_input_option="USER_ENTERED")

    logger.info(
        "Sheets: %s / row %d — fixed=%s, matched=%d cabs, unmatched=%d (→ A%d↓)",
        title, date_row, fixed, len(matched), len(fallback_rows),
        fallback_rows[0]["row"] if fallback_rows else 0,
    )

    return {
        "enabled": True,
        "worksheet": title,
        "date_row": date_row,
        "fixed": fixed,
        "matched": matched,
        "unmatched": fallback_rows,
    }
