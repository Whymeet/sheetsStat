"""Запись дневного отчёта build_report в Google Sheets.

Таблица — «Копия kubyshka-zaim.ru». На каждый месяц — отдельная вкладка
(«Апрель 26», «Май 26», …). Столбец A — даты `dd.mm.yyyy` (жёстко: дата —
первичный ключ строки и последний позиционный якорь).

Агрегатные колонки НЕ захардкожены буквами: перед записью читается шапка
строки 2 и каждая метрика находится по своей подписи (реестр `AGG_COLUMNS`,
у каждой метрики — каноническая подпись + номер вхождения для дублей вроде
«Приход», который есть и в C, и в блоке СМС). Нестандартные подписи бренда
переопределяются в конфиге: `google_sheets.column_labels: {key: "подпись"}`.

Метрики реестра (легаси-раскладка в скобках — только исторический default):
    prihod        (C)  — формула `={leadstech.sum}` (+ доходные ручные поля)
    zatraty       (D)  — формула Σ(расход_i * коэф_i) + <sms_cost>
    clicks_lt     (E)  — leadstech.clicks
    metrika_v     (F)  — yandex_metrika.visits
    zayavki       (G)  — Достижения/Целевые визиты цели бренда
                         (yandex_metrika.zayavki_metric: reaches|visits)
    sms_count     (Z)  — eightconnect.count (кол-во отправленных SMS)
    sms_cost      (AA) — eightconnect.cost
    sms_charge    (AC) — eightconnect.charge
    sms_clients   (AD) — всегда 0 (по требованию)
    perehody      (AG) — leadstech.hosts
    dohod_vitrina (AH) — формула `={leadstech.sum}-<sms_charge>`
(Layout сдвинут на две колонки 2026-09-02: удалены N «бекендер» и R «Долеты
и Крот»; старые листы мигрируются удалением этих колонок руками.)

Политика промаха — strict: подпись не нашлась → метрика НЕ пишется (warning
в сводке `header_warnings`, ячейку заполнит шаблонная формула строки 33, если
она там есть); формула с неразрешённым операндом не пишется целиком (молча
выкинуть слагаемое = тихо исказить числа). Если не разрешилась НИ ОДНА подпись
(шапка пустая/не прочиталась — сломанный лист, а не «подвинули колонку») —
полный фолбэк на легаси-буквы реестра.

Поля `leadstech.*` — это сумма по всем аккаунтам LeadsTech бренда
(`leadstech.accounts[]` в конфиге); разбивка лежит в `leadstech.accounts` отчёта
и в таблицу не пишется.

Кабинетная зона — от CABINET_START_COL (AP, фиксированная граница: определять
её динамически ненадёжно, у брендов разметка строки 1 различается). В строке 2
лежат названия рекламных кабинетов: по каждому spent'у из отчёта ищем свою
колонку и пишем spent в `{col}{date_row}`. Кабинеты, которых нет в шапке (в т.ч.
из-за того, что лист физически уже EZ), в таблицу не пишутся — видны в
JSON-отчёте и в счётчике `unmatched`. Коэффициент кабинета для формулы «Затрат»
берётся из строки 1 над его именем: число → множитель, пусто/символ → 1
(см. `_parse_coeff` / `_build_zatraty_formula`).
"""
from __future__ import annotations

import calendar
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger("sheetsstat.sheets_writer")


RU_MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

# Реестр агрегатных метрик: key -> (каноническая подпись строки 2,
# номер вхождения слева направо (1-based, для дублей подписей),
# легаси-буква — используется ТОЛЬКО при полном фолбэке, когда не разрешилась
# ни одна подпись). Подпись метрики можно переопределить per-brand через
# конфиг `google_sheets.column_labels: {key: "подпись"}` (вхождение тогда 1).
AGG_COLUMNS: Dict[str, Tuple[str, int, str]] = {
    "prihod":        ("Приход",          1, "C"),   # формула
    "zatraty":       ("Затраты",         1, "D"),   # формула
    "clicks_lt":     ("Клики лт",        1, "E"),
    "metrika_v":     ("Метрика визиты",  1, "F"),
    "zayavki":       ("Заявки с сайта",  1, "G"),
    "sms_count":     ("кол-во смсок",    1, "Z"),
    "sms_cost":      ("Расход",          1, "AA"),
    "sms_charge":    ("Приход",          2, "AC"),  # дубль «Приход» — 2-е вхождение (блок СМС)
    "sms_clients":   ("Клиенты",         1, "AD"),  # пишется литерал 0
    "perehody":      ("Переходы Уники",  1, "AG"),
    "dohod_vitrina": ("Доход с витрины", 1, "AH"),  # формула
}

