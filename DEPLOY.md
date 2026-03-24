# Пошаговый деплой на сервер

Деплой идёт через Docker: GitHub Actions собирает образ, пушит в GHCR, по SSH на сервере выполняется `docker compose pull` и `up -d`.

---

## 1. Подготовка сервера.

- Сервер с Linux (Ubuntu/Debian удобнее всего).
- Доступ по SSH под пользователем, от которого будет запускаться Docker.

**Установка Docker и Docker Compose (если ещё нет):**

```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Проверка:

```bash
docker --version
docker compose version
```

Добавьте пользователя в группу `docker`, чтобы не писать `sudo`:

```bash
sudo usermod -aG docker $USER
# выйти из SSH и зайти снова
```

---

## 2. Клонирование репозитория на сервер

Выберите каталог, где будет лежать проект (например `/home/user/new_ozon_parser`). Дальше этот путь будет **DEPLOY_PATH**.

```bash
cd ~
git clone https://github.com/YOUR_USERNAME/new_ozon_parser.git
cd new_ozon_parser
```

Замените `YOUR_USERNAME` на ваш логин GitHub (или полный URL репозитория).

---

## 3. Файл `.env` на сервере

В каталоге проекта создайте `.env` с теми же переменными, что и локально. Минимум:

```bash
nano .env
```

Пример содержимого:

```env
DATABASE_URL=sqlite+aiosqlite:////app/data/ozon.db
TELEGRAM_BOT_TOKEN=ваш_токен
TELEGRAM_CHAT_ID=ваш_chat_id
USE_REMOTE_CHROME=true
REMOTE_CHROME_HTTP=http://127.0.0.1:9222
SELECTOR_TILE_ROOT=.tile-root
SELECTOR_PRICE=.c35_3_13-a6
SELECTOR_NAME_LINK=.ki4_24
SELECTOR_WAIT_TIMEOUT=30
SELECTOR_MAX_CARDS=100
TASK_HARD_TIMEOUT_SECONDS=600
```

`REMOTE_CHROME_WS` можно не фиксировать в `.env`: после рестарта Chrome его browser-id меняется.
Приложение автоматически берет актуальный `webSocketDebuggerUrl` через
`REMOTE_CHROME_HTTP/json/version`.

Создайте каталог для БД (том в compose примонтирует его):

```bash
mkdir -p data
```

Добавьте строку с образом из GitHub Container Registry (подставьте свой `OWNER/REPO`, как в URL репозитория, **в нижнем регистре**):

```env
IMAGE=ghcr.io/owner/repo:latest
```

Docker Compose подставляет `IMAGE` из этого же `.env` при запуске `docker-compose.prod.yml`.

Файл `.env` в репозиторий не коммитить.

### Chrome на сервере (GUI + CDP)

Парсер подключается к уже запущенному Chrome на **той же машине**. Образ в Docker использует `network_mode: host`, чтобы внутри контейнера работало `REMOTE_CHROME_HTTP=http://127.0.0.1:9222`.

1. Поднимите Chrome с remote debugging (как у вас в `~/.config/autostart/`, порт **9222**).
2. После перезагрузки сначала зайдите по RDP (чтобы стартовала сессия и Chrome), либо настройте автологин/отложенный старт приложения.

---

## 4. Секреты в GitHub

В репозитории: **Settings → Secrets and variables → Actions → New repository secret.**

Создайте четыре секрета:

| Имя             | Значение |
|-----------------|----------|
| `DEPLOY_HOST`   | IP или домен сервера (например `123.45.67.89` или `myserver.com`) |
| `DEPLOY_USER`   | SSH-пользователь (например `ubuntu` или `root`) |
| `DEPLOY_SSH_KEY`| Приватный SSH-ключ: содержимое `~/.ssh/id_rsa` (или другого ключа) с вашего компьютера. Копировать целиком, включая строки `-----BEGIN ... KEY-----` и `-----END ... KEY-----`. |
| `DEPLOY_PATH`   | Полный путь к проекту на сервере (например `/home/ubuntu/new_ozon_parser`) |

**Если репозиторий приватный** — добавьте ещё один секрет:

