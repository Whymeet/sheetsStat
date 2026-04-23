# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Запуск и разработка

Локально (venv уже в репо):
```bash
source venv/bin/activate
uvicorn app:app --reload --port 8000           # веб-морда + API на http://localhost:8000
python3 daily_report.py --date 2026-04-17 --sub1 kub   # CLI-режим того же отчёта
```

В Docker (конфиг/output/logs монтируются с хоста, внешний порт 8001):
```bash
docker compose up -d --build
docker compose logs -f web
```

Healthcheck: `GET /api/health` → `{"ok": true}`.

Тестов и линтеров в проекте нет — проверка работы только через ручной запуск отчёта и сравнение с Google Sheets.

## Архитектура

### Единая точка сборки отчёта

`core.build_report(config, day, sub1)` — единственная функция, строящая дневной отчёт. Её дёргают три точки входа:
- `app.py` (FastAPI, `POST /api/report`) — веб-морда
- `daily_report.py` — CLI-обёртка
- `scheduler.ReportScheduler` — автозапуск каждый день в настраиваемое время по Европа/Самара за предыдущий день

`build_report` последовательно собирает пять внешних источников и опционально пишет в Google Sheets:

| Источник | Клиент | Что берём |
|---|---|---|
| VK Ads (через Ads Manager vktest2) | `ads_manager_client.py` | `GET /api/telegram/daily-stats?date=&label=<sub1>` — spent по кабинетам пользователя |
| Yandex Direct (через свой Ads Manager) | `yandex_client.py` | то же, но Яндекс |
| Яндекс.Метрика | `yandex_metrika_client.py` | `visits`/`pageviews`/`users` счётчика + достижения по списку целей |
| LeadsTech | `leadstech_client.py` | `data.summary` из `/v1/front/stat/by-subid` по `sub1` |
| 8connect | `eightconnect_client.py` | `/api/report/list` → суммы cost/charge по `scheme_ids` |

Важный архитектурный принцип: **sheetsStat НЕ хранит VK/Yandex токены и список кабинетов**. Для VK/Yandex ходим в отдельные Ads Manager-ы (`kybyshka-dev.ru`, `yamanager.kybyshka-dev.ru`) по логину/паролю пользователя — JWT получаем через `POST /api/auth/login`, кабинеты привязаны к пользователю на стороне Ads Manager. Истёкший токен автоматически перевыпускается в `_get` (401 → relogin → retry).

Общая JWT-механика (login, _get с авто-relogin) живёт в `http_base.JWTAuthClient`; `AdsManagerClient` и `YandexAdsManagerClient` — тонкие наследники, добавляющие свой `get_daily_stats` (VK пробрасывает `label`, Yandex — нет). Одна общая функция `core._collect_ads_stats` обходит `accounts[]` и превращает ответ в унифицированный `{cabinets, total, errors}`.

### Конфиг

Единый JSON — `cfg/lt_vk_config.json`. Редактируется через UI (`POST /api/config`, вкладка «Настройки»); перед записью делается `.bak`. Схема жёстко задана pydantic-моделями в `app.py` (`ConfigPayload`), дефолты для новых полей прописаны там же, так что старые конфиги не ломаются после обновления схемы.

`cfg/service_account.json` — Google сервисный аккаунт для Sheets. Путь настраивается в `google_sheets.service_account_json_path`. Если `google_sheets.enabled=false`, запись в Sheets пропускается, но отчёт всё равно собирается и сохраняется в `output/`.

### Google Sheets writer — самое специфичное место