# Семантика метрик — что именно пишется в колонку. Показывается во вкладке
# «Колонки» UI рядом с редактируемой подписью (GET /api/sheets/columns).
AGG_COLUMN_DESCRIPTIONS: Dict[str, str] = {
    "prihod":        "Формула: доход вебмастера LeadsTech (литерал) + Σ ручных доходных полей",
    "zatraty":       "Формула: Расход СМС + Σ(кабинет × коэффициент из строки 1)",
    "clicks_lt":     "LeadsTech: уники (uniques) по sub1, сумма по всем аккаунтам",
    "metrika_v":     "Яндекс.Метрика: визиты счётчика за день",
    "zayavki":       "Яндекс.Метрика: число цели «Zayvka» (или первой цели бренда)",
    "sms_count":     "8connect: количество отправленных SMS (count)",
    "sms_cost":      "8connect: расход на рассылку (cost)",
    "sms_charge":    "8connect: доход с рассылки (charge)",
    "sms_clients":   "Всегда пишется 0 (по требованию); в «Приход» не входит",
    "perehody":      "LeadsTech: уникальные хосты (hosts) по sub1",
    "dohod_vitrina": "Формула: доход вебмастера LeadsTech (sumwebmaster) − Приход СМС",
}

# «Всегда-плоские» слагаемые формулы «Затраты» (множитель 1) вне диапазона
# кабинетов: расход 8connect (лежит до AP). Коэффициенты кабинетных колонок
# читаются динамически из строки 1 листа над именем кабинета
# (см. _parse_coeff): число → множитель, пусто/символ → 1.
ZATRATY_PLAIN_KEYS: Tuple[str, ...] = ("sms_cost",)

# Граница кабинетной зоны: имена кабинетов — в строке 2 от AP, коэффициенты
# (НДС/комиссии) — в строке 1 над именем. Левее AP — агрегатная зона (подписи
# метрик реестра A..AK + служебные AL..AO). Граница фиксированная: определять
# её динамически по строке 1 ненадёжно (разметка у брендов различается).
# AP = прежний AR минус две удалённые колонки (N «бекендер», R «Долеты и
# Крот») — ровно то, что получается в старых листах после их удаления.
# EZ — верхняя граница «с запасом» (под будущий рост числа кабинетов), но лист
# конкретного месяца может быть уже неё: Google Sheets не даст прочитать
# диапазон, выходящий за пределы физической сетки листа. Поэтому диапазон
# чтения шапки в _read_header() всегда обрезается по ws.col_count — раздувать
# лист пустыми столбцами под эту константу не нужно.
CABINET_START_COL = "AP"
CABINET_MAX_COL    = "EZ"


def cabinet_bounds(config_or_gs: Dict[str, Any]) -> Tuple[int, int]:
    """(start_idx, max_idx) кабинетной зоны бренда.

    Область редактируется в конфиге: `google_sheets.cabinet_start_col` /
    `cabinet_max_col` (дефолт AP..EZ). Начало не может залезать в агрегатную
    зону (подписи метрик реестра A..AK): всё левее первой колонки после
    реестра (AL) молча поднимается до неё — иначе агрегатные колонки
    посчитались бы кабинетами.
    """
    import metrics as metrics_mod
    agg_end_idx = max(_col_index(c) for c, _m in metrics_mod.layout_columns())
    gs = config_or_gs.get("google_sheets", config_or_gs) or {}

    def _parse(v: Any, default: str) -> int:
        s = str(v or "").strip().upper()
        return _col_index(s) if s and s.isalpha() else _col_index(default)

    start = max(_parse(gs.get("cabinet_start_col"), CABINET_START_COL),
                agg_end_idx + 1)
    end = _parse(gs.get("cabinet_max_col"), CABINET_MAX_COL)
    return start, max(end, start)

TARGET_GOAL_FOR_ZAYAVKI = "Zayvka"

# Строка-эталон формул. Читаем её один раз на вкладку и вставляем все
# обнаруженные формулы в новую строку даты (с заменой ссылок на строку).
TEMPLATE_ROW = 33
_TEMPLATE_RANGE = f"A{TEMPLATE_ROW}:ZZ{TEMPLATE_ROW}"
# \b нужен, чтобы 33 не цеплялось посреди 330/133, но срабатывало на AR33 / AR$33.
_TEMPLATE_ROW_RE = re.compile(rf"(\$?[A-Z]+\$?){TEMPLATE_ROW}\b")
# Кэш формул по (spreadsheet_id, title вкладки) — один раз на процесс.
# spreadsheet_id в ключе обязателен: процесс обслуживает все бренды, а title
# вида «Август 26» одинаков во всех таблицах.
_TEMPLATE_CACHE: Dict[Tuple[str, str], Dict[str, str]] = {}

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


def _read_header(ws: Any, max_idx: Optional[int] = None) -> Tuple[List[Any], List[str]]:
    """Читает шапку листа одним запросом: `A1:{end}2`, где end обрезан по
    реальной ширине листа (CABINET_MAX_COL — лишь запас на будущее).

    Возвращает `(coeff_row, label_row)`: строка 1 (коэффициенты «Затрат» над
    кабинетами) и строка 2 (подписи агрегатных колонок + имена кабинетов).
    Индекс в списке = номер колонки − 1; короткие/пустые строки паддятся,
    т.к. Sheets возвращает их укороченными (в т.ч. из-за merged-ячеек).

    Рендер — UNFORMATTED_VALUE: коэффициенты приходят точными числами
    (формат ячейки округляет: 1.048 показывается как «1,05»), а текстовые
    подписи в этом рендере не меняются.

    Не кэшируется: свежая шапка на каждый прогон — это и есть смысл
    динамической привязки (колонки двигают между запусками).
    """
    end_idx = min(max_idx or _col_index(CABINET_MAX_COL), ws.col_count)
    rng = f"A1:{_col_letter(end_idx)}2"
    try:
        rows = ws.get(rng, value_render_option="UNFORMATTED_VALUE")
    except TypeError:  # старый gspread без kwarg'а
        rows = ws.get(rng)
    rows = list(rows or [])
    while len(rows) < 2:
        rows.append([])
    return rows[0], rows[1]


