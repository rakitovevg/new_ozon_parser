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

### Перезапуск контейнера вручную

Все команды выполняйте из каталога деплоя (`DEPLOY_PATH`):

```bash
cd $DEPLOY_PATH
```

Перезапуск всех сервисов из `docker-compose.prod.yml`:

```bash
docker compose -f docker-compose.prod.yml restart
```

Только сервис приложения (в compose он называется `app`):

```bash
docker compose -f docker-compose.prod.yml restart app
```

Полное пересоздание контейнеров (после смены `.env`, переменных окружения или когда нужен «чистый» запуск с тем же образом):

```bash
docker compose -f docker-compose.prod.yml up -d --force-recreate
```

После правки `.env` обычно достаточно:

```bash
docker compose -f docker-compose.prod.yml up -d
```

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

## 9. Домен вместо IP (nginx + HTTPS)

Приложение отдаёт страницы по относительным путям (`/`, `/api/...`), поэтому достаточно проксировать **ваш домен** на порт **8000**. В `.env` добавьте (без слэша в конце):

```env
PUBLIC_BASE_URL=https://parser.example.com
TRUSTED_PROXY_HOSTS=parser.example.com
```

Пример конфига nginx (сертификат — Certbot или свой):

```nginx
server {
    listen 443 ssl http2;
    server_name parser.example.com;

    ssl_certificate     /etc/letsencrypt/live/parser.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/parser.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}

server {
    listen 80;
    server_name parser.example.com;
    return 301 https://$host$request_uri;
}
```

После правки `.env` перезапустите контейнер: `docker compose -f docker-compose.prod.yml up -d`.

Иконка вкладки и логотип в шапке: `/static/favicon.svg`.

---

## 10. Telegram при недоступности сервиса

Само приложение при падении **не может** отправить сообщение. Поэтому используется **отдельный скрипт** `scripts/healthcheck_telegram.py`: раз в несколько минут делает HTTP-запрос к админке; при ошибке шлёт Telegram (и опционально — при восстановлении).

В `.env` уже должны быть `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID`. Дополнительно можно задать:

| Переменная | Смысл |
|------------|--------|
| `HEALTHCHECK_URL` | URL проверки, по умолчанию `http://127.0.0.1:8000/` (при `network_mode: host` у контейнера это хост) |
| `HEALTHCHECK_REPEAT_SEC` | Повторять алерт, пока сервис лежит, каждые N секунд (`0` — только первое уведомление) |
| `HEALTHCHECK_RECOVER_NOTIFY` | `true` — сообщить, когда сервис снова отвечает |

Установка **systemd timer** — сначала зайдите в каталог с клоном репозитория (там должны лежать `deploy/ozon-parser-healthcheck.*` и `scripts/healthcheck_telegram.py`):

```bash
export DEPLOY_PATH=/home/ВАШ_ПОЛЬЗОВАТЕЛЬ/new_ozon_parser   # подставьте реальный путь
cd "$DEPLOY_PATH"
test -f deploy/ozon-parser-healthcheck.service || { echo "Файл не найден: сделайте git pull или проверьте путь"; exit 1; }

sudo cp deploy/ozon-parser-healthcheck.service /etc/systemd/system/
sudo cp deploy/ozon-parser-healthcheck.timer /etc/systemd/system/
sudo sed -i 's|/opt/new_ozon_parser|'"${DEPLOY_PATH}"'|g' /etc/systemd/system/ozon-parser-healthcheck.service

sudo systemctl daemon-reload
sudo systemctl enable --now ozon-parser-healthcheck.timer
```

Если видите `sed: can't read ... No such file` — вы запустили `sed` **до** `cp`, или `cp` не скопировал файл (не тот каталог, нет папки `deploy/`). Порядок: **сначала** две команды `sudo cp ...`, **потом** `sed` и `daemon-reload`.

Если видите `Unit file ... does not exist` — не скопированы `.service` и `.timer` в `/etc/systemd/system/` или опечатка в имени юнита.

Проверка вручную:

```bash
cd $DEPLOY_PATH
set -a && source .env && set +a
python3 scripts/healthcheck_telegram.py; echo exit:$?
```

Статус таймера: `systemctl list-timers | grep healthcheck`.

---

## 11. WireGuard: доступ к Telegram Bot API при блокировке (РФ)

Если с сервера **не открывается** `https://api.telegram.org` (таймаут в `curl`, ошибки `ConnectTimeout` в логах), а **Ozon и остальной интернет** должны идти как раньше, поднимается **туннель только для подсетей Telegram**: на **зарубежном VPS** — WireGuard **сервер**, на **сервере с парсером** — **клиент**. MTProto/MTProxy для приложения Telegram с телефона к этому **не относится**; бот ходит по **HTTPS** на `api.telegram.org`.

### Что понадобится

- Второй VPS **за пределами РФ** (любой недорогой, статический IPv4).
- На обоих хостах: Ubuntu/Debian, открытый **UDP-порт** на зарубежном VPS (например **51820**) в фаерволе и у провайдера.

