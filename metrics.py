"""Семантический реестр метрик: единый источник правды для расчётов и Sheets.

Метрики бывают трёх видов:
- **base_service** — запрашиваемые: собираются из сервисов (LeadsTech, Метрика,
  8connect) в core.build_report; `source` — путь в отчёте ("leadstech.clicks")
  или спец-ключ ("metrika.goal"); `literal` — фиксированное значение
  (sms_clients = 0 по требованию).
- **base_manual** — вводятся людьми прямо в таблице (R «Долеты и Крот»);
  бэкенд читает значение обратно из строки листа и никогда не перезаписывает.
  Ручные колонки кабинетной зоны (AVITO, Google) в реестре НЕ живут — они
  описываются в конфиге бренда `google_sheets.manual_cabinets` и входят в
  cabinet_spend с коэффициентом 1.
- **computed** — считаются по `expr` (мини-DSL, синтаксис Python: + - * /,
  скобки, имена метрик, числа). Одно выражение интерпретируется дважды:
  бэкендом (eval_metrics: None-семантика, деление на 0 → None) и генератором
  A1-формул для Sheets (expr_to_a1). Формулы воспроизводят живую таблицу
  ОДИН В ОДИН (решение пользователя): деления НЕ оборачиваются в IFERROR —
  в таблице возможен #DIV/0!, у бэкенда в этой ситуации None.

Оцифровано с эталонной таблицы бренда osnovnoy «Кубыха» (лист «Август 26»).
Колонки AN..AQ эталона (AN=дубль AL, AP=AO*72, AQ=AP/J) признаны мусором и
не переносятся; формула =IFERROR(W/J) в колонке R старых строк — баг, R
всегда чисто ручной ввод.

Псевдопеременная `cabinet_spend` (Σ spent_кабинета × коэффициент + ручные
кабинетные колонки × 1) не является метрикой реестра: бэкенд получает её
готовым числом в env, генератор формул — списком слагаемых (col, coeff).

Валидация реестра (парс выражений, toposort, уникальность колонок и
согласованность occurrence дублей подписей) выполняется при импорте модуля —
битый реестр валит старт приложения сразу.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

CABINET_SPEND = "cabinet_spend"  # псевдопеременная, подставляется извне
# Σ значений ручных полей с target="prihod" (доходные, например «Крот»).
# Пусто/нет полей → слагаемое исчезает из A1-формулы и = 0
# в расчёте — формулы брендов без таких полей не меняются.
MANUAL_INCOME = "manual_income"
_PSEUDO_VARS = {CABINET_SPEND, MANUAL_INCOME}

# Метрики, которые можно отключить per-brand (`google_sheets.disabled_metrics`).
# Отключённая метрика: не читается из листа, слагаемое с ней исчезает из
# A1-формул (элизия), зависимые формулы с ней в множителе/делителе не пишутся,
# в бэкенд-расчёте участвует как 0. По умолчанию все включены.
OPTIONAL_METRICS = {"dolety"}


@dataclass(frozen=True)
class Metric:
    key: str
    col: Optional[str]            # каноническая буква layout; None = скрытая
    label: Optional[str]          # подпись строки 2; None у скрытых
    kind: str                     # "date" | "base_service" | "base_manual" | "computed"
    occurrence: int = 1           # вхождение подписи слева направо (дубли)
    source: Optional[str] = None  # base_service: путь в отчёте / "metrika.goal"
    expr: Optional[str] = None    # computed: мини-DSL
    literal: Any = None           # base_service с фиксированным значением
    iferror: bool = False         # обернуть A1-формулу в IFERROR (сейчас нигде)
    description: str = ""


def _m(*args, **kwargs) -> Tuple[str, Metric]:
    m = Metric(*args, **kwargs)
    return m.key, m


# Порядок вставки = канонический layout листа (A..AM), скрытые — в конце.
METRICS: Dict[str, Metric] = dict([
    _m("date", "A", "Дата", "date",
       description="Дата строки, dd.mm.yyyy"),
    _m("chistaya", "B", "Чистая", "computed", expr="prihod - zatraty",
       description="Чистая прибыль: Приход − Затраты"),
    _m("prihod", "C", "Приход", "computed",
       expr="dohod_vitrina + manual_income",
       description="Приход: Доход с витрины + Σ ручных доходных полей (target=prihod)"),
    _m("zatraty", "D", "Затраты", "computed",
       expr="sms_cost + cabinet_spend",
       description="Затраты: Расход СМС + Σ(расход кабинета × коэффициент)"),
    _m("clicks_lt", "E", "Клики лт", "base_service", source="leadstech.clicks",
       description="LeadsTech: уники (uniques) по sub1, сумма по всем аккаунтам"),
    _m("metrika_v", "F", "Метрика визиты", "base_service", source="yandex_metrika.visits",
       description="Яндекс.Метрика: визиты счётчика за день"),
    _m("zayavki", "G", "Заявки с сайта", "base_service", source="metrika.goal",
       description="Яндекс.Метрика: число цели «Zayvka» (или первой цели бренда)"),
    _m("obshchee", "H", "Общее", "computed", expr="metrika_v + zayavki",
       description="Общее: визиты Метрики + заявки"),
    _m("cv_sayta", "I", "CV сайта", "computed", expr="obshchee * 100 / clicks_lt",
       description="CV сайта, %: Общее × 100 / Клики лт"),
    _m("cv_klik", "J", "CV клик", "computed", expr="klik * 100 / clicks_lt",
       description="CV клика, %: клик × 100 / Клики лт"),
    _m("klik", "K", "клик", "computed", expr="perehody",
       description="клик = Переходы Уники (алиас)"),
    _m("pokupka", "L", "Покупка", "computed", expr="zatraty / klik",
       description="Покупка: Затраты / клик"),
    _m("prodazha", "M", "Продажа", "computed", expr="vitrina / klik",
       description="Продажа: Витрина / клик"),
    _m("bekender", "N", "бекендер", "computed", expr="dolety / obshchee",
       description="бекендер: Долеты / Общее"),
    _m("sms_share", "O", "SMS", "computed", expr="sms_chistye / obshchee",
       description="SMS на заявку: SMS чистые / Общее"),
    _m("api_share", "P", "API", "computed", expr="api_dohod / obshchee",
       occurrence=1,
       description="API на заявку: API / Общее"),
    _m("dohod_na_zayavku", "Q", "Доход на заявку", "computed", expr="vitrina / obshchee",
       description="Доход на заявку: Витрина / Общее"),
    _m("dolety", "R", "Долеты и Крот", "base_manual",
       description="«Долеты и Крот» — вводится руками в таблице; только операнд «бекендера», в «Приход» не входит"),
    _m("sms_chistye", "S", "SMS чистые", "computed", expr="sms_charge - sms_cost",
       description="SMS чистые: Приход СМС − Расход СМС"),
    _m("prihody_sms", "T", "приходы смс", "computed", expr="sms_charge",
       description="приходы смс = Приход СМС (алиас)"),
    _m("api_dohod", "U", "API", "computed", expr="vsego", occurrence=2,
       description="API: Всего (=AH)"),
    _m("vitrina", "V", "Витрина", "computed", expr="itogo",
       description="Витрина = Итого витрина+СМС+АПИ (алиас)"),
    _m("adfox", "W", "Adfox", "computed", expr="itogo",
       description="Adfox = Итого (в эталоне ссылалось на AL через мусорную AN)"),
    _m("epc", "X", "EPC", "computed", expr="prihod / clicks_lt",
       description="EPC: Приход / Клики лт"),
    _m("pokupka_s_lida", "Y", "Покупка с лида", "computed", expr="zatraty / obshchee",
       description="Покупка с лида: Затраты / Общее"),
    _m("prodazha_s_lida", "Z", "Продажа с лида", "computed", expr="prihod / obshchee",
       description="Продажа с лида: Приход / Общее"),
    _m("roi", "AA", "ROI", "computed", expr="chistaya / zatraty",
       description="ROI: Чистая / Затраты"),
    _m("sms_count", "AB", "кол-во смсок", "base_service", source="eightconnect.count",
       description="8connect: количество отправленных SMS"),
    _m("sms_cost", "AC", "Расход", "base_service", source="eightconnect.cost",
       description="8connect: расход на рассылку"),
    _m("chistye", "AD", "Чистые", "computed", expr="sms_charge - sms_cost",
       description="Чистые СМС: Приход СМС − Расход СМС (дубль S; AG ссылается сюда)"),
    _m("sms_charge", "AE", "Приход", "base_service", source="eightconnect.charge",
       occurrence=2,
       description="8connect: доход с рассылки"),
    _m("sms_clients", "AF", "Клиенты", "base_service", literal=0,
       description="Клиенты: всегда 0 (по требованию); в «Приход» не входит"),
    _m("roi_sms", "AG", "ROI SMS", "computed", expr="chistye / sms_cost",
       description="ROI SMS: Чистые / Расход СМС"),
    _m("vsego", "AH", "Всего", "computed", expr="sms_clients * 1",
       description="Всего: Клиенты × 1"),
    _m("perehody", "AI", "Переходы Уники", "base_service", source="leadstech.hosts",
       description="LeadsTech: уникальные хосты по sub1"),
    _m("dohod_vitrina", "AJ", "Доход с витрины", "computed",
       expr="lt_sumwebmaster - sms_charge",
       description="Доход с витрины: доход вебмастера LeadsTech − Приход СМС"),
    _m("dohod_s_unika", "AK", "Доход с одного уника", "computed", expr="itogo / perehody",
       description="Доход с одного уника: Итого / Переходы Уники"),
    _m("itogo", "AL", "Итого витрина + СМС + АПИ", "computed",
       expr="sms_charge + dohod_vitrina + api_dohod",
       description="Итого: Приход СМС + Доход с витрины + API"),
    _m("marzha_s_klika", "AM", "Маржа с клика", "computed",
       expr="(chistaya - sms_cost) / klik",
       description="Маржа с клика: (Чистая − Расход СМС) / клик"),
    # Скрытая базовая: не имеет колонки, в A1-формулах инлайнится литералом
    # (как сейчас `=681266,48-AE13` в живой таблице).
    _m("lt_sumwebmaster", None, None, "base_service", source="leadstech.sum",
       description="LeadsTech: доход вебмастера (sumwebmaster) — операнд «Дохода с витрины»"),
])


# ============================== парсер DSL ==============================

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)


def _parse_expr(key: str, expr: str) -> ast.expr:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"metrics[{key}]: битое выражение {expr!r}: {e}") from e
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.Constant, ast.Name, ast.Load)):
            if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
                raise ValueError(f"metrics[{key}]: недопустимая константа {node.value!r}")
            continue
        if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
            continue
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            continue
        if isinstance(node, _ALLOWED_BINOPS + (ast.USub,)):
            continue
        raise ValueError(
            f"metrics[{key}]: недопустимый узел {type(node).__name__} в {expr!r}"
        )
    return tree.body


def _expr_names(node: ast.expr) -> List[str]:
    return [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]


_PARSED: Dict[str, ast.expr] = {}


def _validate_registry() -> List[str]:
    """Парс выражений, проверка ссылок/колонок/дублей, топологический порядок.

    Возвращает порядок вычисления computed-метрик. Ошибка реестра валит импорт.
    """
    known = set(METRICS) | _PSEUDO_VARS
    cols_seen: Dict[str, str] = {}
    for key, m in METRICS.items():
        if m.col is not None:
            if m.col in cols_seen:
                raise ValueError(f"metrics: колонка {m.col} занята и {cols_seen[m.col]}, и {key}")
            cols_seen[m.col] = key
        if m.kind == "computed":
            if not m.expr:
                raise ValueError(f"metrics[{key}]: computed без expr")
            _PARSED[key] = _parse_expr(key, m.expr)
            for name in _expr_names(_PARSED[key]):
                if name not in known:
                    raise ValueError(f"metrics[{key}]: неизвестное имя {name!r} в {m.expr!r}")

    # occurrence: подпись должна получать вхождения 1..N слева направо
    by_label: Dict[str, List[Tuple[int, int]]] = {}
    for idx, m in enumerate(METRICS.values()):
        if m.label:
            by_label.setdefault(m.label, []).append((idx, m.occurrence))
    for label, entries in by_label.items():
        expected = list(range(1, len(entries) + 1))
        got = [occ for _, occ in sorted(entries)]
        if got != expected:
            raise ValueError(f"metrics: подпись {label!r} — вхождения {got}, ожидалось {expected}")

    # toposort computed-метрик (DFS, детект циклов)
    order: List[str] = []
    state: Dict[str, int] = {}  # 1 = в обработке, 2 = готово

    def visit(key: str) -> None:
        if key in _PSEUDO_VARS or METRICS[key].kind != "computed":
            return
        if state.get(key) == 2:
            return
        if state.get(key) == 1:
            raise ValueError(f"metrics: цикл через {key!r}")
        state[key] = 1
        for dep in _expr_names(_PARSED[key]):
            visit(dep)
        state[key] = 2
        order.append(key)

    for key, m in METRICS.items():
        if m.kind == "computed":
            visit(key)
    return order


COMPUTE_ORDER: List[str] = _validate_registry()


# ============================== вычислитель ==============================

def _div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b


def eval_expr(node: ast.expr, env: Dict[str, Optional[float]]) -> Optional[float]:
    """Вычисляет AST над env. Операнд None → None; деление на 0 → None
    (в таблице в этой ситуации #DIV/0!)."""
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.UnaryOp):
        v = eval_expr(node.operand, env)
        return None if v is None else -v
    if isinstance(node, ast.BinOp):
        left = eval_expr(node.left, env)
        right = eval_expr(node.right, env)
        if isinstance(node.op, ast.Div):
            return _div(left, right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
    raise ValueError(f"eval_expr: неожиданный узел {type(node).__name__}")


def _get_path(report: Dict[str, Any], path: str) -> Optional[float]:
    cur: Any = report
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    try:
        return float(cur) if cur is not None else None
    except (TypeError, ValueError):
        return None


def base_env(
    report: Dict[str, Any],
    goal_value: Optional[float],
    manual_values: Optional[Dict[str, Optional[float]]] = None,
) -> Dict[str, Optional[float]]:
    """Значения базовых метрик из отчёта build_report.

    `goal_value` — число цели Метрики (логика выбора цели живёт в
    sheets_writer._find_goal_value, сюда приходит готовое число).
    `manual_values` — ручные метрики, прочитанные из листа ({key: значение}).
    """
    env: Dict[str, Optional[float]] = {}
    manual_values = manual_values or {}
    for key, m in METRICS.items():
        if m.kind == "base_service":
            if m.literal is not None:
                env[key] = float(m.literal)
            elif m.source == "metrika.goal":
                env[key] = goal_value
            else:
                env[key] = _get_path(report, m.source or "")
        elif m.kind == "base_manual":
            env[key] = manual_values.get(key)
    return env


def compute_metrics(
    report: Dict[str, Any],
    goal_value: Optional[float],
    cabinet_spend: Optional[float],
    manual_values: Optional[Dict[str, Optional[float]]] = None,
    manual_income: Optional[float] = None,
    disabled: Optional[set] = None,
) -> Tuple[Dict[str, Optional[float]], Dict[str, Any]]:
    """Вычисляет все метрики. Возвращает (values, meta).

    Ручные метрики без значения участвуют в суммах как 0 (и попадают в
    meta["assumed_zero"]), но в values остаются None — прозрачно видно,
    что данных не было.
    """
    env = base_env(report, goal_value, manual_values)
    env[CABINET_SPEND] = cabinet_spend
    env[MANUAL_INCOME] = float(manual_income or 0.0)
    disabled = disabled or set()
    for k in disabled:
        env[k] = 0.0  # отключённая метрика участвует как 0, без warning'а

    assumed_zero = [k for k, v in env.items()
                    if v is None and METRICS.get(k) and METRICS[k].kind == "base_manual"
                    and k not in disabled]
    calc_env = dict(env)
    for k in assumed_zero:
        calc_env[k] = 0.0
    if calc_env.get(CABINET_SPEND) is None:
        calc_env[CABINET_SPEND] = 0.0
        assumed_zero.append(CABINET_SPEND)

    for key in COMPUTE_ORDER:
        calc_env[key] = eval_expr(_PARSED[key], calc_env)

    values: Dict[str, Optional[float]] = {}
    for key, m in METRICS.items():
        if m.kind == "date":
            continue
        v = env.get(key) if m.kind == "base_manual" else calc_env.get(key)
        values[key] = round(v, 4) if isinstance(v, float) else v
    meta = {"assumed_zero": assumed_zero}
    return values, meta


# ============================== A1-генератор ==============================

def _fmt_number(v: float) -> str:
    """Число для формулы: русская локаль (десятичная запятая, USER_ENTERED).

    Денежные литералы (sumwebmaster) — как legacy `_build_dohod_vitrina_formula`:
    ровно 2 знака; целые — без дробной части.
    """
    s = f"{float(v):g}" if float(v).is_integer() else f"{v:.2f}"
    return s.replace(".", ",")


def _fmt_coeff(v: float) -> str:
    """Коэффициент кабинета: без обрезания знаков (0.954 → «0,954»),
    как legacy `_build_zatraty_formula`."""
    return f"{float(v):g}".replace(".", ",")


@dataclass
class A1Context:
    colmap: Dict[str, str]                      # key -> буква колонки (реальный layout)
    row: int
    literals: Dict[str, float]                  # скрытые метрики -> инлайн-значение
    cabinet_terms: List[Tuple[str, float]]      # [(буква, коэффициент)] для cabinet_spend
    income_terms: List[str] = None              # буквы доходных ручных полей (manual_income)
    disabled: Optional[set] = None              # отключённые метрики (слагаемые выпадают)


_PRECEDENCE = {ast.Add: 1, ast.Sub: 1, ast.Mult: 2, ast.Div: 2}


def _to_a1(node: ast.expr, ctx: A1Context, parent_prec: int = 0) -> str:
    if isinstance(node, ast.Constant):
        return _fmt_number(float(node.value))
    if isinstance(node, ast.Name):
        if ctx.disabled and node.id in ctx.disabled:
            return ""  # отключённая метрика: слагаемое исчезает (элизия в BinOp)
        if node.id == MANUAL_INCOME:
            terms = [f"{c}{ctx.row}" for c in (ctx.income_terms or [])]
            if not terms:
                return ""  # слагаемое исчезает (см. элизию в BinOp)
            inner = "+".join(terms)
            return f"({inner})" if parent_prec > 1 and len(terms) != 1 else inner
        if node.id == CABINET_SPEND:
            parts = []
            for col, k in ctx.cabinet_terms:
                ref = f"{col}{ctx.row}"
                parts.append(ref if k == 1 else f"{ref}*{_fmt_coeff(k)}")
            inner = "+".join(parts) if parts else "0"
            # сумма — приоритет сложения: скобки, если родитель сильнее
            return f"({inner})" if parent_prec > 1 and len(parts) != 1 else inner
        if node.id in ctx.colmap:
            return f"{ctx.colmap[node.id]}{ctx.row}"
        if node.id in ctx.literals:
            return _fmt_number(ctx.literals[node.id])
        raise ValueError(f"expr_to_a1: {node.id!r} нет ни в colmap, ни в literals")
    if isinstance(node, ast.UnaryOp):
        inner = _to_a1(node.operand, ctx, 3)
        return "" if inner == "" else f"-{inner}"
    if isinstance(node, ast.BinOp):
        prec = _PRECEDENCE[type(node.op)]
        op = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}[type(node.op)]
        left = _to_a1(node.left, ctx, prec)
        # правый операнд при - и / требует скобок на равном приоритете
        right = _to_a1(node.right, ctx, prec + (1 if isinstance(node.op, (ast.Sub, ast.Div)) else 0))
        # элизия пустых псевдо-слагаемых (manual_income без полей): A+"" -> A
        if right == "" and isinstance(node.op, (ast.Add, ast.Sub)):
            return left
        if left == "" and isinstance(node.op, ast.Add):
            return right
        # пустой операнд в -a, a*b, a/b — формула не имеет смысла целиком
        if left == "" or right == "":
            return ""
        s = f"{left}{op}{right}"
        return f"({s})" if prec < parent_prec else s
    raise ValueError(f"expr_to_a1: неожиданный узел {type(node).__name__}")