def _resolve_agg_columns(
    label_row: List[Any],
    labels_override: Dict[str, str],
    cabinet_start_idx: Optional[int] = None,
) -> Tuple[Dict[str, str], List[str]]:
    """Находит букву колонки для каждой метрики AGG_COLUMNS по подписи в
    строке 2. Возвращает `({key: буква}, [warning, ...])`.

    Ищем только левее CABINET_START_COL — кабинет с именем «Приход» не должен
    сбивать счёт вхождений. Промах подписи → key отсутствует в результате
    (strict-политика — писать по старой букве опаснее, чем не писать).
    Не разрешился ни один key → полный фолбэк на легаси-буквы реестра
    (шапка пустая/не прочиталась — это сломанный лист, а не сдвиг колонок).
    """
    if cabinet_start_idx is None:
        cabinet_start_idx = _col_index(CABINET_START_COL)
    by_label: Dict[str, List[str]] = {}
    for offset, cell in enumerate(label_row):
        if offset + 1 >= cabinet_start_idx:
            break
        norm = _norm(str(cell))
        if norm:
            by_label.setdefault(norm, []).append(_col_letter(offset + 1))

    cols: Dict[str, str] = {}
    warnings: List[str] = []
    for key, (label, occurrence, _legacy) in AGG_COLUMNS.items():
        override = (labels_override or {}).get(key)
        if override:
            label, occurrence = override, 1
        hits = by_label.get(_norm(label), [])
        if len(hits) >= occurrence:
            cols[key] = hits[occurrence - 1]
        else:
            warnings.append(
                f"подпись {label!r}"
                + (f" (вхождение {occurrence})" if occurrence > 1 else "")
                + f" не найдена в строке 2 — метрика {key} не записана"
            )

    # Sanity поверх порядкового номера: «Приход» блока СМС обязан быть правее
    # «Расхода». Если нет — кто-то вставил ещё один «Приход» между дублями,
    # порядковый счёт сбился, доверять sms_charge нельзя.
    if "sms_charge" in cols and "sms_cost" in cols:
        if _col_index(cols["sms_charge"]) <= _col_index(cols["sms_cost"]):
            warnings.append(
                f"колонка sms_charge ({cols['sms_charge']}) левее sms_cost "
                f"({cols['sms_cost']}) — похоже, появился лишний дубль подписи; "
                "метрика sms_charge не записана"
            )
            del cols["sms_charge"]

    if not cols:
        warnings = [
            "ни одна подпись агрегатных колонок не найдена в строке 2 — "
            "полный фолбэк на легаси-раскладку "
            + ", ".join(f"{k}={v[2]}" for k, v in AGG_COLUMNS.items())
        ]
        cols = {key: legacy for key, (_l, _o, legacy) in AGG_COLUMNS.items()}

    return cols, warnings


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


def month_title(day: date) -> str:
    """Каноническое имя месячной вкладки: «Сентябрь 26»."""
    return f"{RU_MONTHS[day.month - 1]} {day.year % 100:02d}"


def _norm_title(title: Any) -> str:
    return " ".join(str(title or "").split()).lower()


def find_month_worksheet(spreadsheet: Any, day: date) -> Optional[Any]:
    """Вкладка месяца: сначала точное имя, затем без учёта регистра/пробелов
    («май 26» ≡ «Май 26»). Иначе автосоздание плодило бы дубликаты вкладок
    для старых месяцев, названных с маленькой буквы."""
    title = month_title(day)
    try:
        return spreadsheet.worksheet(title)
    except Exception:
        pass
    try:
        wanted = _norm_title(title)
        for ws in spreadsheet.worksheets():
            if _norm_title(getattr(ws, "title", "")) == wanted:
                return ws
    except Exception as e:
        logger.warning("Sheets: не удалось перечислить вкладки: %s", e)
    return None


def _pick_worksheet(spreadsheet: Any, day: date) -> Tuple[Optional[Any], str]:
    """Вернуть (worksheet, фактический title). Если не нашли — (None, каноническое имя)."""
    ws = find_month_worksheet(spreadsheet, day)
    if ws is None:
        return None, month_title(day)
    return ws, str(getattr(ws, "title", None) or month_title(day))


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


def _load_template_formulas(ws: Any, spreadsheet_id: str, title: str) -> Dict[str, str]:
    """Читает формулы из строки-эталона TEMPLATE_ROW вкладки.

    Возвращает `{col_letter: formula}` только для ячеек, начинающихся с `=`.
    Результат кэшируется по (spreadsheet_id, title) на всё время жизни процесса.
    """
    cache_key = (spreadsheet_id, title)
    cached = _TEMPLATE_CACHE.get(cache_key)
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
    _TEMPLATE_CACHE[cache_key] = template
    return template


