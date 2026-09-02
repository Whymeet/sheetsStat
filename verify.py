"""Сверка семантического реестра метрик с живой таблицей Google Sheets.

Режим A — валидация оцифровки формул («one-step»): для каждой computed-метрики
её ПРЯМЫЕ операнды берутся из ячеек листа (включая другие computed-колонки),
формула вычисляется бэкендом и сравнивается со значением самой ячейки.
Каскадные расхождения исключены: каждая формула проверяется изолированно.

Особые случаи:
- lt_sumwebmaster (скрытая) — восстанавливается из литерала формулы
  «Дохода с витрины» (`=681266,48-AC13` → 681266.48), для этого её колонка
  читается FORMULA-рендером;
- cabinet_spend — Σ(ячейка × коэффициент строки 1) по кабинетной зоне (от AP);
  влияет только на «Затраты» (D). Исторические строки таблицы могли зашивать
  другие коэффициенты, чем в строке 1, — расхождение D репортится отдельно
  (это дрейф данных листа, а не ошибка транскрипции формулы);
- #DIV/0!/#N/A/пусто в ячейке == None у бэкенда.

Запуск: ./venv/bin/python verify.py <profile_id> [вкладка ...]
"""
from __future__ import annotations

import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import metrics as M
from sheets_writer import (
    CABINET_START_COL, _col_index, _col_letter, _norm, _parse_coeff, cabinet_bounds,
    manual_cabinet_labels, manual_income_labels_norm,
)

TOL_ABS = 0.01
TOL_REL = 0.001

_AJ_LITERAL_RE = re.compile(r"^=([\d\s]+(?:[.,]\d+)?)-")


resolve_registry_columns = M.resolve_registry_columns