def expr_to_a1(key: str, ctx: A1Context) -> Optional[str]:
    """A1-формула computed-метрики для строки ctx.row (с ведущим `=`).

    None — формула выродилась (все значимые операнды отключены)."""
    m = METRICS[key]
    if m.kind != "computed":
        raise ValueError(f"expr_to_a1: {key} не computed")
    body = _to_a1(_PARSED[key], ctx)
    if body == "":
        return None
    if m.iferror:
        body = f"IFERROR({body};)"
    return "=" + body


def _to_human(node: ast.expr, names: Dict[str, str], parent_prec: int = 0) -> str:
    if isinstance(node, ast.Constant):
        return f"{float(node.value):g}"
    if isinstance(node, ast.Name):
        return names.get(node.id, node.id)
    if isinstance(node, ast.UnaryOp):
        return f"−{_to_human(node.operand, names, 3)}"
    if isinstance(node, ast.BinOp):
        prec = _PRECEDENCE[type(node.op)]
        op = {ast.Add: " + ", ast.Sub: " − ", ast.Mult: " × ", ast.Div: " / "}[type(node.op)]
        left = _to_human(node.left, names, prec)
        right = _to_human(node.right, names,
                          prec + (1 if isinstance(node.op, (ast.Sub, ast.Div)) else 0))
        s = f"{left}{op}{right}"
        return f"({s})" if prec < parent_prec else s
    raise ValueError(type(node).__name__)