def _substitute_template_row(formula: str, target_row: int) -> str:
    """Заменяет в формуле ссылки вида AR33 / $AR$33 на тот же столбец, но с target_row."""
    return _TEMPLATE_ROW_RE.sub(rf"\g<1>{target_row}", formula)


def _build_prihod_formula(row: int, sumwebmaster: float,
                          income_cols: Optional[List[str]] = None) -> str:
    """Формула «Приход»: `={leadstech.sum}` + колонки доходных ручных полей
    (target="prihod"). Литерал — полная сумма вебмастера LeadsTech (как в
    «Доходе с витрины», но без вычета СМС). Приход СМС и «Клиенты» в приход
    не входят (решение пользователя, 2026-09-02).
    """
    base = "=" + f"{sumwebmaster:.2f}".replace(".", ",")
    for col in income_cols or []:
        base += f"+{col}{row}"
    return base


def _build_dohod_vitrina_formula(row: int, sumwebmaster: float, cols: Dict[str, str]) -> str:
    """Формула «Доход с витрины»: `={sumwebmaster}-<sms_charge>{row}`.

    Число форматируем с запятой — таблица в русской локали (USER_ENTERED).
    """
    literal = f"{sumwebmaster:.2f}".replace(".", ",")
    return f"={literal}-{cols['sms_charge']}{row}"


def _build_zatraty_formula(row: int, coeffs: Dict[str, float], plain_cols: List[str]) -> str:
    """Собирает формулу «Затрат» для заданной строки.

    `coeffs` — карта `колонка → множитель` по кабинетам с именем в строке 2
    (коэффициент из строки 1 листа; пусто/символ → 1). `plain_cols` —
    разрешённые колонки ZATRATY_PLAIN_KEYS, добавляются как есть
    (множитель 1), вне диапазона кабинетов.

    Формат коэффициента — с запятой в качестве десятичного разделителя,
    т.к. таблица в русской локали (USER_ENTERED парсит по локали).
    """
    parts: List[str] = [f"{col}{row}" for col in plain_cols]
    for col, k in coeffs.items():
        if k == 1:
            parts.append(f"{col}{row}")
        else:
            parts.append(f"{col}{row}*{f'{k:g}'.replace('.', ',')}")
    return "=" + "+".join(parts)


def _parse_coeff(cell: Any) -> float:
    """Коэффициент из ячейки строки 1. Пусто / не-число / ≤0 → 1.0.

    Понимает оба рендера ячейки: UNFORMATTED_VALUE (число) и FORMATTED_VALUE
    (строка «1,16» — запятая приводится к точке).
    """
    if isinstance(cell, bool):          # bool — подтип int, отсекаем явно
        return 1.0
    if isinstance(cell, (int, float)):
        k = float(cell)
    else:
        s = str(cell or "").strip().replace(",", ".")
        if not s:
            return 1.0
        try:
            k = float(s)
        except ValueError:
            return 1.0
    return k if 0.0 < k < float("inf") else 1.0


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


@dataclass
class SheetContext:
    """Открытый лист + прочитанная шапка + окно строки даты.

    Открывается ДО вычисления метрик (build_report читает отсюда текущие
    значения ручных кабинетных колонок AVITO/Google), затем передаётся в
    write_daily_report — лист не открывается дважды.
    """
    enabled: bool = True
    error: Optional[str] = None
    spreadsheet: Any = None
    spreadsheet_id: str = ""
    ws: Any = None
    title: str = ""
    date_row: Optional[int] = None
    created: bool = False  # вкладка месяца создана этим прогоном (sheet_builder)
    coeff_row: List[Any] = field(default_factory=list)
    label_row: List[Any] = field(default_factory=list)
    row_values: List[Any] = field(default_factory=list)  # окно строки даты (UNFORMATTED)


