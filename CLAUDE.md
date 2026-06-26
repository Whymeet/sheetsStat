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
- `scheduler.ReportScheduler` — автозапуск каждого бренда в его собственное время по Европа/Самара за предыдущий день

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

### Конфиг и профили (бренды)

Каждый бренд — отдельный **профиль**: файл `cfg/profiles/<id>.json` со своим набором всех источников, `sub1`, `google_sheets.spreadsheet_id` и **своим расписанием** `schedule: {enabled, time}`. Глобальный манифест `cfg/profiles.json` хранит порядок профилей, активный (`active_id`) и дефолтное время для новых брендов. Профили редактируются через UI (`POST /api/profiles/{id}/config`); перед записью делается `.bak`. Схема задана pydantic-моделью `ConfigPayload` в `app.py`, дефолты для новых полей там же — старые профили не ломаются после обновления схемы.

Новый бренд проще всего завести **копией** существующего (`POST /api/profiles` с `copy_from`, или кнопка «копировать» в UI) и сменой `sub1` / `spreadsheet_id` / кредов. Раскладка таблицы у всех брендов одинаковая (см. sheets_writer), поэтому таблица нового бренда должна быть копией того же шаблона.

`cfg/lt_vk_config.json` — legacy: при первом старте `ensure_profiles_migrated()` мигрирует его в первый профиль, дальше используется только CLI-обёрткой. `cfg/service_account.json` — Google сервисный аккаунт для Sheets, путь в `google_sheets.service_account_json_path`. При `google_sheets.enabled=false` запись в Sheets пропускается, но отчёт всё равно собирается в `output/`.

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

- `output/<YYYY-MM-DD>_<sub1>__<profile_id>.json` — полный JSON-отчёт (пишет веб/планировщик; `profile_id` в имени, т.к. `sub1` у разных брендов может совпадать). CLI-режим пишет без суффикса профиля.
- `output/8connect_<YYYY-MM-DD>.json` — сырой ответ 8connect для отладки (пишет `core._save_eightconnect_raw`).
- Директория в docker-compose примонтирована с хоста, поэтому отчёты переживают пересборку контейнера.

### Планировщик (APScheduler) — расписание на каждый бренд

`scheduler.py` (`ReportScheduler`) — обёртка над `AsyncIOScheduler`, стартует в FastAPI lifespan. У **каждого** профиля своё расписание `schedule: {enabled, time}` в его файле; планировщик регистрирует **по одному cron-job на бренд** (`report__<pid>`), у каждого своё время. Cron жёстко привязан к `ZoneInfo("Europe/Samara")` (UTC+4). В заданное время — `build_report(config, вчера, sub1)` только этого бренда, результат пишется как ручной `POST /api/report`: в `output/{date}_{sub1}__{pid}.json` и в Sheets. «Вчера» — по Самарской дате (`datetime.now(SAMARA_TZ).date() - 1 day`), независимо от TZ контейнера.

`_profiles_provider()` отдаёт `(pid, config, sub1, schedule)` по всем профилям; `_effective_schedule` подставляет время из манифеста для ещё не мигрированных профилей. `reload()` пересобирает набор job'ов (добавляет включённые, снимает выключенные/удалённые). При сохранении конфига/расписания (`POST /api/profiles/{id}/config` или `.../schedule`) дёргается `scheduler.reload()`; плюс watcher раз в 10с авто-перечитывает расписания — ребилд/рестарт не нужен. Статус — `GET /api/schedule`: сводка (`enabled`, ближайший `next_run`) + `profiles[pid] = {enabled, next_run, last_run}`. Ручной прогон — `POST /api/schedule/run-now {profile_id?}` (без тела — все бренды), трейс `trigger: "manual"`.

Миграция: `ensure_profile_schedules_seeded()` при старте засевает `schedule` в профили, у которых его нет, из общего времени манифеста (одноразово, идемпотентно; ошибка записи логируется warning'ом и **не валит старт** — расписание тогда берётся из манифеста на лету).

Зависимости в requirements: `APScheduler==3.10.4`, `tzdata==2024.2` (для ZoneInfo в docker-слим).

### Фронт

`static/index.html` + `app.js` + `app.css` — раскладка «**сайдбар брендов + рабочая область**», ходит только в `/api/*`. Слева список брендов с поиском, бейджем расписания (`⏰ HH:MM` / «выкл») и индикатором последнего запуска; снизу — кнопки создать/копировать/переименовать/удалить, плюс те же действия по **ПКМ на бренде** (контекстное меню адресуется конкретному бренду, не активному). Справа 4 вкладки: **Обзор** (дашборд всех брендов: тумблер расписания + время правятся прямо в таблице → `POST /api/profiles/{id}/schedule`, колонки next_run / последний запуск / «прогнать за вчера»), **Отчёт**, **Настройки** (скоупятся на выбранный бренд, включая карточку его расписания), **История** (с фильтром по бренду). Статика монтируется в FastAPI через `StaticFiles` **после** API-роутов (иначе SPA-маунт перекроет их).

## Конвенции

- Все внешние вызовы завернуты в `try/except` внутри `collect_*` функций `core.py`: ошибка одного источника не валит отчёт, она ложится в `result[<source>].errors` и рендерится в UI как warning.
- Все денежные/числовые значения из внешних API прогоняются через `_to_int` / `_to_float` — внешние сервисы любят слать строки и `null`.
- Даты в отчёте и в именах файлов — строго ISO `YYYY-MM-DD`. LeadsTech хочет `dd-mm-yyyy` — конвертация внутри `leadstech_client`.
- `spreadsheet_id` в UI можно вставить полным URL — нормализуется на входе через `_extract_spreadsheet_id` (валидатор pydantic).
