#!/usr/bin/env bash
# Сборка десктопного запуска под macOS в один исполняемый файл.
# Требования: активный venv с установленными зависимостями, pyinstaller.
#
# Использование:
#   ./build_mac.sh
#
# Результат: dist/OzonParser — один исполняемый файл.
# Положи его в корень проекта (рядом с .env и папкой data/) и запускай оттуда.

set -e
cd "$(dirname "$0")"

if [ -z "$VIRTUAL_ENV" ]; then
  echo "Активируйте venv: source .venv/bin/activate"
  exit 1
fi

pip install pyinstaller -q

# Один файл, консоль (логи в терминал). Папку app целиком кладём в бандл (--add-data).
pyinstaller \
  --onefile \
  --name OzonParser \
  --clean \
  --noconfirm \
  --paths . \
  --add-data "app:app" \
  --hidden-import aiosqlite \
  --hidden-import app.main \
  --hidden-import app.config \
  --hidden-import app.database \
  --hidden-import app.models \
  --hidden-import app.parser \
  --hidden-import app.scheduler \
  --hidden-import app.telegram \
  --hidden-import app.proxy_rotation \
  desktop_launcher.py

echo ""
echo "Готово. Исполняемый файл: dist/OzonParser"
echo "Скопируй его в корень проекта (рядом с .env и data/) и запускай: ./OzonParser"