def open_sheet_context(
    config: Dict[str, Any],
    day: date,
    *,
    spreadsheet: Any = None,
    report: Optional[Dict[str, Any]] = None,
) -> SheetContext:
    """Открывает таблицу/вкладку/строку даты и читает шапку + окно строки.

    Недостающая месячная вкладка создаётся из реестра метрик (sheet_builder)
    всегда; `report` даёт имена кабинетов текущего дня для шапки создаваемой
    вкладки. Если генерация упала — ошибка в ctx.error, запись пропускается.
    """
    gs_cfg = config.get("google_sheets") or {}
    if not gs_cfg.get("enabled", False):
        return SheetContext(enabled=False)

    if spreadsheet is None:
        spreadsheet = _build_gsheets_spreadsheet(config)
        if spreadsheet is None:
            return SheetContext(error="не удалось открыть spreadsheet (см. логи)")

    ctx = SheetContext(
        spreadsheet=spreadsheet,
        spreadsheet_id=str(getattr(spreadsheet, "id", None)
                           or gs_cfg.get("spreadsheet_id") or ""),
    )
    ws, title = _pick_worksheet(spreadsheet, day)
    ctx.title = title
    if ws is None:
        try:
            import sheet_builder
            extra = [name for name, _s, _src in _collect_cabinets(report or {})]
            ws, ctx.created = sheet_builder.ensure_month_worksheet(
                spreadsheet, day, config, extra_cabinets=extra,
            )
            ctx.title = str(getattr(ws, "title", None) or title)
        except Exception as e:
            logger.error("Sheets/%s: автосоздание вкладки упало: %s", title, e,
                         exc_info=True)
            ctx.error = f"вкладка {title!r} не найдена, автосоздание упало: {e}"
            return ctx
    if ws is None:
        ctx.error = f"вкладка {title!r} не найдена"
        return ctx
    ctx.ws = ws

    ctx.date_row = _find_date_row(ws, day)
    if ctx.date_row is None:
        ctx.error = f"дата {day.strftime('%d.%m.%Y')} не найдена в столбце A"
        return ctx

    _start_idx, _max_idx = cabinet_bounds(gs_cfg)
    try:
        ctx.coeff_row, ctx.label_row = _read_header(ws, _max_idx)
    except Exception as e:
        logger.warning("Sheets/%s: не удалось прочитать шапку A1:…2: %s", title, e)

    # Окно строки даты — источник значений ручных кабинетных колонок. Ошибка
    # чтения не фатальна: они просто посчитаются как 0.
    try:
        end_idx = min(_max_idx, ws.col_count)
        rng = f"A{ctx.date_row}:{_col_letter(end_idx)}{ctx.date_row}"
        try:
            rows = ws.get(rng, value_render_option="UNFORMATTED_VALUE")
        except TypeError:  # старый gspread
            rows = ws.get(rng)
        ctx.row_values = rows[0] if rows else []
    except Exception as e:
        logger.warning("Sheets/%s: не удалось прочитать строку даты %s: %s",
                       title, ctx.date_row, e)
    return ctx


def _cell_to_float(v: Any) -> Optional[float]:
    """UNFORMATTED-ячейка → float|None (ошибки листа #DIV/0! и пр. → None)."""
    if v is None or v == "" or isinstance(v, bool):
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


def _ctx_row_value(ctx: SheetContext, col_letter: str) -> Optional[float]:
    i = _col_index(col_letter) - 1
    return _cell_to_float(ctx.row_values[i] if i < len(ctx.row_values) else None)


def manual_cabinet_entries(gs_cfg: Dict[str, Any]) -> List[Dict[str, str]]:
    """Нормализованные ручные поля: [{label, name, target}].

    Конфиг может содержать строки (legacy) или объекты — приводим к одному
    виду; label — подпись колонки в кабинетной зоне листа, name — название
    в системе, target — куда суммируется значение: "zatraty" (расход,
    формула D; default) или "prihod" (доход, слагаемое формулы C).
    """
    out: List[Dict[str, str]] = []
    for item in gs_cfg.get("manual_cabinets") or []:
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


def manual_income_labels_norm(gs_cfg: Dict[str, Any]) -> set:
    """Нормализованные подписи ДОХОДНЫХ ручных полей (target=prihod).

    Их колонки в кабинетной зоне исключаются из формулы «Затрат» и
    суммируются в «Приход» (manual_income).
    """
    return {_norm(e["label"]) for e in manual_cabinet_entries(gs_cfg)
            if e["target"] == "prihod"}


def manual_cabinet_labels(gs_cfg: Dict[str, Any]) -> List[str]:
    return [e["label"] for e in manual_cabinet_entries(gs_cfg)]


def zayavki_value(config: Dict[str, Any], report: Dict[str, Any]) -> int:
    """Число цели Метрики для «Заявок с сайта» (столбец G)."""
    ym = report.get("yandex_metrika") or {}
    zayavki_metric = ((config.get("yandex_metrika") or {}).get("zayavki_metric")) or "visits"
    if zayavki_metric not in ("reaches", "visits"):
        zayavki_metric = "visits"
    return _find_goal_value(ym, TARGET_GOAL_FOR_ZAYAVKI, zayavki_metric)