def _cell_value(v: Any) -> Optional[float]:
    """UNFORMATTED-ячейка → float|None. Ошибки листа (#DIV/0! и пр.) → None."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or s.startswith("#"):
        return None
    try:
        return float(s.replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _parse_aj_literal(formula: Any) -> Optional[float]:
    if not isinstance(formula, str):
        return None
    m = _AJ_LITERAL_RE.match(formula.replace("\xa0", "").replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _close(expected: Optional[float], actual: Optional[float]) -> bool:
    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    diff = abs(expected - actual)
    return diff <= TOL_ABS or (abs(actual) > 1e-9 and diff / abs(actual) <= TOL_REL)


def verify_worksheet(ws, config: Dict[str, Any]) -> Dict[str, Any]:
    """Сверка одной месячной вкладки. Возвращает сводку с расхождениями."""
    gs_cfg = config.get("google_sheets") or {}
    end_idx = min(_col_index("EZ"), ws.col_count)
    end_col = _col_letter(end_idx)

    grid = ws.get(f"A1:{end_col}40", value_render_option="UNFORMATTED_VALUE")
    coeff_row = grid[0] if grid else []
    label_row = grid[1] if len(grid) > 1 else []

    cab_start, _ = cabinet_bounds(gs_cfg)
    cols, warnings = resolve_registry_columns(
        label_row, gs_cfg.get("column_labels") or {}, cab_start)
    aj_col = cols.get("dohod_vitrina")
    aj_formulas: Dict[int, Any] = {}
    if aj_col:
        fr = ws.get(f"{aj_col}3:{aj_col}40", value_render_option="FORMULA")
        for i, row in enumerate(fr):
            if row:
                aj_formulas[i + 3] = row[0]

    cabinet_start = cab_start
    manual_cabinets = { _norm(x) for x in manual_cabinet_labels(gs_cfg) }
    income_norm = manual_income_labels_norm(gs_cfg)

    mismatches: List[Dict[str, Any]] = []
    checked = rows_checked = 0

    for ri0 in range(2, len(grid)):  # строки 3..40
        row = grid[ri0]
        rownum = ri0 + 1
        date_serial = row[0] if row else None
        if not isinstance(date_serial, (int, float)) or date_serial < 40000:
            continue

        def cell(col_letter: Optional[str]) -> Optional[float]:
            if not col_letter:
                return None
            i = _col_index(col_letter) - 1
            return _cell_value(row[i] if i < len(row) else None)

        # есть ли в строке данные вообще (не будущая дата)
        base_cols = [cols.get(k) for k, m in M.METRICS.items()
                     if m.kind == "base_service" and m.col]
        if all(cell(c) is None for c in base_cols):
            continue
        rows_checked += 1

        # env: ВСЕ колонки листа по их фактическим значениям (one-step).
        # Пустая ячейка-операнд в Sheets-арифметике = 0 — повторяем; деление
        # на 0 у eval_expr по-прежнему None.
        env: Dict[str, Optional[float]] = {
            k: (cell(c) if cell(c) is not None else 0.0) for k, c in cols.items()
        }
        env["lt_sumwebmaster"] = _parse_aj_literal(aj_formulas.get(rownum))

        def raw_cell(col_letter: str) -> Any:
            i = _col_index(col_letter) - 1
            return row[i] if i < len(row) else None

        # cabinet_spend: Σ ячейка × коэф строки 1 по кабинетной зоне;
        # доходные ручные поля (target=prihod) — в manual_income, не в Затраты
        spend = 0.0
        income = 0.0
        for i0 in range(cabinet_start - 1, len(row)):
            label = label_row[i0] if i0 < len(label_row) else ""
            norm = _norm(str(label))
            if not norm:
                continue
            v = _cell_value(row[i0])
            if v is None:
                continue
            if norm in income_norm:
                income += v
                continue
            k = _parse_coeff(coeff_row[i0] if i0 < len(coeff_row) else None)
            spend += v * (1.0 if norm in manual_cabinets else k)
        env[M.CABINET_SPEND] = spend
        env[M.MANUAL_INCOME] = income

        for key in M.COMPUTE_ORDER:
            col = cols.get(key)
            if col is None:
                continue
            if env["lt_sumwebmaster"] is None and "lt_sumwebmaster" in M._expr_names(M._PARSED[key]):
                continue  # нет литерала — строку писал не бэкенд, нечего сверять
            raw = raw_cell(col)
            if raw is None or raw == "":
                continue  # лист эту ячейку никогда не считал — сравнивать нечего
            expected = M.eval_expr(M._PARSED[key], env)
            actual = cell(col)
            checked += 1
            if not _close(expected, actual):
                mismatches.append({
                    "row": rownum, "key": key, "col": col,
                    "expected": expected, "actual": actual,
                })

    return {
        "worksheet": ws.title,
        "rows_checked": rows_checked,
        "cells_checked": checked,
        "mismatches": mismatches,
        "resolve_warnings": warnings,
        "resolved_columns": cols,
    }


def main() -> int:
    import json
    import gspread

    pid = sys.argv[1] if len(sys.argv) > 1 else "osnovnoy"
    tabs = sys.argv[2:] or ["Август 26", "Июль 26", "Июнь 26"]

    import db
    config = db.get_profile(pid)
    if not config:
        print(f"нет профиля {pid}")
        return 2
    gs_cfg = config["google_sheets"]
    gc = gspread.service_account(filename=gs_cfg.get("service_account_json_path", "cfg/service_account.json"))
    sh = gc.open_by_key(gs_cfg["spreadsheet_id"])

    total_mm = 0
    for tab in tabs:
        try:
            ws = sh.worksheet(tab)
        except Exception:
            print(f"[{tab}] вкладки нет — пропуск")
            continue
        res = verify_worksheet(ws, config)
        total_mm += len(res["mismatches"])
        print(f"[{tab}] строк: {res['rows_checked']}, ячеек: {res['cells_checked']}, "
              f"расхождений: {len(res['mismatches'])}")
        if res["resolve_warnings"]:
            print("  подписи:", res["resolve_warnings"])
        for mm in res["mismatches"][:25]:
            print(f"  row {mm['row']} {mm['key']}({mm['col']}): "
                  f"ожидалось {mm['expected']}, в листе {mm['actual']}")
        if len(res["mismatches"]) > 25:
            print(f"  ... и ещё {len(res['mismatches']) - 25}")
    print("ИТОГО расхождений:", total_mm)
    return 0 if total_mm == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