def default_system_names() -> Dict[str, str]:
    """Дефолтные системные имена метрик (для UI и рендера формул)."""
    names = {k: (mm.label or k) for k, mm in METRICS.items()}
    names["lt_sumwebmaster"] = "Доход вебмастера ЛТ"
    names["sms_charge"] = "Приход СМС"
    names["sms_cost"] = "Расход СМС"
    return names


def human_formula(key: str, names_override: Optional[Dict[str, str]] = None) -> Optional[str]:
    """«Чистая = Приход − Затраты» — формула в системных именах, для UI.

    `names_override` — per-brand имена (`google_sheets.metric_names`),
    накладываются поверх дефолтов.
    """
    m = METRICS[key]
    if m.kind != "computed":
        return None
    names = default_system_names()
    for k, v in (names_override or {}).items():
        if str(v).strip():
            names[k] = str(v).strip()
    names[CABINET_SPEND] = "Σ(кабинет × коэф)"
    names[MANUAL_INCOME] = "Σ(ручные доходные поля)"
    return f"{names.get(key, key)} = {_to_human(_PARSED[key], names)}"


# ============================== итоговая строка ==============================
# Оцифровка строки 34 эталона («Июль 26» LASDSDS): итог после последней даты
# месяца. Виды: "sum" — SUM по колонке за месяц; "expr" — та же формула
# метрики, применённая к итоговой строке (коэффициенты пересчитываются от
# сумм, а не суммируются); "avg_pos" — среднее по положительным значениям.
# Отличия от эталона (осознанные): metrika_v суммируем (в эталоне F34 забыт),
# кабинетные колонки суммируются ВСЕ (в эталоне — часть), мусорные AN..AQ нет.
TOTALS: Dict[str, str] = {
    "chistaya": "sum", "prihod": "sum", "zatraty": "sum", "clicks_lt": "sum",
    "metrika_v": "sum", "zayavki": "sum", "obshchee": "sum", "klik": "sum",
    "dolety": "sum", "sms_chistye": "sum", "prihody_sms": "sum",
    "api_dohod": "sum", "vitrina": "sum", "adfox": "sum", "sms_count": "sum",
    "sms_cost": "sum", "chistye": "sum", "sms_charge": "sum",
    "sms_clients": "sum", "vsego": "sum", "perehody": "sum",
    "dohod_vitrina": "sum", "itogo": "sum",
    "cv_sayta": "expr", "cv_klik": "expr", "pokupka": "expr",
    "prodazha": "expr", "bekender": "expr", "sms_share": "expr",
    "api_share": "expr", "dohod_na_zayavku": "expr",
    "pokupka_s_lida": "expr", "prodazha_s_lida": "expr", "roi": "expr",
    "dohod_s_unika": "expr",
    "epc": "avg_pos", "roi_sms": "avg_pos", "marzha_s_klika": "avg_pos",
}
for _k, _kind in TOTALS.items():
    if _k not in METRICS:
        raise ValueError(f"TOTALS: неизвестная метрика {_k!r}")
    if _kind == "expr" and METRICS[_k].kind != "computed":
        raise ValueError(f"TOTALS: expr-итог у не-computed метрики {_k!r}")