| Имя           | Значение |
|---------------|----------|
| `GHCR_TOKEN`  | GitHub Personal Access Token (PAT) с правом **read:packages**, чтобы сервер мог скачивать образ из GitHub Container Registry. Создать: GitHub → Settings → Developer settings → Personal access tokens. |

---

## 5. Первый деплой

**Вариант А: из интерфейса GitHub**

1. Откройте репозиторий → вкладка **Actions**.
2. Слева выберите workflow **Deploy**.
3. Справа нажмите **Run workflow** → **Run workflow**.

**Вариант Б: пуш в ветку main**

```bash
git push origin main
```

Workflow сам запустится.

В **Actions** откройте последний запуск и убедитесь, что оба шага зелёные: **build-and-push** и **deploy**.

---

## 6. Проверка на сервере.

По SSH на сервер:

```bash
cd /home/user/new_ozon_parser   # ваш DEPLOY_PATH
docker compose -f docker-compose.prod.yml ps
```

Должен быть контейнер `app` в статусе **Up**. Логи:

```bash
docker compose -f docker-compose.prod.yml logs -f app
```

Приложение слушает порт **8000**. Проверка с сервера:

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/
```

Ожидается `200`.

Чтобы открыть приложение снаружи, настройте фаервол/security group (открыть порт 8000) и при необходимости nginx как обратный прокси с HTTPS.

---

## 7. Дальнейшие деплои

При каждом пуше в ветку **main** workflow сам:

1. Соберёт новый Docker-образ.
2. Отправит его в GHCR.
3. По SSH зайдёт на сервер, выполнит `docker compose pull` и `up -d`.

Обновление происходит без простоя (compose поднимает новый контейнер и останавливает старый). Данные БД сохраняются в каталоге `data/` на сервере.

---

## 8. Автозапуск после перезагрузки сервера

1. **Docker** должен быть включён:

   ```bash
   sudo systemctl enable docker
   ```

2. В `docker-compose.prod.yml` у сервиса `app` указано `restart: unless-stopped` — после старта Docker контейнер поднимется сам.

3. **Первый** запуск стека после ребута: если используете только Docker без systemd-обёртки, выполните один раз на сервере:

   ```bash
   cd $DEPLOY_PATH
   docker compose -f docker-compose.prod.yml up -d
   ```

4. **Опционально — systemd-юнит**, чтобы `docker compose up -d` выполнялся при загрузке (удобно, если контейнер когда-то остановили вручную):

   ```bash
   sudo cp deploy/ozon-parser.service /etc/systemd/system/ozon-parser.service
   sudo sed -i 's|/opt/new_ozon_parser|'"$DEPLOY_PATH"'|g' /etc/systemd/system/ozon-parser.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now ozon-parser.service
   ```

   В `DEPLOY_PATH` в `.env` обязательно есть `IMAGE=ghcr.io/...`.

5. **Chrome** с CDP не входит в контейнер — поднимайте его на хосте (autostart в сессии пользователя, см. выше). Парсер в Docker ждёт `http://127.0.0.1:9222`.

---

## Возможные проблемы

**Ошибка при SSH в Actions**  
- Проверьте `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PATH`.  
- Убедитесь, что на сервер можно зайти по ключу: с вашего ПК `ssh DEPLOY_USER@DEPLOY_HOST`. В GitHub должен быть добавлен тот же приватный ключ, что вы используете для этого входа.

**Ошибка при `docker compose pull` (403 Forbidden)**  
- Репозиторий приватный и не задан `GHCR_TOKEN`, либо у токена нет права **read:packages**. Добавьте/обновите секрет `GHCR_TOKEN`.

**Сайт не открывается снаружи**  
- Откройте порт 8000 в фаерволе: `sudo ufw allow 8000` (если используете ufw).  
- Для HTTPS настройте nginx (или другой прокси) перед приложением.

**Нужно перезапустить контейнер вручную:**

```bash
cd $DEPLOY_PATH
docker compose -f docker-compose.prod.yml restart app
```

**Посмотреть логи приложения:**

```bash
docker compose -f docker-compose.prod.yml logs -f app
```
