# New Ozon Parser

Веб-приложение на FastAPI для мониторинга выдачи Ozon: хранит задачи поиска по URL, фильтрует по цене, сохраняет найденные товары и отправляет уведомления в Telegram.

## Технологии

- Python 3.11
- FastAPI + Jinja2 (API и админка)
- SQLAlchemy async + SQLite
- APScheduler (глобальное расписание)
- Playwright (через remote Chrome/CDP)

## Возможности

- Управление задачами поиска из админки (`/`): создать, включить/выключить, запустить, остановить, удалить.
- Глобальные настройки расписания: интервал или ежедневный запуск.
- Глобальная настройка прокси и список прокси с ротацией.
- Хранение найденных товаров в БД.
- Telegram-уведомления по подходящим товарам и ошибкам задач.

## Быстрый старт

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Заполните минимум в `.env`:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `USE_REMOTE_CHROME`
- `REMOTE_CHROME_HTTP` (если `USE_REMOTE_CHROME=true`, обычно `http://127.0.0.1:9222`)

`REMOTE_CHROME_WS` можно не задавать: приложение автоматически получает актуальный
`webSocketDebuggerUrl` из `REMOTE_CHROME_HTTP/json/version`. Это переживает рестарты Chrome.

Запуск:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Админка: [http://localhost:8000](http://localhost:8000)

## Docker

```bash
docker compose up -d
```

Для продакшн-сценария и CI/CD см. `DEPLOY.md`.

## Важное по безопасности

- Не храните реальные секреты и куки в репозитории.
- `.env` должен быть только локальным/серверным файлом.
- Если секреты уже утекли в git-историю, замените токены и cookies.

## API (кратко)

- `GET/POST /api/settings/use-proxy`
- `GET/POST /api/settings/schedule`
- `GET /api/brands`
- `GET/POST /api/search-tasks`
- `PATCH/DELETE /api/search-tasks/{id}`
- `POST /api/search-tasks/{id}/run`
- `POST /api/search-tasks/{id}/stop`
- `POST /api/search-tasks/run-all`
- `GET /api/found-products`
- `GET/POST/DELETE /api/proxies`
