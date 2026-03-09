# New Ozon Parser

Парсер Ozon: веб-админка и бэкенд. Поиск по бренду и модели, уведомления в Telegram, если цена в карточке не выше заданного порога. Selenium (Chrome), глобальный режим прокси, общее расписание для всех задач.

## Возможности

- **Задачи поиска** — бренд (из списка), модель, минимальная цена. Можно запустить задачу сразу или оставить активной для запуска по общему расписанию.
- **Расписание для всех задач** — один раз настраивается в интерфейсе: по интервалу (сек) или ежедневно в указанное время. Подхватывается без перезапуска.
- **Прокси** — глобальный переключатель «С прокси» / «Без прокси» и загрузка списка прокси (одна строка — один URL).
- **Парсинг** — Chrome (Selenium), ожидание карточек на странице, разбор цен; при цене ≤ порога — запись в «Найденные товары» и сообщение в Telegram.
- **Вкладки админки**: Задачи (создание, список, запуск/остановка, вкл/выкл, удаление), Найденные товары, Прокси.

## Установка

```bash
cd new_ozon_parser
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Отредактируйте .env: TELEGRAM_*, SEARCH_URL1, SEARCH_URL2, при необходимости селекторы и CHROME_VERSION_MAIN
```

## Запуск

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Откройте в браузере: **http://localhost:8000**

## .env

- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — уведомления в Telegram.
- `SEARCH_URL1`, `SEARCH_URL2` — итоговый URL поиска: `SEARCH_URL1 + brand.name + "-" + brand.code + SEARCH_URL2 + model`.
- `CHROME_VERSION_MAIN` — мажорная версия Chrome (по умолчанию 145) для undetected-chromedriver.
- `SELECTOR_TILE_ROOT`, `SELECTOR_PRICE`, `SELECTOR_NAME_LINK`, `SELECTOR_WAIT_TIMEOUT`, `SELECTOR_MAX_CARDS` — селекторы и лимит карточек.

## Бренды

Таблица `brands` заполняется при первом запуске из встроенного списка. При необходимости добавьте записи (name, code) в БД или скриптом.

## Деплой

Инструкция по деплою на сервер (Docker, GitHub Actions) — в файле **[DEPLOY.md](DEPLOY.md)**.

Локальный запуск через Docker:

```bash
docker compose up -d
# приложение на http://localhost:8000
```

## API (кратко)

- `GET/POST /api/settings/use-proxy` — глобальный режим прокси.
- `GET/POST /api/settings/schedule` — глобальное расписание для всех задач.
- `GET /api/brands` — список брендов.
- `GET/POST /api/search-tasks`, `PATCH/DELETE /api/search-tasks/{id}`, `POST .../run`, `.../stop`, `POST /api/search-tasks/run-all`.
- `GET /api/found-products` — найденные товары.
- `GET/POST/DELETE /api/proxies` — список прокси.