def compute_cabinet_spend(
    ctx: Optional[SheetContext],
    config: Dict[str, Any],
    report: Dict[str, Any],
) -> Tuple[Optional[float], List[str], Dict[str, Optional[float]]]:
    """Предсказывает слагаемое кабинетов формулы «Затрат» ровно как лист.

    С живым листом: идём по колонкам кабинетной зоны (подпись строки 2
    непуста); значение колонки = свежий spent из отчёта (если кабинет отчёта
    матчится в эту колонку — так же его запишет writer) либо текущее значение
    ячейки (ручные AVITO/Google и колонки, которые сегодняшний прогон не
    трогает); множитель — из строки 1. Это в точности сумма, которую даст
    формула D после записи. Кабинеты отчёта БЕЗ колонки в шапке в лист не
    попадают и в сумму не входят (как и в формулу D); они видны в сводке
    записи (`unmatched`).

    Без листа (enabled=false/ошибка): фолбэк — Σ spent×коэф из конфига
    `google_sheets.cabinet_coeffs` (дефолт 1); ручные значения недоступны.

    Возвращает (сумма|None, warnings, {label ручного поля: значение|None}).
    """
    gs_cfg = config.get("google_sheets") or {}
    cfg_coeffs = {_norm(k): float(v) for k, v in (gs_cfg.get("cabinet_coeffs") or {}).items()}
    warnings: List[str] = []
    cabinets = _collect_cabinets(report)
    manual_vals: Dict[str, Optional[float]] = {
        e["label"]: None for e in manual_cabinet_entries(gs_cfg)
    }

    if ctx is None or ctx.error or not ctx.enabled or not ctx.label_row:
        if not cabinets:
            return None, warnings, manual_vals
        total = sum(float(spent) * cfg_coeffs.get(_norm(name), 1.0)
                    for name, spent, _src in cabinets)
        return total, warnings, manual_vals

    headers_map: Dict[str, str] = {}
    coeffs_map: Dict[str, float] = {}
    income_norm = manual_income_labels_norm(gs_cfg)
    start_idx, _ = cabinet_bounds(gs_cfg)
    for idx0 in range(start_idx - 1, len(ctx.label_row)):
        norm = _norm(str(ctx.label_row[idx0]))
        if not norm:
            continue
        col = _col_letter(idx0 + 1)
        headers_map.setdefault(norm, col)
        if norm in income_norm:
            continue  # доходное поле: не слагаемое Затрат (уйдёт в Приход)
        raw = ctx.coeff_row[idx0] if idx0 < len(ctx.coeff_row) else None
        coeffs_map[col] = _parse_coeff(raw)

    # свежие spent по колонкам — как их запишет writer (суммирование дублей)
    by_col_spent: Dict[str, float] = {}
    for name, spent, _source in cabinets:
        col = _find_header_column(headers_map, name)
        if col is None:
            continue
        by_col_spent[col] = by_col_spent.get(col, 0.0) + float(spent)
        # расхождение коэффициентов конфиг vs строка 1 — громко
        cfg_k = cfg_coeffs.get(_norm(name))
        if cfg_k is not None and abs(cfg_k - coeffs_map.get(col, 1.0)) > 1e-9:
            warnings.append(
                f"кабинет {name!r}: коэффициент в конфиге {cfg_k} ≠ строке 1 "
                f"листа {coeffs_map.get(col)} — считаю по листу"
            )

    total = 0.0
    for col, k in coeffs_map.items():
        v = by_col_spent.get(col)
        if v is None:
            v = _ctx_row_value(ctx, col)  # ручные/нетронутые колонки — из ячейки
        if v is not None:
            total += v * k

    # значения зарегистрированных ручных полей — для отчёта/UI
    for label in manual_vals:
        col = headers_map.get(_norm(label))
        if col is not None:
            manual_vals[label] = _ctx_row_value(ctx, col)
    return total, warnings, manual_vals


