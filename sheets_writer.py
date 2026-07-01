"""Запись дневного отчёта build_report в Google Sheets.

Таблица — «Копия kubyshka-zaim.ru». На каждый месяц — отдельная вкладка
(«Апрель 26», «Май 26», …). Столбец A — даты `dd.mm.yyyy`.

На строке даты пишем агрегаты в фиксированных колонках:
    C  — Приход            = формула `=AE{row}+AJ{row}+AF{row}+R{row}`
    E  — Клики ЛТ          = leadstech.clicks
    F  — Метрика визиты    = yandex_metrika.visits
    G  — Заявки с сайта    = Достижения/Целевые визиты цели бренда
                             (yandex_metrika.zayavki_metric: reaches|visits)
    AB — 8connect SMS      = eightconnect.count (кол-во отправленных SMS)
    AC — 8connect cost     = eightconnect.cost
    AE — 8connect charge   = eightconnect.charge
    AF — Клиенты           = всегда 0 (по требованию)
    AI — Переходы уники    = leadstech.hosts
    AJ — Доход с витрины   = формула `={leadstech.sum}-AE{row}` (sumwebmaster − 8connect charge)

В `AR2:CL2` лежат названия рекламных кабинетов. По каждому spent'у из отчёта
ищем свою колонку и пишем spent в `{col}{date_row}`. Кабинеты, которых нет
в шапке, добавляем стопкой начиная с A37 (A=имя, B=spent) — «чтобы не пропали».
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import date
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger("sheetsstat.sheets_writer")


RU_MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

COL_PRIHOD         = "C"
COL_ZATRATY        = "D"
COL_CLICKS_LT      = "E"
COL_METRIKA_V      = "F"
COL_ZAYAVKI        = "G"
COL_PEREHODY       = "AI"
COL_DOHOD_VITRINA  = "AJ"

# 8connect: расход пишем в AC (уже слагаемое формулы D без коэффициента),
# доход/приход — в AE. AC входит в ZATRATY_PLAIN_COLS ниже, так что формула
# в D автоматически подхватит расход 8connect.
# AB — кол-во отправленных SMS (count), AF — кол-во клиентов (client).
COL_8CONN_COST    = "AC"
COL_8CONN_CHARGE  = "AE"
COL_8CONN_COUNT   = "AB"
COL_8CONN_CLIENTS = "AF"

# Коэффициенты (НДС/комиссии) по колонкам кабинетов для формулы «Затраты».
# Колонки без коэффициента (AC, AX, BC) — суммируются как есть (множитель 1).
ZATRATY_COEFFS: Dict[str, float] = {
    "AR": 0.954, "AS": 1.16,  "AT": 1.16,  "AU": 0.99,  "AV": 1.048,
    "AW": 0.99,               "AY": 1.048, "AZ": 1.16,  "BA": 0.972,
    "BB": 0.99,  "BD": 1.16,  "BE": 0.99,  "BF": 0.99,  "BG": 1.048,
    "BH": 1.16,  "BJ": 0.9,   "BK": 0.9,   "BL": 0.9,   "BM": 0.9,
    "BN": 0.99,  "BO": 0.99,  "BP": 0.99,  "BQ": 0.99,  "BR": 0.99,
    "BS": 0.99,  "BT": 0.954, "BU": 0.954, "BV": 0.954, "BW": 0.954,
    "BX": 0.954, "BY": 0.954, "BZ": 1.048, "CA": 0.954, "CB": 0.954,
    "CC": 0.954, "CD": 0.954, "CE": 0.954, "CF": 0.954, "CG": 1.048,
    "CH": 1.048, "CI": 1.16,  "CJ": 1.048, "CK": 1.16,  "CL": 1.16,
    "CM": 1.16,  "CN": 1.16,  "CO": 1.16,  "CP": 1.16,
}
ZATRATY_PLAIN_COLS: Tuple[str, ...] = ("AC", "AX", "BC")

CABINET_HEADER_RANGE = "AR2:EZ2"
FALLBACK_START_ROW   = 37
FALLBACK_NAME_COL    = "A"
FALLBACK_SPENT_COL   = "B"

TARGET_GOAL_FOR_ZAYAVKI = "Zayvka"

# Строка-эталон формул. Читаем её один раз на вкладку и вставляем все
# обнаруженные формулы в новую строку даты (с заменой ссылок на строку).
TEMPLATE_ROW = 33
_TEMPLATE_RANGE = f"A{TEMPLATE_ROW}:ZZ{TEMPLATE_ROW}"
# \b нужен, чтобы 33 не цеплялось посреди 330/133, но срабатывало на AR33 / AR$33.
_TEMPLATE_ROW_RE = re.compile(rf"(\$?[A-Z]+\$?){TEMPLATE_ROW}\b")
# Кэш формул по title вкладки — один раз на процесс.
_TEMPLATE_CACHE: Dict[str, Dict[str, str]] = {}

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


def _goal_num(g: Dict[str, Any], metric: str) -> int:
    try:
        return int(g.get(metric) or 0)
    except (TypeError, ValueError):
        return 0


def _find_goal_value(yandex_metrika: Dict[str, Any], goal_name: str, metric: str) -> int:
    """Число цели для столбца G.

    `metric` — какое число цели брать: "visits" (Целевые визиты) или
    "reaches" (Достижения); выбирается в настройках бренда.

    Цель для G у каждого бренда своя (напр. "Zayvka" или "Заявка с сайта"),
    поэтому не привязываемся жёстко к одному имени: сначала пробуем матч по
    `goal_name`, а если такой цели нет — берём первую настроенную цель бренда
    (у профиля обычно ровно одна цель — она и есть «Заявки с сайта»).
    """
    goals = (yandex_metrika or {}).get("goals", []) or []
    target = _norm(goal_name)
    for g in goals:
        if _norm(g.get("goal_name", "")) == target:
            return _goal_num(g, metric)
    return _goal_num(goals[0], metric) if goals else 0


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


def _load_template_formulas(ws: Any, title: str) -> Dict[str, str]:
    """Читает формулы из строки-эталона TEMPLATE_ROW вкладки.

    Возвращает `{col_letter: formula}` только для ячеек, начинающихся с `=`.
    Результат кэшируется по title вкладки на всё время жизни процесса.
    """
    cached = _TEMPLATE_CACHE.get(title)
    if cached is not None:
        return cached

    try:
        rows = ws.get(_TEMPLATE_RANGE, value_render_option="FORMULA")
    except TypeError:
        # Совместимость со старыми версиями gspread без kwarg'а.
        rows = ws.get(_TEMPLATE_RANGE)

    cells = rows[0] if rows else []
    template: Dict[str, str] = {}
    for offset, cell in enumerate(cells):
        if isinstance(cell, str) and cell.startswith("="):
            template[_col_letter(1 + offset)] = cell

    logger.info(
        "Sheets/%s: шаблонных формул в строке %d — %d (%s)",
        title, TEMPLATE_ROW, len(template), ", ".join(sorted(template.keys())) or "—",
    )
    _TEMPLATE_CACHE[title] = template
    return template


def _substitute_template_row(formula: str, target_row: int) -> str:
    """Заменяет в формуле ссылки вида AR33 / $AR$33 на тот же столбец, но с target_row."""
    return _TEMPLATE_ROW_RE.sub(rf"\g<1>{target_row}", formula)


def _build_prihod_formula(row: int) -> str:
    """Формула C: `=AE{row}+AJ{row}+AF{row}+R{row}`."""
    return f"={COL_8CONN_CHARGE}{row}+{COL_DOHOD_VITRINA}{row}+AF{row}+R{row}"


def _build_dohod_vitrina_formula(row: int, sumwebmaster: float) -> str:
    """Формула AJ: `={sumwebmaster}-AE{row}` (sumwebmaster − 8connect charge).

    Число форматируем с запятой — таблица в русской локали (USER_ENTERED).
    """
    literal = f"{sumwebmaster:.2f}".replace(".", ",")
    return f"={literal}-{COL_8CONN_CHARGE}{row}"


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


def _build_gsheets_spreadsheet(config: Dict[str, Any]) -> Optional[Any]:
    """Открывает Google Sheets Spreadsheet по конфигу.

    Блок `google_sheets` в `cfg/lt_vk_config.json`:

        "google_sheets": {
          "enabled": true,
          "service_account_json_path": "cfg/service_account.json",
          "spreadsheet_id": "1AbCdEfG..."
        }

    Возвращает None, если интеграция выключена / неправильно настроена /
    не удалось достучаться до Google API после ретраев.
    """
    gs_cfg = config.get("google_sheets")
    if not gs_cfg:
        logger.info(
            "Google Sheets: блок 'google_sheets' в конфиге не задан — интеграция выключена"
        )
        return None

    if not gs_cfg.get("enabled", True):
        logger.info("Google Sheets: google_sheets.enabled = false — интеграция выключена")
        return None

    service_account_path = gs_cfg.get("service_account_json_path") or os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_JSON"
    )
    spreadsheet_id = gs_cfg.get("spreadsheet_id") or os.getenv("GOOGLE_SPREADSHEET_ID")

    if not service_account_path or not spreadsheet_id:
        logger.warning(
            "Google Sheets: не заданы service_account_json_path или spreadsheet_id — интеграция выключена"
        )
        return None

    if not os.path.exists(service_account_path):
        logger.error(
            "Google Sheets: файл сервисного аккаунта не найден: %s. "
            "Проверь google_sheets.service_account_json_path в конфиге.",
            service_account_path,
        )
        return None

    try:
        import gspread  # type: ignore
    except ImportError:
        logger.error(
            "Google Sheets: не найдена библиотека gspread. "
            "Установи зависимости: pip install gspread google-auth"
        )
        return None

    max_retries = 3
    retry_delay = 2
    spreadsheet = None
    try:
        for attempt in range(max_retries):
            try:
                logger.info("Google Sheets: попытка подключения %d/%d...", attempt + 1, max_retries)
                gc = gspread.service_account(filename=service_account_path)
                spreadsheet = gc.open_by_key(spreadsheet_id)
                break
            except Exception as retry_exc:
                if attempt < max_retries - 1:
                    logger.warning(
                        "Google Sheets: ошибка при подключении (попытка %d): %s",
                        attempt + 1, retry_exc,
                    )
                    logger.info("Повторная попытка через %d секунд...", retry_delay)
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    raise
    except Exception as exc:
        logger.error("Google Sheets: не удалось открыть таблицу после %d попыток: %s", max_retries, exc)
        logger.error("Проверьте:")
        logger.error("  1. Интернет-соединение")
        logger.error("  2. Файл service_account.json существует и содержит валидные credentials")
        logger.error("  3. Service account имеет доступ к таблице с ID: %s", spreadsheet_id)
        logger.error("  4. Нет блокировки firewall/антивируса для доступа к Google API")
        return None

    logger.info("Google Sheets: открыт spreadsheet '%s'", spreadsheet.title)
    return spreadsheet


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
        spreadsheet = _build_gsheets_spreadsheet(config)
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
    zayavki_metric = ((config.get("yandex_metrika") or {}).get("zayavki_metric")) or "visits"
    if zayavki_metric not in ("reaches", "visits"):
        zayavki_metric = "visits"
    zayavki_val = _find_goal_value(ym, TARGET_GOAL_FOR_ZAYAVKI, zayavki_metric)

    fixed = {
        "prihod":               float(leadstech.get("sum") or 0),
        "clicks_lt":            int(leadstech.get("clicks") or 0),
        "metrika_v":            int(ym.get("visits") or 0),
        "zayavki":              int(zayavki_val or 0),
        "perehody":             int(leadstech.get("hosts") or 0),
        "eightconnect_cost":    round(float(ec.get("cost") or 0), 2),
        "eightconnect_charge":  round(float(ec.get("charge") or 0), 2),
        "eightconnect_count":   int(ec.get("count") or 0),
        "eightconnect_clients": int(ec.get("clients") or 0),
    }

    # Шаблонные формулы из строки 33 — идут ПЕРВЫМИ; если наша логика пишет
    # в ту же колонку (C, D, E, F, G, AI, AC, AE, AR..CL), последняя запись в
    # batch_update перекроет шаблон.
    try:
        template = _load_template_formulas(ws, title)
    except Exception as e:
        logger.warning("Sheets/%s: не удалось загрузить шаблон строки %d: %s",
                       title, TEMPLATE_ROW, e)
        template = {}

    template_batch: List[Dict[str, Any]] = [
        {"range": f"{col}{date_row}",
         "values": [[_substitute_template_row(formula, date_row)]]}
        for col, formula in template.items()
    ]

    batch: List[Dict[str, Any]] = template_batch + [
        # AC — слагаемое формулы D; пишем ДО D, чтобы batch_update увидел актуальное значение.
        {"range": f"{COL_8CONN_COST}{date_row}",     "values": [[fixed["eightconnect_cost"]]]},
        {"range": f"{COL_8CONN_CHARGE}{date_row}",   "values": [[fixed["eightconnect_charge"]]]},
        # AB — кол-во SMS, AF — клиенты (по требованию всегда 0).
        {"range": f"{COL_8CONN_COUNT}{date_row}",    "values": [[fixed["eightconnect_count"]]]},
        {"range": f"{COL_8CONN_CLIENTS}{date_row}",  "values": [[0]]},
        # AJ (доход с витрины) = sumwebmaster − AE; пишем ДО C, т.к. C ссылается на AJ.
        {"range": f"{COL_DOHOD_VITRINA}{date_row}",  "values": [[_build_dohod_vitrina_formula(date_row, fixed["prihod"])]]},
        # C (Приход) — формула =AE+AJ+AF+R (AF пишется чуть выше).
        {"range": f"{COL_PRIHOD}{date_row}",         "values": [[_build_prihod_formula(date_row)]]},
        {"range": f"{COL_ZATRATY}{date_row}",        "values": [[_build_zatraty_formula(date_row)]]},
        {"range": f"{COL_CLICKS_LT}{date_row}",      "values": [[fixed["clicks_lt"]]]},
        {"range": f"{COL_METRIKA_V}{date_row}",      "values": [[fixed["metrika_v"]]]},
        {"range": f"{COL_ZAYAVKI}{date_row}",        "values": [[fixed["zayavki"]]]},
        {"range": f"{COL_PEREHODY}{date_row}",       "values": [[fixed["perehody"]]]},
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

    logger.info("Sheets: заголовки шапки (%d): %s", len(headers_map),
                {v: k for k, v in list(headers_map.items())})

    cabinets = _collect_cabinets(report)
    logger.info("Sheets: кабинеты из отчёта (%d): %s", len(cabinets),
                [(n, s) for n, _, s in cabinets])

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
        "Sheets: %s / row %d — fixed=%s, matched=%d cabs, unmatched=%d (→ A%d↓), "
        "template-formulas=%d",
        title, date_row, fixed, len(matched), len(fallback_rows),
        fallback_rows[0]["row"] if fallback_rows else 0,
        len(template_batch),
    )

    return {
        "enabled": True,
        "worksheet": title,
        "date_row": date_row,
        "fixed": fixed,
        "matched": matched,
        "unmatched": fallback_rows,
        "template_formulas": len(template_batch),
    }
