# Python + Chromium для Selenium (парсер Ozon)
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Chromium и зависимости для headless
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    fonts-liberation \
    libnss3 libx11-6 libatk-bridge2.0-0 libatspi2.0-0 \
    libgtk-3-0 libxcomposite1 libxcursor1 libxdamage1 libxrandr2 libgbm1 \
    wget \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# undetected_chromedriver ищет chrome в стандартных путях
ENV CHROME_BIN=/usr/bin/chromium \
    CHROMIUM_FLAGS="--no-sandbox --disable-dev-shm-usage --disable-gpu --headless=new"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p data

EXPOSE 8000

# Переменные для SQLite в контейнере (можно переопределить через -e или .env)
ENV DATABASE_URL=sqlite+aiosqlite:////app/data/ozon.db

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
