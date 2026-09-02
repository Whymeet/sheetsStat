"""Генерация листов Google Sheets из семантического реестра метрик.

- ensure_month_worksheet: недостающая месячная вкладка («Август 26») создаётся
  с нуля — шапка (строка 1: прочерки в агрегатной зоне + коэффициенты над
  кабинетами; строка 2: подписи метрик + имена кабинетов), все даты месяца в
  столбце A, формулы вычисляемых колонок во всех строках дат.
- create_brand_spreadsheet: целая таблица для нового бренда (создаётся в Drive
  сервисного аккаунта и расшаривается адресам google_sheets.share_with).

Формулы генерятся из metrics.METRICS (expr_to_a1) по каноническому layout.
Исключение — метрики, чьи выражения ссылаются на скрытые (без колонки)
метрики — «Приход» (= lt_sumwebmaster + manual_income) и «Доход с витрины»
(= lt_sumwebmaster − sms_charge): литерал
sumwebmaster известен только в момент прогона, поэтому такие ячейки остаются
пустыми и заполняются ежедневной записью (как и сейчас).

Ручные колонки (base_manual: R «Долеты и Крот»; кабинетные manual_cabinets:
AVITO, Google) получают подпись, но никаких формул/значений — их ведут люди.

Вся запись значений — одним values-batch (квоты Sheets), заморозка строк —
одним spreadsheets.batchUpdate.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import metrics as M
from sheets_writer import (
    CABINET_START_COL, RU_MONTHS, _col_index, _col_letter, _norm, cabinet_bounds,
    disabled_metrics, manual_cabinet_entries,
)

logger = logging.getLogger("sheetsstat.sheet_builder")

FIRST_DATA_ROW = 3


def month_title(day: date) -> str:
    return f"{RU_MONTHS[day.month - 1]} {day.year % 100:02d}"


# ============ Оформление (оцифровано с эталонной вкладки «Июль 26») ============
# Спека привязана к семантическим ключам метрик, буквы резолвятся в момент
# генерации — переживёт смену layout. Рукотворные непоследовательности эталона
# (смесь шрифтов, случайные отличия ячеек) намеренно не копируются.

_C_YELLOW = {"red": 1, "green": 1}                            # шапка: агрегаты
_C_GREEN = {"green": 1}                                       # шапка: СМС/витрина + кабинеты
_C_ORANGE = {"red": 0.9, "green": 0.57, "blue": 0.22}         # шапка: EPC
_C_BLUE = {"red": 0.29, "green": 0.53, "blue": 0.91}          # шапка: ROI
_C_LGREEN = {"red": 0.85, "green": 0.92, "blue": 0.83}        # данные: СМС/витрина
_C_LGREY = {"red": 0.95, "green": 0.95, "blue": 0.95}         # данные: серые колонки

_HEADER_BG = {  # особые цвета подписей (дефолт — жёлтый)
    "sms_cost": _C_GREEN, "sms_charge": _C_GREEN, "dohod_vitrina": _C_GREEN,
    "epc": _C_ORANGE, "roi": _C_BLUE,
}
_DATA_BG = {
    "zatraty": _C_LGREY, "obshchee": _C_LGREY, "dolety": _C_LGREY,
    "chistye": _C_LGREY, "dohod_s_unika": _C_LGREY, "roi_sms": _C_LGREY,
    "sms_cost": _C_LGREEN, "sms_charge": _C_LGREEN,
    "sms_clients": _C_LGREEN, "dohod_vitrina": _C_LGREEN,
}
_DATA_BOLD = {"chistaya", "prihod", "obshchee", "epc", "pokupka_s_lida",
              "prodazha_s_lida", "roi", "vsego", "itogo"}
_NUMBER_FMT = {  # дефолт — {"type": "NUMBER", "pattern": "#,##0"}
    "date": {"type": "DATE", "pattern": "dd.mm.yyyy"},
    "roi": {"type": "PERCENT", "pattern": "0.00%"},
    "roi_sms": {"type": "PERCENT", "pattern": "0%"},
    "bekender": {"type": "NUMBER", "pattern": "#,##0.00"},
    "sms_share": {"type": "NUMBER", "pattern": "#,##0.00"},
    "api_share": {"type": "NUMBER", "pattern": "#,##0.00"},
    "dohod_na_zayavku": {"type": "NUMBER", "pattern": "#,##0.00"},
    "marzha_s_klika": {"type": "NUMBER", "pattern": "#,##0.00"},
}
_COL_WIDTH = {  # px из эталона; отсутствующие агрегаты — 100
    "date": 75, "chistaya": 72, "zatraty": 102, "metrika_v": 123,
    "zayavki": 106, "dohod_na_zayavku": 115, "dolety": 246, "sms_chistye": 131,
    "pokupka_s_lida": 108, "prodazha_s_lida": 113, "perehody": 170,
    "dohod_vitrina": 121, "dohod_s_unika": 150, "itogo": 211,
    "marzha_s_klika": 159,
}
_CABINET_WIDTH = 180
_MANUAL_WIDTH = 120
_DEFAULT_NF = {"type": "NUMBER", "pattern": "#,##0"}


def _grid_range(sheet_id: int, r1: int, r2: int, c1: int, c2: int) -> Dict[str, int]:
    """Полуоткрытый GridRange из 1-based включительных строк/колонок."""
    return {"sheetId": sheet_id, "startRowIndex": r1 - 1, "endRowIndex": r2,
            "startColumnIndex": c1 - 1, "endColumnIndex": c2}


def _repeat_cell(rng: Dict[str, int], fmt: Dict[str, Any]) -> Dict[str, Any]:
    fields = ",".join(f"userEnteredFormat.{k}" for k in fmt)
    return {"repeatCell": {"range": rng, "cell": {"userEnteredFormat": fmt},
                           "fields": fields}}


def _format_requests(
    sheet_id: int,
    layout: List[Tuple[str, Any]],
    cabinets: List[Tuple[str, str, float, bool]],
    n_data_rows: int,
    width: int,
    totals_row: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Requests оформления новой вкладки: фоны, границы, форматы, ширины."""
    last_row = FIRST_DATA_ROW + n_data_rows - 1
    reqs: List[Dict[str, Any]] = []
    solid = {"style": "SOLID"}

    # 1. Сплошная сетка границ по всей используемой области.
    reqs.append({"updateBorders": {
        "range": _grid_range(sheet_id, 1, last_row, 1, width),
        "top": solid, "bottom": solid, "left": solid, "right": solid,
        "innerHorizontal": solid, "innerVertical": solid,
    }})

    # 2. Шапка (строка 2): жёлтая база, bold, по центру, с переносом.
    reqs.append(_repeat_cell(
        _grid_range(sheet_id, 2, 2, 1, width),
        {"backgroundColor": _C_YELLOW,
         "textFormat": {"bold": True},
         "horizontalAlignment": "CENTER",
         "verticalAlignment": "MIDDLE",
         "wrapStrategy": "OVERFLOW_CELL"},
    ))
    #    Особые цвета подписей метрик.
    for col, m in layout:
        bg = _HEADER_BG.get(m.key)
        if bg:
            i = _col_index(col)
            reqs.append(_repeat_cell(_grid_range(sheet_id, 2, 2, i, i),
                                     {"backgroundColor": bg}))
    #    Кабинетная зона — зелёные подписи.
    if cabinets:
        c1 = _col_index(cabinets[0][0])
        c2 = _col_index(cabinets[-1][0])
        reqs.append(_repeat_cell(_grid_range(sheet_id, 2, 2, c1, c2),
                                 {"backgroundColor": _C_GREEN}))

    # 3. Данные: числа вправо, дефолтный формат.
    reqs.append(_repeat_cell(
        _grid_range(sheet_id, FIRST_DATA_ROW, last_row, 1, width),
        {"horizontalAlignment": "RIGHT", "numberFormat": _DEFAULT_NF},
    ))
    #    Per-колоночные фоны/bold/форматы.
    for col, m in layout:
        fmt: Dict[str, Any] = {}
        if m.key in _DATA_BG:
            fmt["backgroundColor"] = _DATA_BG[m.key]
        if m.key in _DATA_BOLD:
            fmt["textFormat"] = {"bold": True}
        if m.key in _NUMBER_FMT:
            fmt["numberFormat"] = _NUMBER_FMT[m.key]
        if fmt:
            i = _col_index(col)
            reqs.append(_repeat_cell(
                _grid_range(sheet_id, FIRST_DATA_ROW, last_row, i, i), fmt))

    # 4. Ширины колонок (сгруппированные диапазоны одинаковой ширины).
    widths: List[int] = [100] * width
    for col, m in layout:
        widths[_col_index(col) - 1] = _COL_WIDTH.get(m.key, 100)
    for col, _name, _k, is_manual in cabinets:
        widths[_col_index(col) - 1] = _MANUAL_WIDTH if is_manual else _CABINET_WIDTH
    start = 0
    for i in range(1, width + 1):
        if i == width or widths[i] != widths[start]:
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": start, "endIndex": i},
                "properties": {"pixelSize": widths[start]},
                "fields": "pixelSize",
            }})
            start = i

    # 5. Итоговая строка: жёлтая, bold, жирная верхняя граница.
    if totals_row:
        reqs.append(_repeat_cell(
            _grid_range(sheet_id, totals_row, totals_row, 1, width),
            {"backgroundColor": _C_YELLOW, "textFormat": {"bold": True}},
        ))
        reqs.append({"updateBorders": {
            "range": _grid_range(sheet_id, totals_row, totals_row, 1, width),
            "top": {"style": "SOLID_MEDIUM"},
        }})
    return reqs