def total_formula(key: str, ctx: A1Context, first_row: int, last_row: int) -> Optional[str]:
    """A1-формула итоговой строки для метрики (ctx.row = номер итоговой строки).

    Возвращает None, если у метрики нет итога или её колонка не разрешена.
    """
    kind = TOTALS.get(key)
    if not kind:
        return None
    if ctx.disabled and key in ctx.disabled:
        return None
    col = ctx.colmap.get(key)
    if col is None:
        return None
    if kind == "sum":
        return f"=SUM({col}{first_row}:{col}{last_row})"
    if kind == "avg_pos":
        # ; — разделитель аргументов в русской локали таблиц
        return f'=AVERAGEIF({col}{first_row}:{col}{last_row};">0")'
    if kind == "expr":
        return expr_to_a1(key, ctx)
    return None


def computed_keys() -> List[str]:
    return [k for k, m in METRICS.items() if m.kind == "computed"]


def manual_keys() -> List[str]:
    return [k for k, m in METRICS.items() if m.kind == "base_manual"]


def layout_columns() -> List[Tuple[str, Metric]]:
    """[(буква, метрика)] канонического layout, по порядку колонок."""
    from sheets_writer import _col_index  # локальный импорт: избегаем цикла
    cols = [(m.col, m) for m in METRICS.values() if m.col is not None]
    return sorted(cols, key=lambda cm: _col_index(cm[0]))


