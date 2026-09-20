"""
TransferStats Forwarder Agent

Пересылает сообщения из выбранных Telegram-групп на сервер бота.
НЕ читает/обрабатывает сообщения, НЕ отправляет ничего в Telegram.

Режимы управления группами ([agent] management в agent.ini):
  site — выбор групп на локальном сайте настройки (историческое поведение,
         локальный набор статичен до перенастройки);
  bot  — сервер отдаёт список всех групп на сервер бота (POST /api/groups-sync)
         и в heartbeat-ответе получает актуальный набор транслируемых групп
         (forward_ids) по подписанному каналу HMAC — «безопасная обратная связь».

Код полностью открыт: https://github.com/Skorpion11215/telegram-agent
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from agent_logic import (
    ActiveGroups,
    MessageDedup,
    apply_heartbeat_management,
    apply_server_groups,
    write_management_ini,
    build_ingest_body,
    find_tunnel_url,
    groups_sync_payload,
    is_forwardable_message,
    read_config,
    should_reconfigure_requested,
    should_sync_requested,
)

# Импорт критичной логики из .so (Cython) или .py (fallback)
try:
    from core import sign_request, compute_integrity, get_hw_fingerprint, handle_challenge
except ImportError:
    from core._fallback import sign_request, compute_integrity, get_hw_fingerprint, handle_challenge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("agent")

SEEN_TTL = 300                 # 5 минут
HEARTBEAT_INTERVAL = 60        # сек
DIALOGS_RESYNC_INTERVAL = 6 * 3600  # пересинхронизация диалогов раз в 6 часов

# Состояние рантайма заполняется в configure() (тестируемость, импорт без agent.ini)
ST = SimpleNamespace(
    base_dir=None, client=None, http=None,
    api_key="", api_secret="", endpoint="", token="", phone="", management="site",
    active=None, active_ids=set(), dedup=None, last_dialogs_sync=0.0,
)


def configure(base_dir: Path | str | None = None) -> None:
    """Загрузить agent.ini и собрать Telethon-клиент."""
    base = Path(base_dir or os.environ.get("AGENT_HOME") or Path(__file__).parent)
    config_path = base / "agent.ini"
    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    conf = read_config(config_path)
    if not all([conf["api_key"], conf["api_secret"], conf["endpoint"], conf["session"]]):
        raise ValueError("Config incomplete (api_key/api_secret/endpoint/session). "
                         "Run setup first: telegram-agent reconfigure")

    ST.base_dir = base
    ST.api_key = conf["api_key"]
    ST.api_secret = conf["api_secret"]
    ST.endpoint = conf["endpoint"]
    ST.token = conf["token"]
    ST.phone = conf["phone"]
    ST.management = conf["management"]
    ST.active = ActiveGroups(base / "agent_groups.json")
    ST.active_ids = ST.active.load()
    ST.dedup = MessageDedup(ttl_seconds=SEEN_TTL)
    ST.http = None

    ST.client = TelegramClient(StringSession(conf["session"]),
                               int(conf["api_id"] or 0), conf["api_hash"])
    ST.client.add_event_handler(on_message, events.NewMessage())

    log.info("Agent configured: %d groups, endpoint=%s, management=%s",
             len(ST.active_ids), ST.endpoint, ST.management)


# ── HTTP клиент ───────────────────────────────────────────────────────────────

async def _get_http() -> httpx.AsyncClient:
    if ST.http is None or ST.http.is_closed:
        ST.http = httpx.AsyncClient(timeout=10)
    return ST.http


async def _signed_post(path: str, payload: dict) -> httpx.Response | None:
    """POST подписанным запросом; None при сетевой ошибке."""
    body = json.dumps(payload).encode()
    headers = sign_request(ST.api_key, ST.api_secret, body)
    try:
        http = await _get_http()
        return await http.post(f"{ST.endpoint}{path}", content=body, headers=headers)
    except Exception as e:
        log.warning("POST %s failed: %s", path, e)
        return None


# ── Пересылка сообщений ───────────────────────────────────────────────────────

async def on_message(event):
    """Новые сообщения: пересылаем только фактически транслируемые группы."""
    if event.chat_id not in ST.active_ids:
        return
    if not is_forwardable_message(event.message):
        return
    if ST.dedup.is_duplicate(event.message.id):
        return

    body = build_ingest_body(event.chat_id, event.message.id,
                             event.message.text, event.sender_id).encode()
    headers = sign_request(ST.api_key, ST.api_secret, body)
    try:
        http = await _get_http()
        resp = await http.post(f"{ST.endpoint}/ingest", content=body, headers=headers)
        if resp.status_code == 200:
            log.debug("Forwarded msg=%d group=%d", event.message.id, event.chat_id)
        else:
            log.warning("Ingest error %d: %s", resp.status_code, resp.text[:100])
    except Exception as e:
        log.error("Ingest failed: %s", e)


# ── Синхронизация списка групп на сервер ─────────────────────────────────────

async def collect_dialogs() -> list[dict]:
    groups = []
    async for dialog in ST.client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            groups.append({"id": dialog.id, "title": dialog.title})
    return groups


async def sync_dialogs() -> None:
    """Отправить на сервер все группы + (site-режим) локально выбранные."""
    try:
        groups = await collect_dialogs()
    except Exception as e:
        log.warning("collect_dialogs failed: %s", e)
        return
    payload = groups_sync_payload(groups, sorted(ST.active_ids), ST.management)
    resp = await _signed_post("/api/groups-sync", payload)
    if resp is not None and resp.status_code == 200:
        ST.last_dialogs_sync = time.time()
        log.info("Groups sync OK: %d dialogs sent (management=%s)",
                 len(payload["groups"]), ST.management)
    elif resp is not None:
        log.warning("Groups sync error %d: %s", resp.status_code, resp.text[:100])


# ── Reconfigure (только site-режим) ──────────────────────────────────────────

async def _handle_reconfigure():
    """Запустить setup_web + cloudflared для перенастройки групп."""
    if ST.management != "site":
        log.info("Reconfigure requested, but management=bot — группы управляются в боте, игнорируем")
        return
    from agent_logic import port_is_open
    if port_is_open(port=8080):
        log.info("setup-web уже на :8080 — не поднимаем второй сайт (ождём URL)")
        return
    import subprocess

    log.info("Запуск сайта перенастройки...")
    try:
        subprocess.Popen(
            [sys.executable, "setup_web.py", "--reconfigure",
             f"--token={ST.token}", "--management=site"],
            cwd=str(ST.base_dir),
        )
    except Exception as e:
        log.error("Failed to launch setup_web: %s", e)
        return

    try:
        with open("/tmp/cf_tunnel.log", "w") as _cf_log:
            subprocess.Popen(
                ["cloudflared", "tunnel", "--url", "http://127.0.0.1:8080"],
                stdout=_cf_log, stderr=subprocess.STDOUT,
            )
    except Exception as e:
        log.error("Failed to launch cloudflared: %s", e)
        return

    url = None
    for _ in range(20):
        await asyncio.sleep(1)
        try:
            with open("/tmp/cf_tunnel.log") as f:
                url = find_tunnel_url(f.read())
                if url:
                    break
        except FileNotFoundError:
            pass

    if not url:
        log.error("Failed to get cloudflare tunnel URL")
        return

    resp = await _signed_post("/api/reconfigure-url", {"url": url})
    if resp is not None and resp.status_code == 200:
        log.info("Reconfigure URL sent: %s", url)
    else:
        log.warning("Reconfigure URL send failed: %s",
                    resp.status_code if resp is not None else "network")


# ── Heartbeat ─────────────────────────────────────────────────────────────────

async def heartbeat_once() -> None:
    """Один цикл heartbeat: отправка статуса + приём директив сервера."""
    body = json.dumps({
        "groups_count": len(ST.active_ids),
        "management": ST.management,
    }).encode()
    headers = sign_request(ST.api_key, ST.api_secret, body)
    http = await _get_http()
    resp = await http.post(f"{ST.endpoint}/heartbeat", content=body, headers=headers)

    if resp.status_code != 200:
        log.warning("Heartbeat error: %d", resp.status_code)
        return

    data = resp.json()
    challenge = data.get("challenge")
    if challenge:
        log.info("Challenge received, responding...")
        challenge_body = json.dumps({
            "challenge_nonce": challenge,
            "response": handle_challenge(challenge, ST.api_key),
        }).encode()
        challenge_headers = sign_request(ST.api_key, ST.api_secret, challenge_body)
        await http.post(f"{ST.endpoint}/challenge/respond",
                        content=challenge_body, headers=challenge_headers)

    # сервер — хозяин режима управления (профиль бота может переключить bot⇄site)
    new_mode = apply_heartbeat_management(data, ST.management)
    if new_mode:
        ST.management = new_mode
        try:
            write_management_ini(ST.base_dir / "agent.ini", new_mode)
            log.info("Management mode switched per server: %s", new_mode)
        except OSError as e:
            log.warning("failed to persist management: %s", e)

    # bot-режим: сервер диктует набор групп (безопасная обратная связь)
    ST.active_ids = apply_server_groups(data, ST.active_ids, ST.management, ST.active)

    # синхронизация списка диалогов: просьба сервера (sync) или протёкший интервал
    sync_due = time.time() - ST.last_dialogs_sync > DIALOGS_RESYNC_INTERVAL
    if should_sync_requested(data) or sync_due:
        await sync_dialogs()

    if should_reconfigure_requested(data):
        await _handle_reconfigure()


async def heartbeat_loop():
    while True:
        try:
            await heartbeat_once()
        except Exception as e:
            log.warning("Heartbeat failed: %s", e)
        await asyncio.sleep(HEARTBEAT_INTERVAL)


# ── Fingerprint verification ─────────────────────────────────────────────────

async def verify_install() -> None:
    """Отправить binary_hash и hw_fingerprint на сервер при первом запуске."""
    try:
        body = json.dumps({
            "token": ST.token,
            "binary_hash": compute_integrity(),
            "hw_fingerprint": get_hw_fingerprint(),
        }).encode()
        http = await _get_http()
        resp = await http.post(f"{ST.endpoint}/install/verify", content=body)
        if resp.status_code == 200:
            log.info("Install verified OK")
        elif resp.status_code == 403:
            log.error("Install verification FAILED — binary or hardware changed!")
            log.error("Run: telegram-agent reconfigure")
            sys.exit(1)
        else:
            log.warning("Install verify: %d %s", resp.status_code, resp.text[:100])
    except Exception as e:
        log.warning("Install verify failed: %s", e)


# ── Main ──────────────────────────────────────────────────────────────────────

async def run() -> None:
    await verify_install()
    await ST.client.start(phone=ST.phone)
    log.info("Telethon connected, listening on %d groups", len(ST.active_ids))
    await sync_dialogs()
    try:
        await asyncio.gather(
            ST.client.run_until_disconnected(),
            heartbeat_loop(),
        )
    except Exception as e:
        log.error("Agent crashed: %s", e)
        raise


async def main():
    configure()
    await run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (FileNotFoundError, ValueError) as e:
        log.error("%s", e)
        sys.exit(1)
