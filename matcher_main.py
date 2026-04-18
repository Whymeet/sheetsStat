import json
import logging
import os
from datetime import date, timedelta, datetime
from typing import Any, Dict, List, Tuple, Optional

from config_loader import load_lt_vk_config
from leadstech_client import build_leadstech_client
from lt_aggregation import aggregate_leadstech_by_banner
from vk_ads_client import VkAdsClient, VkAdsConfig


logger = logging.getLogger("lt_vk_matcher")


def setup_simple_logging():
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    os.makedirs("logs", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join("logs", f"lt_vk_matcher_{ts}.log")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.info("Логи пишутся в %s", log_path)


def compute_lookback_period(config: Dict[str, Any]) -> Tuple[date, date]:
    analysis_cfg = config.get("analysis", {})
    lookback_days = int(analysis_cfg.get("lookback_days", 7))
    today = date.today()
    date_to = today
    date_from = today - timedelta(days=lookback_days)

    logger.info(
        "Период анализа: %s .. %s (%d дней)",
        date_from.isoformat(),
        date_to.isoformat(),
        lookback_days,
    )
    return date_from, date_to


def build_vk_client_for_cabinet(
    config: Dict[str, Any],
    cab_cfg: Dict[str, Any],
) -> VkAdsClient:
    vk_common = config.get("vk_ads", {})
    vk_cfg = VkAdsConfig(
        base_url=vk_common["base_url"],
        api_token=cab_cfg["api_vk_ads"],
    )
    return VkAdsClient(vk_cfg)


def merge_leadstech_and_vk_for_cabinet(
    cabinet_name: str,
    leadstech_label: str,
    lt_by_banner: Dict[int, Dict[str, Any]],
    vk_spent_by_banner: Dict[int, float],
    date_from: date,
    date_to: date,
) -> List[Dict[str, Any]]:
    """
    Склеиваем данные по одному кабинету:
    Leadstech (доход) + VK Ads (траты) + расчёт profit и ROI.
    """
    results: List[Dict[str, Any]] = []

    for banner_id, lt_data in lt_by_banner.items():
        lt_revenue = float(lt_data.get("lt_revenue", 0.0))
        vk_spent = float(vk_spent_by_banner.get(banner_id, 0.0))

        profit = lt_revenue - vk_spent
        roi_percent: Optional[float] = None
        if vk_spent > 0:
            roi_percent = profit / vk_spent * 100.0

        row: Dict[str, Any] = {
            "cabinet_name": cabinet_name,
            "leadstechLabel": leadstech_label,
            "banner_id": banner_id,
            "vk_spent": round(vk_spent, 2),
            "lt_revenue": round(lt_revenue, 2),
            "lt_clicks": int(lt_data.get("lt_clicks", 0)),
            "lt_conversions": int(lt_data.get("lt_conversions", 0)),
            "lt_approved": int(lt_data.get("lt_approved", 0)),
            "lt_inprogress": int(lt_data.get("lt_inprogress", 0)),
            "lt_rejected": int(lt_data.get("lt_rejected", 0)),
            "profit": round(profit, 2),
            "roi_percent": round(roi_percent, 2) if roi_percent is not None else None,
            "period": {
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
            },
        }

        results.append(row)

    logger.info(
        "Кабинет %s: финальный мердж по %d объявлениям",
        cabinet_name,
        len(results),
    )
    return results


def normalize_cabinet_name(name: str) -> str:
    """
    Делает имя кабинета безопасным для файла.
    Сейчас по требованиям — просто заменяем пробелы на "_".
    """
    return name.replace(" ", "_")


def save_cabinet_results_to_json(
    cabinet_name: str,
    data: List[Dict[str, Any]],
    config: Dict[str, Any],
    date_from: date,
    date_to: date,
) -> Optional[str]:
    """
    Сохраняет статистику ОТДЕЛЬНО для одного кабинета в файл вида:
      Матвей_zk1_16-23-11-2025.json

    Формат имени:
      <cabinet_slug>_<DD_FROM>-<DD_TO>-<MM>-<YYYY>.json
    """
    if not data:
        logger.warning("Кабинет %s: нет данных для сохранения", cabinet_name)
        return None

    output_cfg = config.get("output", {})
    out_dir = output_cfg.get("dir", "data")
    os.makedirs(out_dir, exist_ok=True)

    cab_slug = normalize_cabinet_name(cabinet_name)

    day_from = date_from.day
    day_to = date_to.day
    month = date_to.month
    year = date_to.year

    filename = f"{cab_slug}_{day_from:02d}-{day_to:02d}-{month:02d}-{year}.json"
    path = os.path.join(out_dir, filename)

    payload = {
        "generated_at": datetime.now().isoformat(),
        "cabinet_name": cabinet_name,
        "period": {
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
        },
        "count": len(data),
        "items": data,
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info("Кабинет %s: результаты сохранены в %s", cabinet_name, path)
    return path


def build_gsheets_spreadsheet(config: Dict[str, Any]) -> Optional[Any]:
    """Создаёт объект Google Sheets Spreadsheet по конфигу.

    Ожидается блок в lt_vk_config.json:

      "google_sheets": {
        "enabled": true,
        "service_account_json_path": "cfg/service_account.json",
        "spreadsheet_id": "1AbCdEfG..."
      }

    Если блок отключён или не настроен, возвращает None.
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

    try:
        import time
        max_retries = 3
        retry_delay = 2

        for attempt in range(max_retries):
            try:
                logger.info(f"Google Sheets: попытка подключения {attempt + 1}/{max_retries}...")
                gc = gspread.service_account(filename=service_account_path)
                spreadsheet = gc.open_by_key(spreadsheet_id)
                break
            except Exception as retry_exc:
                if attempt < max_retries - 1:
                    logger.warning(f"Google Sheets: ошибка при подключении (попытка {attempt + 1}): {retry_exc}")
                    logger.info(f"Повторная попытка через {retry_delay} секунд...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # экспоненциальная задержка
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


def write_cabinet_results_to_sheet(
    spreadsheet: Any,
    cabinet_name: str,
    rows: List[Dict[str, Any]],
) -> None:
    """Создаёт/очищает вкладку по имени кабинета и записывает туда данные.

    Колонки:
    ID объявления | Траты Vk | Доход Лидстех | ROI

    Логика сортировки:
    - сначала строки, где ROI есть и это число, по убыванию;
    - потом строки с пустым/нечисловым ROI (они оказываются внизу).
    """
    if spreadsheet is None:
        return

    try:
        import gspread  # type: ignore
    except ImportError:
        logger.error("Google Sheets: библиотека gspread не установлена, пропускаем запись")
        return

    sheet_title = cabinet_name

    # Ищем/создаём вкладку
    try:
        ws = spreadsheet.worksheet(sheet_title)
        logger.info("Google Sheets: вкладка '%s' найдена, очищаем", sheet_title)
        ws.clear()
    except gspread.WorksheetNotFound:
        logger.info("Google Sheets: вкладка '%s' не найдена, создаём", sheet_title)
        ws = spreadsheet.add_worksheet(title=sheet_title, rows=1000, cols=10)

    if not rows:
        logger.warning(
            "Google Sheets: кабинет %s — нет строк, на вкладке только очистка",
            cabinet_name,
        )
        return

    # --- 1. Разбиваем строки: с ROI и без ROI ---
    rows_with_roi: List[Dict[str, Any]] = []
    rows_without_roi: List[Dict[str, Any]] = []

    for row in rows:
        roi = row.get("roi_percent")
        if isinstance(roi, (int, float)):
            rows_with_roi.append(row)
        else:
            rows_without_roi.append(row)

    # --- 2. Сортируем строки с числовым ROI по убыванию ---
    rows_with_roi.sort(
        key=lambda r: r.get("roi_percent", 0.0),
        reverse=True,
    )

    # --- 3. Сначала с ROI, потом без ROI ---
    ordered_rows = rows_with_roi + rows_without_roi

    # --- 4. Готовим данные для записи в таблицу ---
    headers = [
        "ID объявления",   # banner_id
        "Траты Vk",        # vk_spent
        "Доход Лидстех",   # lt_revenue
        "ROI",             # roi_percent
    ]

    data: List[List[Any]] = [headers]

    for row in ordered_rows:
        data.append(
            [
                row.get("banner_id", ""),      # ID объявления
                row.get("vk_spent", ""),       # Траты Vk
                row.get("lt_revenue", ""),     # Доход Лидстех
                row.get("roi_percent", ""),    # ROI (число или пусто)
            ]
        )

    # --- 5. Записываем в Google Sheets ---
    # Чтоб не было DeprecationWarning — используем именованные аргументы
    ws.update(range_name="A1", values=data)

    logger.info(
        "Google Sheets: кабинет %s — записано %d строк (отсортировано по ROI в Python)",
        cabinet_name,
        len(ordered_rows),
    )


def main():
    setup_simple_logging()
    logger.info("=== Leadstech ↔ VK Ads matcher стартует ===")

    config = load_lt_vk_config()
    date_from, date_to = compute_lookback_period(config)

    # Пытаемся открыть Google Sheets (если настроено в конфиге).
    gs_spreadsheet = build_gsheets_spreadsheet(config)

    cabinets = config.get("cabinets", [])
    if not cabinets:
        logger.error("В конфиге нет cabinets")
        return

    # один общий клиент для Leadstech (логин/пароль — один раз)
    lt_client = build_leadstech_client(config)

    # Получаем список полей для поиска banner_id
    banner_sub_fields = lt_client.cfg.banner_sub_fields

    for cab in cabinets:
        name = cab["name"]
        lt_label = cab["leadstechLabel"]

        logger.info(
            "--- Кабинет: %s (leadstechLabel=%s, banner_sub_fields=%s) ---",
            name,
            lt_label,
            banner_sub_fields,
        )

        # 1. Leadstech: тянем доходы по сабам для конкретного leadstechLabel
        lt_rows = lt_client.get_stat_by_subid(
            date_from=date_from,
            date_to=date_to,
            sub1_value=lt_label,
            subs_fields=banner_sub_fields,
        )

        lt_by_banner = aggregate_leadstech_by_banner(
            lt_rows,
            banner_sub_fields=banner_sub_fields,
        )

        if not lt_by_banner:
            logger.warning(
                "Кабинет %s: нет данных Leadstech с %s — пропускаем",
                name,
                banner_sub_fields,
            )
            continue

        banner_ids = sorted(lt_by_banner.keys())
        logger.info(
            "Кабинет %s: объявлений из Leadstech = %d",
            name,
            len(banner_ids),
        )

        # 2. VK Ads: тянем траты по тем же объявлениям
        vk_client = build_vk_client_for_cabinet(config, cab)
        vk_spent_by_banner = vk_client.get_spent_by_banner(
            date_from,
            date_to,
            banner_ids,
        )

        # 3. Мердж: доход + траты + profit/ROI
        cabinet_results = merge_leadstech_and_vk_for_cabinet(
            cabinet_name=name,
            leadstech_label=lt_label,
            lt_by_banner=lt_by_banner,
            vk_spent_by_banner=vk_spent_by_banner,
            date_from=date_from,
            date_to=date_to,
        )

        # 4. Сохранение ОТДЕЛЬНОГО файла для кабинета
        save_cabinet_results_to_json(
            cabinet_name=name,
            data=cabinet_results,
            config=config,
            date_from=date_from,
            date_to=date_to,
        )

        # 5. Запись/обновление вкладки в Google Sheets
        write_cabinet_results_to_sheet(
            spreadsheet=gs_spreadsheet,
            cabinet_name=name,
            rows=cabinet_results,
        )

    logger.info("=== Готово ===")


if __name__ == "__main__":
    main()