def resolve_registry_columns(
    label_row: List[Any],
    labels_override: Dict[str, str],
    cabinet_start_idx: Optional[int] = None,
) -> Tuple[Dict[str, str], List[str]]:
    """Колонка для каждой метрики реестра по подписи строки 2 (зона левее AR).

    Та же логика, что sheets_writer._resolve_agg_columns, но на полном
    реестре (~38 колонок вместо 12) и без фолбэка на легаси-буквы: промах —
    просто warning (используется для чтения ручных значений и сверки).
    """
    from sheets_writer import CABINET_START_COL, _col_index, _col_letter, _norm

    cabinet_start = cabinet_start_idx or _col_index(CABINET_START_COL)
    by_label: Dict[str, List[str]] = {}
    for idx0, cell in enumerate(label_row):
        if idx0 + 1 >= cabinet_start:
            break
        norm = _norm(str(cell))
        if norm:
            by_label.setdefault(norm, []).append(_col_letter(idx0 + 1))

    cols: Dict[str, str] = {}
    warnings: List[str] = []
    for key, m in METRICS.items():
        if m.label is None:
            continue
        label, occurrence = m.label, m.occurrence
        override = (labels_override or {}).get(key)
        if override:
            label, occurrence = override, 1
        hits = by_label.get(_norm(label), [])
        if len(hits) >= occurrence:
            cols[key] = hits[occurrence - 1]
        else:
            warnings.append(f"{key}: подпись {label!r} (вх. {occurrence}) не найдена")
    return cols, warnings