def _hidden_keys() -> set:
    return {k for k, m in M.METRICS.items() if m.col is None}


def _formula_keys() -> List[str]:
    """Computed-метрики, которым можно сгенерировать формулу заранее
    (не ссылаются на скрытые метрики — те пишутся в момент прогона)."""
    hidden = _hidden_keys()
    out = []
    for key in M.computed_keys():
        deps = set(M._expr_names(M._PARSED[key]))
        if deps & hidden:
            continue
        out.append(key)
    return out


def _cabinet_layout(config: Dict[str, Any], extra_cabinets: Optional[List[str]] = None,
                    ) -> List[Tuple[str, str, float, bool]]:
    """[(буква, имя, коэффициент, is_manual)] кабинетной зоны от AR.

    Имена: google_sheets.cabinets из конфига + кабинеты текущего отчёта
    (extra_cabinets) + manual_cabinets в конце. Коэффициенты — из
    google_sheets.cabinet_coeffs (дефолт 1), у ручных всегда 1.
    """
    gs_cfg = config.get("google_sheets") or {}
    coeffs = {_norm(k): float(v) for k, v in (gs_cfg.get("cabinet_coeffs") or {}).items()}
    manual_entries = manual_cabinet_entries(gs_cfg)
    manual = [e["label"] for e in manual_entries]
    manual_income_norm = {_norm(e["label"]) for e in manual_entries if e["target"] == "prihod"}
    manual_norm = {_norm(x) for x in manual}

    names: List[str] = []
    seen = set()
    for name in list(gs_cfg.get("cabinets") or []) + list(extra_cabinets or []):
        name = str(name).strip()
        norm = _norm(name)
        if not norm or norm in seen or norm in manual_norm:
            continue
        seen.add(norm)
        names.append(name)

    start, _ = cabinet_bounds(gs_cfg)
    out: List[Tuple[str, str, float, bool]] = []
    idx = start
    for name in names:
        out.append((_col_letter(idx), name, coeffs.get(_norm(name), 1.0), False))
        idx += 1
    for name in manual:
        # is_manual=True; доходные помечаем коэффициентом-маркером не нужно —
        # разведение по формулам делает build_month_grid по manual_income_norm
        out.append((_col_letter(idx), name, 1.0, True))
        idx += 1
    return out


