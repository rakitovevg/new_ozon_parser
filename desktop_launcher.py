#!/usr/bin/env python3
"""
Точка входа для десктопного запуска под macOS.
Запускает uvicorn и открывает браузер на http://127.0.0.1:8000/
Работает и при запуске из исходников (python desktop_launcher.py),
и из собранного PyInstaller-бинарника (один файл в корне проекта).
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
import socket
from pathlib import Path

# Корень проекта: при сборке PyInstaller — папка с исполняемым файлом,
# иначе — папка с этим скриптом. Ожидается, что рядом лежат .env и data/
if getattr(sys, "frozen", False):
    _base_dir = Path(sys.executable).resolve().parent
    os.environ["OZON_PROJECT_ROOT"] = str(_base_dir)
else:
    _base_dir = Path(__file__).resolve().parent

os.chdir(_base_dir)
if str(_base_dir) not in sys.path:
    sys.path.insert(0, str(_base_dir))

def _wait_for_server_and_open():
    url = "http://127.0.0.1:8000/"
    # Ждём, пока порт 8000 начнёт слушать (до ~15 секунд), потом открываем браузер.
    deadline = time.time() + 15
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.3)
            try:
                s.connect(("127.0.0.1", 8000))
                break
            except OSError:
                time.sleep(0.3)
                continue
    webbrowser.open(url)

def main():
    # Открываем браузер после того, как сервер реально поднимется
    t = threading.Thread(target=_wait_for_server_and_open, daemon=True)
    t.start()
    # Запускаем сервер в этом процессе
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        log_level="info",
    )

if __name__ == "__main__":
    main()