`sheets_writer.py` пишет в таблицу «Копия kubyshka-zaim.ru». Поведение завязано на *фиксированную* структуру листа, ломать её нельзя:
- На каждый месяц отдельная вкладка вида `«Апрель 26»` (русские названия из `RU_MONTHS`). Если её нет — создаётся копированием шаблонной вкладки.
- Даты — в столбце A в формате `dd.mm.yyyy`. Перед записью ищем строку по дате; если нет — добавляем новую.
- Агрегаты по фиксированным колонкам: `C` приход, `E` клики ЛТ, `F` визиты Метрики, `G` заявки, `AI` переходы-уники, `AC`/`AE` — cost/charge 8connect.
- Шапка кабинетов VK — в `AR2:CL2`. По каждому `account_name` из `ads_manager.cabinets` ищем колонку в шапке и пишем spent. Если кабинета нет в шапке — кладём запасную пару «имя+spent» в `A37+` (см. `FALLBACK_START_ROW`), чтобы данные не потерялись.
- Для формулы «Затраты» в столбце D используется таблица коэффициентов НДС/комиссий `ZATRATY_COEFFS` (одна колонка кабинета = один множитель); `ZATRATY_PLAIN_COLS` (AC, BC) суммируются без множителя.
- Формулы клонируются из строки-эталона `TEMPLATE_ROW = 33`: одну строку читаем через `ValueRenderOption=FORMULA`, кэшируем в `_TEMPLATE_CACHE` по title вкладки и при вставке заменяем ссылки на 33 на номер реальной строки даты через `_TEMPLATE_ROW_RE`.

Если трогаешь layout — меняй вместе: константы колонок, `ZATRATY_COEFFS`, строку-эталон 33 на всех месячных вкладках.

### Output

- `output/<YYYY-MM-DD>_<sub1>.json` — полный JSON-отчёт (пишет и CLI, и веб).
- `output/8connect_<YYYY-MM-DD>.json` — сырой ответ 8connect для отладки (пишет `core._save_eightconnect_raw`).
- Директория в docker-compose примонтирована с хоста, поэтому отчёты переживают пересборку контейнера.

### Планировщик (APScheduler)

`scheduler.py` (`ReportScheduler`) — обёртка над `AsyncIOScheduler`, стартует в FastAPI lifespan и читает блок `schedule` из того же `cfg/lt_vk_config.json`:

```json
"schedule": {"enabled": true, "time": "09:00", "sub1": "kub"}
```

Cron-trigger жёстко привязан к `ZoneInfo("Europe/Samara")` (UTC+4). В заданное время — `build_report(config, вчера, sub1)`, результат пишется так же, как ручной `POST /api/report`: в `output/{date}_{sub1}.json` и в Sheets. «Вчера» вычисляется по Самарской дате (`datetime.now(SAMARA_TZ).date() - 1 day`), независимо от TZ контейнера.

При каждом `POST /api/config` scheduler перечитывает конфиг и перерегистрирует job — ребилд/рестарт не требуется. Статус — `GET /api/schedule` (`enabled`, `next_run` с TZ-offset, `last_run` с датой/sub1/ok/error). Ручной тест — `POST /api/schedule/run-now {"sub1": "..."}` — та же функция, что запускает cron, с трейсом `trigger: "manual"`.

Зависимости в requirements: `APScheduler==3.10.4`, `tzdata==2024.2` (для ZoneInfo в docker-слим).

### Фронт

`static/index.html` + `app.js` + `app.css` — две вкладки (Отчёт / Настройки), ходят только в `/api/*`. Монтируется в FastAPI через `StaticFiles` **после** регистрации API-роутов (иначе SPA-маунт перекроет их).

## Конвенции

- Все внешние вызовы завернуты в `try/except` внутри `collect_*` функций `core.py`: ошибка одного источника не валит отчёт, она ложится в `result[<source>].errors` и рендерится в UI как warning.
- Все денежные/числовые значения из внешних API прогоняются через `_to_int` / `_to_float` — внешние сервисы любят слать строки и `null`.
- Даты в отчёте и в именах файлов — строго ISO `YYYY-MM-DD`. LeadsTech хочет `dd-mm-yyyy` — конвертация внутри `leadstech_client`.
- `spreadsheet_id` в UI можно вставить полным URL — нормализуется на входе через `_extract_spreadsheet_id` (валидатор pydantic).