def _coeff_cell(k: float) -> str:
    if k == 1:
        return ""
    return f"{k:g}".replace(".", ",")


def build_month_grid(
    config: Dict[str, Any],
    day: date,
    extra_cabinets: Optional[List[str]] = None,
) -> Tuple[List[List[Any]], int]:
    """Сетка значений (строки 1..N) для values-batch. Возвращает (grid, ширина)."""
    gs_cfg = config.get("google_sheets") or {}
    labels_override = gs_cfg.get("column_labels") or {}
    cabinets = _cabinet_layout(config, extra_cabinets)

    layout = M.layout_columns()  # [(буква, метрика)] A..AM
    agg_end_idx = max(_col_index(c) for c, _ in layout)
    width = max(
        _col_index(cabinets[-1][0]) if cabinets else cabinet_bounds(config)[0],
        agg_end_idx,
    )

    def blank_row() -> List[Any]:
        return [""] * width

    # Строка 1: прочерки в агрегатной зоне (как в эталоне), коэффициенты — над
    # кабинетами (пустой коэффициент = 1, ручные — пусто).
    row1 = blank_row()
    for col, _m in layout:
        i = _col_index(col) - 1
        if col != "A":
            row1[i] = "-"
    for col, _name, k, is_manual in cabinets:
        row1[_col_index(col) - 1] = "" if is_manual else _coeff_cell(k)

    # Строка 2: подписи метрик (с оверрайдами бренда) + имена кабинетов.
    # Отключённые метрики остаются пустой колонкой (позиции не сдвигаются).
    dset = disabled_metrics(gs_cfg)
    row2 = blank_row()
    for col, m in layout:
        if m.key in dset:
            continue
        row2[_col_index(col) - 1] = labels_override.get(m.key) or m.label or ""
    for col, name, _k, _man in cabinets:
        row2[_col_index(col) - 1] = name

    # Строки дат: A — dd.mm.yyyy, формулы computed-колонок из реестра.
    # Доходные ручные поля (target=prihod) — не слагаемые D, а слагаемые C.
    income_norm = {_norm(e["label"]) for e in manual_cabinet_entries(gs_cfg)
                   if e["target"] == "prihod"}
    colmap = {m.key: c for c, m in layout}
    cabinet_terms = [(c, k) for c, n, k, _man in cabinets if _norm(n) not in income_norm]
    income_terms = [c for c, n, _k, _man in cabinets if _norm(n) in income_norm]
    formula_keys = _formula_keys()
    days_in_month = calendar.monthrange(day.year, day.month)[1]

    grid = [row1, row2]
    for dnum in range(1, days_in_month + 1):
        rownum = FIRST_DATA_ROW + dnum - 1
        row = blank_row()
        row[0] = f"{dnum:02d}.{day.month:02d}.{day.year}"
        ctx = M.A1Context(colmap=colmap, row=rownum, literals={},
                          cabinet_terms=cabinet_terms, income_terms=income_terms,
                          disabled=dset)
        for key in formula_keys:
            f = M.expr_to_a1(key, ctx)
            if f:
                row[_col_index(colmap[key]) - 1] = f
        grid.append(row)

    # Итоговая строка месяца (оцифровка строки 34 эталона): SUM/expr/AVERAGEIF
    # по метрикам + SUM по всем кабинетным колонкам (включая ручные).
    first_row, last_row = FIRST_DATA_ROW, FIRST_DATA_ROW + days_in_month - 1
    totals_row = last_row + 1
    trow = blank_row()
    t_ctx = M.A1Context(colmap=colmap, row=totals_row, literals={},
                        cabinet_terms=cabinet_terms, income_terms=income_terms,
                        disabled=dset)
    for _col, m in layout:
        f = M.total_formula(m.key, t_ctx, first_row, last_row)
        if f:
            trow[_col_index(colmap[m.key]) - 1] = f
    for col, _name, _k, _man in cabinets:
        trow[_col_index(col) - 1] = f"=SUM({col}{first_row}:{col}{last_row})"
    grid.append(trow)
    return grid, width


