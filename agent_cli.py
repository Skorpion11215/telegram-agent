"""
TransferStats Agent — CLI управления.

Использование:
    telegram-agent status       — статус агента и режим управления
    telegram-agent reconfigure  — перенастройка групп (site-режим; открывает локальный сайт)
    telegram-agent stop         — остановить агента
    telegram-agent start        — запустить агента
"""

import configparser
import os
import subprocess
import sys
from pathlib import Path

INSTALL_DIR = "/opt/telegram-agent"
SERVICE_NAME = "telegram-agent"


def _management() -> str:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(Path(INSTALL_DIR) / "agent.ini")
    mode = cfg.get("agent", "management", fallback="site").strip().lower()
    return mode if mode in ("bot", "site") else "site"


def _token() -> str:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(Path(INSTALL_DIR) / "agent.ini")
    return cfg.get("agent", "token", fallback="")


def status():
    """Показать статус агента."""
    print(f"Режим управления группами: {_management()}")
    result = subprocess.run(
        ["systemctl", "is-active", SERVICE_NAME],
        capture_output=True, text=True,
    )
    state = result.stdout.strip()
    if state == "active":
        print("Агент: работает")
    else:
        print(f"Агент: {state}")

    subprocess.run(["journalctl", "-u", SERVICE_NAME, "-n", "10", "--no-pager"])


def reconfigure():
    """Перенастройка групп через локальный сайт (site-режим)."""
    if _management() == "bot":
        print("Управление группами включено в боте: меню «📋 Транслируемые группы».")
        print("Локальный сайт перенастройки в этом режиме не используется.")
        return

    print("Остановка агента...")
    subprocess.run(["systemctl", "stop", SERVICE_NAME])

    print("Запуск локального сайта настройки...")
    os.chdir(INSTALL_DIR)
    python = f"{INSTALL_DIR}/venv/bin/python"

    subprocess.Popen(
        [python, "setup_web.py", "--reconfigure", f"--token={_token()}", "--management=site"],
        cwd=INSTALL_DIR,
    )

    subprocess.Popen(
        ["cloudflared", "tunnel", "--url", "http://127.0.0.1:8080"],
        stdout=open("/tmp/cf_tunnel.log", "w"),
        stderr=subprocess.STDOUT,
    )

    import time
    time.sleep(8)

    try:
        with open("/tmp/cf_tunnel.log") as f:
            from agent_logic import find_tunnel_url
            url = find_tunnel_url(f.read())
            if url:
                print(f"\nОткройте на телефоне:\n  {url}\n")
            else:
                print("Ошибка: не удалось получить URL")
    except FileNotFoundError:
        print("Ошибка: лог cloudflared не найден")


def stop():
    subprocess.run(["systemctl", "stop", SERVICE_NAME])
    print("Агент остановлен")


def start():
    subprocess.run(["systemctl", "start", SERVICE_NAME])
    print("Агент запущен")


COMMANDS = {
    "status": status,
    "reconfigure": reconfigure,
    "stop": stop,
    "start": start,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("Использование: telegram-agent <команда>")
        print("Команды:", ", ".join(COMMANDS.keys()))
        sys.exit(1)
    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