Актуальный список подсетей Telegram — на странице **[core.telegram.org/cidr](https://core.telegram.org/cidr)**. Ниже — типичный набор **IPv4** для split-tunnel (его можно вставить в `AllowedIPs` клиента; при смене сетей у Telegram обновите список с официальной страницы).

### A. Зарубежный VPS (WireGuard «сервер»)

```bash
sudo apt-get update && sudo apt-get install -y wireguard

umask 077
wg genkey | tee /etc/wireguard/server_private.key | wg pubkey > /etc/wireguard/server_public.key
wg genkey | tee /etc/wireguard/client_private.key | wg pubkey > /etc/wireguard/client_public.key

# Подставьте внешний IP этого VPS
export WG_ENDPOINT=$(curl -4 -s ifconfig.me || hostname -I | awk '{print $1}')
echo "Endpoint IP: $WG_ENDPOINT"
```

Создайте `/etc/wireguard/wg0.conf` (замените `10.66.66.1`/`10.66.66.2` при желании на другую частную подсеть `/24`):

```ini
[Interface]
Address = 10.66.66.1/24
ListenPort = 51820
PrivateKey = <СОДЕРЖИМОЕ /etc/wireguard/server_private.key>
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE
# Если внешний интерфейс не eth0, замените на ens3, enp0s3 и т.д. (см. ip route)

[Peer]
# Клиент — сервер с парсером (РФ)
PublicKey = <СОДЕРЖИМОЕ /etc/wireguard/client_public.key>
AllowedIPs = 10.66.66.2/32
```

Включите форвардинг и поднимите интерфейс:

```bash
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/99-wireguard-forward.conf
sudo sysctl -p /etc/sysctl.d/99-wireguard-forward.conf

sudo wg-quick up wg0
sudo systemctl enable wg-quick@wg0
```

Фаервол (пример для `ufw`):

```bash
sudo ufw allow 51820/udp
sudo ufw allow OpenSSH
sudo ufw enable
```

**Важно:** в `PostUp` интерфейс с интернетом должен совпадать с реальным (`ip -br a` / `ip route get 1.1.1.1`).

### B. Сервер с парсером (РФ, WireGuard «клиент»)

Скопируйте на этот хост **`client_private.key`** и **`server_public.key`** с зарубежного VPS (через `scp`, только по SSH).

Создайте `/etc/wireguard/wg0.conf`:

```ini
[Interface]
Address = 10.66.66.2/24
PrivateKey = <СОДЕРЖИМОЕ client_private.key>

[Peer]
PublicKey = <СОДЕРЖИМОЕ server_public.key>
Endpoint = <IP_ЗАРУБЕЖНОГО_VPS>:51820
PersistentKeepalive = 25
# Только подсети Telegram (split-tunnel). Список обновляйте по https://core.telegram.org/cidr
AllowedIPs = 91.108.0.0/16, 149.154.160.0/20, 185.76.151.0/24, 91.105.192.0/23
```

При необходимости добавьте остальные IPv4 с [core.telegram.org/cidr](https://core.telegram.org/cidr) через запятую в одну строку `AllowedIPs = ...`.

```bash
sudo sysctl -p /etc/sysctl.d/99-wireguard-forward.conf  # или локально: net.ipv4.ip_forward=1 не обязателен только для клиента
sudo wg-quick up wg0
sudo systemctl enable wg-quick@wg0
```

Проверка с **сервера с парсером**:

```bash
sudo wg show
curl -4 -v --connect-timeout 15 "https://api.telegram.org/bot<TOKEN>/getMe"
```

Должен быть ответ HTTP **200** и JSON с `"ok":true`. Токен не публикуйте в чатах и логах.

Контейнеры Docker на этом же хосте обычно используют **маршрутизацию ядра хоста**: трафик к `149.154.x.x` пойдёт в туннель **без** изменений в `docker-compose.yml`. Если у вас нестандартная схема сети Docker — проверьте `curl` из контейнера: `docker compose -f docker-compose.prod.yml exec app curl -4 -s -o /dev/null -w "%{http_code}" https://api.telegram.org/`.

### Если не хватает подсети

Добавьте недостающий префикс в `AllowedIPs` на **клиенте**, перезапустите: `sudo wg-quick down wg0 && sudo wg-quick up wg0`.

### Полный туннель (не рекомендуется на «боевом» сервере)

Теоретически можно указать `AllowedIPs = 0.0.0.0/0`, тогда **весь** исходящий IPv4 пойдёт через зарубежный VPS — проще отладка, но выше нагрузка и риск оборвать SSH, если не настроен отдельный маршрут к вашему IP. Для продакшена предпочтительнее **только подсети Telegram** из cidr.

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

**Нужно перезапустить контейнер вручную** — см. подраздел **«Перезапуск контейнера вручную»** в разделе **7. Дальнейшие деплои** выше.

**Посмотреть логи приложения:**

```bash
docker compose -f docker-compose.prod.yml logs -f app
```

**Запуск удаленного рабочего стола**

```bash
xfreerdp /v:193.187.92.56:3389 /u:parcer /p:'akitov2009' /sec:rdp /cert:ignore /network:lan /bpp:24
```