def ensure_month_worksheet(
    spreadsheet: Any,
    day: date,
    config: Dict[str, Any],
    extra_cabinets: Optional[List[str]] = None,
) -> Tuple[Any, bool]:
    """Возвращает (worksheet, created). Создаёт вкладку месяца, если её нет."""
    title = month_title(day)
    try:
        return spreadsheet.worksheet(title), False
    except Exception:
        pass

    grid, width = build_month_grid(config, day, extra_cabinets)
    rows = len(grid) + 5
    cols = width + 10
    ws = spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)

    end = _col_letter(width)
    ws.update(grid, f"A1:{end}{len(grid)}", value_input_option="USER_ENTERED")

    # Freeze + всё оформление (границы/фоны/форматы/ширины) — одним batchUpdate.
    try:
        requests = [{
            "updateSheetProperties": {
                "properties": {
                    "sheetId": ws.id,
                    "gridProperties": {"rowCount": rows, "columnCount": cols,
                                       "frozenRowCount": 2},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        }]
        requests += _format_requests(
            ws.id, M.layout_columns(), _cabinet_layout(config, extra_cabinets),
            n_data_rows=len(grid) - 2,  # даты + итоговая строка
            width=width, totals_row=len(grid),
        )
        spreadsheet.batch_update({"requests": requests})
    except Exception as e:
        logger.warning("sheet_builder/%s: оформление вкладки не применилось: %s",
                       title, e)

    logger.info("sheet_builder: создана вкладка %r (%d строк, %d колонок, "
                "%d формульных колонок)", title, rows, cols, len(_formula_keys()))
    return ws, True


def create_brand_spreadsheet(config: Dict[str, Any], brand_name: str,
                             gc: Any, day: Optional[date] = None) -> Dict[str, Any]:
    """Создаёт таблицу бренда с нуля. Возвращает {spreadsheet_id, url, warnings}."""
    gs_cfg = config.get("google_sheets") or {}
    day = day or date.today()
    warnings: List[str] = []

    sh = gc.create(f"{brand_name} — статистика")
    share_with = [s.strip() for s in (gs_cfg.get("share_with") or []) if str(s).strip()]
    if not share_with:
        warnings.append(
            "google_sheets.share_with пуст — таблица создана в Drive сервисного "
            "аккаунта и не видна людям; добавь email и создай заново или "
            "расшарь вручную"
        )
    for email in share_with:
        try:
            sh.share(email, perm_type="user", role="writer", notify=False)
        except Exception as e:
            warnings.append(f"не удалось расшарить {email}: {e}")

    ensure_month_worksheet(sh, day, config)
    # дефолтный «Sheet1» больше не нужен
    try:
        default_ws = sh.sheet1
        if default_ws.title != month_title(day):
            sh.del_worksheet(default_ws)
    except Exception:
        pass

    logger.info("sheet_builder: создана таблица %r (%s), доступ: %s",
                brand_name, sh.id, share_with or "—")
    return {
        "spreadsheet_id": sh.id,
        "url": f"https://docs.google.com/spreadsheets/d/{sh.id}/edit",
        "warnings": warnings,
    }