def write_daily_report(
    config: Dict[str, Any],
    day: date,
    report: Dict[str, Any],
    *,
    spreadsheet: Any = None,
    dry_run: bool = False,
    context: Optional["SheetContext"] = None,
) -> Dict[str, Any]:
    """Пишет отчёт в Google Sheets. Возвращает сводку для API-ответа.

    `context` — заранее открытый open_sheet_context (build_report открывает
    его до вычисления метрик, чтобы прочитать ручные значения); без него
    контекст открывается здесь — сигнатура обратно совместима.

    При `dry_run=True` в таблицу ничего не пишется, а собранный batch
    возвращается в сводке (ключ "batch") — для отладки/дифф-проверок.

    Структура результата:
        {
          "enabled": True,
          "worksheet": "Апрель 26",
          "date_row": 20,
          "matched": [{"name", "spent", "column", "source"}, ...],
          "unmatched": [{"name", "spent", "source"}, ...],  # в таблицу не пишутся
          "fixed": {"prihod": .., "clicks_lt": .., ...},
          "resolved_columns": {"prihod": "C", ...},  # привязка метрик по шапке
          "header_warnings": ["...", ...],           # промахи подписей и т.п.
          "skipped_metrics": ["clicks_lt", ...],     # не записаны (strict)
          "error": "... (если что-то сломалось)",
        }
    """
    gs_cfg = config.get("google_sheets") or {}
    if context is None:
        context = open_sheet_context(config, day, spreadsheet=spreadsheet, report=report)
    if not context.enabled:
        return {"enabled": False, "reason": "google_sheets.enabled = false"}
    if context.error:
        out: Dict[str, Any] = {"enabled": True, "error": context.error}
        if context.ws is not None:  # вкладка нашлась, не нашлась дата — как раньше
            out["worksheet"] = context.title
        return out

    spreadsheet = context.spreadsheet
    ws, title = context.ws, context.title
    date_row = context.date_row
    coeff_row, label_row = context.coeff_row, context.label_row

    leadstech = report.get("leadstech") or {}
    ym = report.get("yandex_metrika") or {}
    ec = report.get("eightconnect") or {}
    zayavki_val = zayavki_value(config, report)

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

    # Managed-режим: формулы строки даты генерятся из реестра метрик
    # (metrics.py), шаблонная строка 33 не используется вовсе.
    managed = bool(gs_cfg.get("managed_formulas"))

    # Шаблонные формулы из строки 33 (только legacy) — идут ПЕРВЫМИ; если наша
    # логика пишет в ту же колонку, последняя запись в batch_update перекроет
    # шаблон.
    # Даты месяца живут в строках 3..2+N; строка 33 — дата только в 31-дневных
    # месяцах. В сгенерированных вкладках коротких месяцев в 33 стоит ИТОГОВАЯ
    # строка (SUM/AVERAGEIF) — клонировать её в строку даты нельзя (получались
    # =SUM(B3:B32) в B3 и циклические ссылки). В старых ручных листах строка 33
    # коротких месяцев пуста, так что поведение для них не меняется.
    days_in_month = calendar.monthrange(day.year, day.month)[1]
    last_date_row = 2 + days_in_month
    template: Dict[str, str] = {}
    if not managed and TEMPLATE_ROW > last_date_row:
        logger.info("Sheets/%s: строка %d за пределами дат месяца (3..%d) — шаблон не клонируется",
                    title, TEMPLATE_ROW, last_date_row)
    elif not managed:
        try:
            template = _load_template_formulas(ws, context.spreadsheet_id, title)
        except Exception as e:
            logger.warning("Sheets/%s: не удалось загрузить шаблон строки %d: %s",
                           title, TEMPLATE_ROW, e)

    template_batch: List[Dict[str, Any]] = [
        {"range": f"{col}{date_row}",
         "values": [[_substitute_template_row(formula, date_row)]]}
        for col, formula in template.items()
    ]

    # Шапка (строка 1 — коэффициенты, строка 2 — подписи/кабинеты) уже
    # прочитана в контексте; D-формула строится из coeffs_map ниже.
    cab_start_idx, _cab_max_idx = cabinet_bounds(gs_cfg)
    cols, header_warnings = _resolve_agg_columns(
        label_row, gs_cfg.get("column_labels") or {}, cab_start_idx
    )
    if context.created:
        header_warnings.insert(0, f"вкладка {title!r} создана автоматически из реестра")
    if _norm(str(label_row[0] if label_row else "")) != "дата":
        header_warnings.append(
            "в A2 не «Дата» — проверь, что даты по-прежнему в столбце A"
        )

    headers_map: Dict[str, str] = {}
    coeffs_map: Dict[str, float] = {}
    income_norm = manual_income_labels_norm(gs_cfg)
    income_cols: List[str] = []  # колонки доходных ручных полей (слагаемые C)
    start_col_idx = cab_start_idx
    if len(label_row) < start_col_idx:
        logger.warning(
            "Sheets/%s: шапка кончается до колонки %s — кабинеты в этом "
            "месяце не будут сматчены", title, _col_letter(start_col_idx),
        )
    for idx0 in range(start_col_idx - 1, len(label_row)):
        norm = _norm(str(label_row[idx0]))
        if not norm:
            continue
        col = _col_letter(idx0 + 1)
        headers_map.setdefault(norm, col)
        if norm in income_norm:
            income_cols.append(col)  # в Затраты не входит
            continue
        # Коэффициент — из строки 1 над именем (та же колонка). Пусто → 1.
        raw = coeff_row[idx0] if idx0 < len(coeff_row) else None
        coeffs_map[col] = _parse_coeff(raw)

    logger.info("Sheets: привязка метрик: %s", cols)
    if header_warnings:
        logger.warning("Sheets/%s: header warnings: %s", title, header_warnings)
    logger.info("Sheets: заголовки шапки (%d): %s", len(headers_map),
                {v: k for k, v in list(headers_map.items())})
    non_unit = {c: k for c, k in coeffs_map.items() if k != 1}
    logger.info("Sheets: коэффициенты ≠1 (%d): %s", len(non_unit), non_unit)

    batch: List[Dict[str, Any]] = list(template_batch)
    skipped_metrics: List[str] = []

    def _add(key: str, value: Any) -> None:
        col = cols.get(key)
        if col is None:
            skipped_metrics.append(key)
            return
        batch.append({"range": f"{col}{date_row}", "values": [[value]]})

    def _add_formula(key: str, operands: Tuple[str, ...], build) -> None:
        # Strict: формулу с неразрешённым операндом не пишем целиком — молча
        # выкинуть слагаемое значит тихо исказить сумму. Ячейку в этом случае
        # заполнит шаблонная формула строки 33 (template_batch выше).
        missing = [k for k in operands if k not in cols]
        if missing:
            skipped_metrics.append(key)
            header_warnings.append(
                f"формула {key} не записана: не разрешены операнды "
                + ", ".join(missing)
            )
            return
        _add(key, build())

    # sms_cost — слагаемое формулы D; sms_charge — операнд dohod_vitrina/prihod.
    _add("sms_cost",    fixed["eightconnect_cost"])
    _add("sms_charge",  fixed["eightconnect_charge"])
    _add("sms_count",   fixed["eightconnect_count"])
    _add("sms_clients", 0)  # по требованию всегда 0
    if not managed:
        # Legacy: три формулы как раньше, остальные колонки — клон строки 33.
        _add_formula("dohod_vitrina", ("sms_charge",),
                     lambda: _build_dohod_vitrina_formula(date_row, fixed["prihod"], cols))
        _add_formula("prihod", (),
                     lambda: _build_prihod_formula(date_row, fixed["prihod"], income_cols))
        _add_formula("zatraty", ZATRATY_PLAIN_KEYS,
                     lambda: _build_zatraty_formula(
                         date_row, coeffs_map, [cols[k] for k in ZATRATY_PLAIN_KEYS]))
    _add("clicks_lt", fixed["clicks_lt"])
    _add("metrika_v", fixed["metrika_v"])
    _add("zayavki",   fixed["zayavki"])
    _add("perehody",  fixed["perehody"])

    if managed:
        # Формулы ВСЕХ вычисляемых метрик из реестра — самовосстановление
        # после ручных правок. Колонки — по подписям строки 2 (полный реестр).
        # Ручные кабинетные колонки здесь не пишутся никогда.
        import metrics as metrics_mod
        reg_cols, reg_warns = metrics_mod.resolve_registry_columns(
            label_row, gs_cfg.get("column_labels") or {}, cab_start_idx
        )
        a1ctx = metrics_mod.A1Context(
            colmap=reg_cols, row=date_row,
            literals={"lt_sumwebmaster": fixed["prihod"]},
            cabinet_terms=sorted(coeffs_map.items(), key=lambda ck: _col_index(ck[0])),
            income_terms=sorted(income_cols, key=_col_index),
        )
        hidden = {k for k, m in metrics_mod.METRICS.items() if m.col is None}
        for key in metrics_mod.computed_keys():
            col = reg_cols.get(key)
            if col is None:
                skipped_metrics.append(key)
                continue
            deps = set(metrics_mod._expr_names(metrics_mod._PARSED[key]))
            missing = [d for d in deps
                       if d not in metrics_mod._PSEUDO_VARS and d not in hidden
                       and d not in reg_cols]
            if missing:
                skipped_metrics.append(key)
                labels = ", ".join(
                    f"«{(metrics_mod.METRICS[d].label or d)}»" for d in missing)
                header_warnings.append(
                    f"managed: формула {key} не записана — не найдена колонка "
                    f"операнда {labels}")
                continue
            try:
                formula = metrics_mod.expr_to_a1(key, a1ctx)
            except Exception as e:
                skipped_metrics.append(key)
                header_warnings.append(f"managed: формула {key} не записана: {e}")
                continue
            if formula is None:
                continue  # выродилась (пустой manual_income) — намеренно
            batch.append({"range": f"{col}{date_row}", "values": [[formula]]})
        if reg_warns:
            header_warnings.extend(f"managed: {w}" for w in reg_warns)

        # Итоговая строка месяца (оцифровка строки 34 эталона) — формулы из
        # реестра переписываются каждый прогон: самовосстановление + новые
        # кабинетные колонки автоматически получают SUM. Реордер-устойчиво:
        # колонки резолвятся по подписям (reg_cols/coeffs_map).
        first_data, last_data = 3, last_date_row  # даты в строках 3..2+N
        totals_row = last_data + 1
        if getattr(ws, "row_count", totals_row) < totals_row:
            header_warnings.append(
                f"managed: итоговая строка {totals_row} за пределами листа — пропущена")
        else:
            t_ctx = metrics_mod.A1Context(
                colmap=reg_cols, row=totals_row, literals={},
                cabinet_terms=[], income_terms=[],
            )
            for key in metrics_mod.TOTALS:
                try:
                    f = metrics_mod.total_formula(key, t_ctx, first_data, last_data)
                except Exception:
                    f = None  # неразрешённый операнд expr-итога — пропуск
                if f:
                    batch.append({"range": f"{reg_cols[key]}{totals_row}",
                                  "values": [[f]]})
            for col in sorted(set(coeffs_map) | set(income_cols), key=_col_index):
                batch.append({"range": f"{col}{totals_row}",
                              "values": [[f"=SUM({col}{first_data}:{col}{last_data})"]]})

    # --- Матчинг кабинетов (headers_map собран выше вместе с коэффициентами) ---
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

    # Кабинеты без колонки в шапке в таблицу НЕ пишем (раньше складывались
    # «про запас» в A37↓ и бесконечно копились, пока лист не упирался в лимит
    # 1000 строк). Они остаются в JSON-отчёте и в сводке ниже.
    unmatched_out = [
        {"name": name, "spent": spent, "source": source}
        for name, spent, source in unmatched
    ]

    if batch and not dry_run:
        ws.batch_update(batch, value_input_option="USER_ENTERED")

    logger.info(
        "Sheets: %s / row %d — fixed=%s, matched=%d cabs, unmatched=%d (в таблицу не пишутся), "
        "template-formulas=%d, skipped=%s%s",
        title, date_row, fixed, len(matched), len(unmatched_out),
        len(template_batch), skipped_metrics or "—",
        " [DRY RUN]" if dry_run else "",
    )

    summary: Dict[str, Any] = {
        "enabled": True,
        "worksheet": title,
        "date_row": date_row,
        "fixed": fixed,
        "matched": matched,
        "unmatched": unmatched_out,
        "template_formulas": len(template_batch),
        "resolved_columns": cols,
        "header_warnings": header_warnings,
        "skipped_metrics": skipped_metrics,
    }
    if dry_run:
        summary["dry_run"] = True
        summary["batch"] = batch
    return summary
