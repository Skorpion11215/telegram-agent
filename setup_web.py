"""
TransferStats Agent — Локальный веб-сайт настройки.

Запускается на 127.0.0.1:8080.
Cloudflare Tunnel проксирует его наружу для доступа с телефона.
После завершения настройки сайт автоматически закрывается.

Режимы:
  --token TOKEN                    Первоначальная настройка
  --reconfigure --token TOKEN      Перенастройка групп (site-режим: только шаг групп)
  --management bot|site            Режим управления группами (иначе из agent.ini)

bot-режим: сайт выполняет ТОЛЬКО авторизацию Telegram (API ключи, телефон, код,
2FA); выбор транслируемых групп перенесён в бота — сервер получает полный список
групп (POST /api/groups-sync при запуске агента) и отдаёт активный набор
обратно по heartbeat.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import web

from agent_logic import (
    MANAGEMENT_BOT,
    MANAGEMENT_SITE,
    normalize_management,
    read_config,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("setup_web")

AGENT_DIR = Path(__file__).parent
TEMPLATE_DIR = AGENT_DIR / "templates"
CONFIG_PATH = AGENT_DIR / "agent.ini"
GROUPS_PATH = AGENT_DIR / "agent_groups.json"
_AGENT_TOKEN = ""
_RECONFIGURE_MODE = False
_MANAGEMENT = MANAGEMENT_SITE

# Telethon клиент (создаётся при вводе API credentials)
_telethon_client = None
_session_string = None
_phone = None
_phone_code_hash = None
_credentials = {"api_key": "", "api_secret": "", "endpoint": "", "fetched": False}


def _read_existing_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return read_config(CONFIG_PATH)
    except Exception:
        return {}


def _read_selected_groups() -> list:
    if GROUPS_PATH.exists():
        try:
            return json.loads(GROUPS_PATH.read_text()).get("groups", [])
        except Exception:
            pass
    return []


async def _collect_dialogs() -> list:
    groups = []
    async for dialog in _telethon_client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            groups.append({"id": dialog.id, "title": dialog.title})
    return groups


async def _fetch_credentials(endpoint: str) -> bool:
    """Получить api_key/api_secret с сервера по токену (один раз за сессию)."""
    if _credentials["fetched"]:
        return bool(_credentials["api_key"] and _credentials["api_secret"])
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{endpoint}/api/agent-credentials?token={_AGENT_TOKEN}")
            if resp.status_code == 200:
                creds = resp.json()
                _credentials["api_key"] = creds.get("api_key", "")
                _credentials["api_secret"] = creds.get("api_secret", "")
                _credentials["endpoint"] = endpoint
                _credentials["fetched"] = True
                log.info("credentials fetched from server")
                return bool(_credentials["api_key"] and _credentials["api_secret"])
            log.warning("server returned %d for credentials", resp.status_code)
    except Exception as e:
        log.warning("failed to get credentials: %s", e)
    _credentials["endpoint"] = endpoint
    return False


def _write_ini(fields: dict) -> None:
    config_content = f"""[telegram]
api_id = {fields['api_id']}
api_hash = {fields['api_hash']}
phone = {fields['phone']}
session = {fields['session']}

[agent]
token = {fields['token']}
api_key = {fields['api_key']}
api_secret = {fields['api_secret']}
endpoint = {fields['endpoint']}
management = {fields['management']}

[groups]
ids = {fields['groups']}
"""
    CONFIG_PATH.write_text(config_content, encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)


# ── Handlers ──────────────────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    html_path = TEMPLATE_DIR / "setup.html"
    if not html_path.exists():
        return web.Response(text="Template not found", status=500)
    html = html_path.read_text(encoding="utf-8")
    html = html.replace("const RECONFIGURE = false;",
                        f"const RECONFIGURE = {'true' if _RECONFIGURE_MODE else 'false'};")
    html = html.replace("const MANAGEMENT = 'site';",
                        f"const MANAGEMENT = '{_MANAGEMENT}';")
    return web.Response(text=html, content_type="text/html")


async def handle_send_code(request: web.Request) -> web.Response:
    """Отправить код подтверждения на номер телефона."""
    global _telethon_client, _phone, _phone_code_hash

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    api_id = body.get("api_id")
    api_hash = body.get("api_hash", "").strip()
    phone = body.get("phone", "").strip()

    log.info("send-code: api_id=%s phone=%s***", api_id, phone[:4] if phone else "")

    if not all([api_id, api_hash, phone]):
        return web.json_response({"error": "api_id, api_hash, phone required"}, status=400)

    try:
        api_id_int = int(api_id)
    except (ValueError, TypeError):
        return web.json_response({"error": "API ID должен быть числом"}, status=400)

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        if _telethon_client:
            try:
                await _telethon_client.disconnect()
            except Exception:
                pass

        _telethon_client = TelegramClient(StringSession(), api_id_int, api_hash)
        await _telethon_client.connect()
        log.info("send-code: connected to Telegram")

        if not await _telethon_client.is_user_authorized():
            result = await _telethon_client.send_code_request(phone)
            _phone = phone
            _phone_code_hash = result.phone_code_hash
            log.info("send-code: code sent")

        return web.json_response({"ok": True})
    except Exception as e:
        log.error("send-code error: %s", e)
        if _telethon_client:
            try:
                await _telethon_client.disconnect()
            except Exception:
                pass
            _telethon_client = None
        return web.json_response({"error": str(e)[:200]}, status=500)


async def handle_verify_code(request: web.Request) -> web.Response:
    """Подтвердить код из Telegram."""
    global _session_string

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    code = body.get("code", "").strip()
    if not code:
        return web.json_response({"error": "code required"}, status=400)

    if not _telethon_client or not _phone or not _phone_code_hash:
        return web.json_response({"error": "send_code first"}, status=400)

    try:
        await _telethon_client.sign_in(_phone, code, phone_code_hash=_phone_code_hash)
        _session_string = _telethon_client.session.save()
        return web.json_response({"ok": True, "needs_2fa": False})
    except Exception as e:
        error_str = str(e).lower()
        if "password" in error_str or "2fa" in error_str:
            return web.json_response({"ok": False, "needs_2fa": True})
        return web.json_response({"error": str(e)}, status=500)


async def handle_verify_2fa(request: web.Request) -> web.Response:
    """Подтвердить 2FA пароль."""
    global _session_string

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    password = body.get("password", "")
    if not password:
        return web.json_response({"error": "password required"}, status=400)

    if not _telethon_client:
        return web.json_response({"error": "no client"}, status=400)

    try:
        await _telethon_client.sign_in(password=password)
        _session_string = _telethon_client.session.save()
        log.info("2FA: authenticated OK, session saved")
        return web.json_response({"ok": True})
    except Exception as e:
        log.error("2FA error: %s", e)
        return web.json_response({"error": str(e)[:200]}, status=500)


async def _ensure_client_for_groups() -> web.Response | None:
    """Подключить существующую сессию в reconfigure-режиме; None если всё ок."""
    global _telethon_client, _session_string
    if _telethon_client:
        return None
    if _RECONFIGURE_MODE:
        config = _read_existing_config()
        session_str = config.get("session", "")
        api_id = config.get("api_id", "")
        api_hash = config.get("api_hash", "")
        if not all([session_str, api_id, api_hash]):
            return web.json_response({"error": "No existing session found in agent.ini"}, status=400)
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            _telethon_client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
            await _telethon_client.connect()
            _session_string = session_str
            log.info("reconfigure: connected with existing session")
        except Exception as e:
            log.error("reconfigure: failed to connect: %s", e)
            return web.json_response({"error": f"Failed to connect: {e}"}, status=500)
        return None
    return web.json_response({"error": "authenticate first"}, status=400)


async def handle_groups(request: web.Request) -> web.Response:
    """Получить список групп пользователя."""
    err = await _ensure_client_for_groups()
    if err:
        return err
    try:
        groups = await _collect_dialogs()
        return web.json_response({"ok": True, "groups": groups})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_selected_groups(request: web.Request) -> web.Response:
    selected = _read_selected_groups()
    return web.json_response({"ok": True, "selected": selected})


async def handle_save(request: web.Request) -> web.Response:
    """Сохранить конфигурацию и запустить/перезапустить агента."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    selected_groups = body.get("groups", [])

    if _RECONFIGURE_MODE:
        config = _read_existing_config()
        if not config:
            return web.json_response({"error": "No existing config found"}, status=400)
        api_id, api_hash = config.get("api_id", ""), config.get("api_hash", "")
        phone = config.get("phone", "")
        api_key, api_secret = config.get("api_key", ""), config.get("api_secret", "")
        endpoint = config.get("endpoint", "")
        token = config.get("token", "") or _AGENT_TOKEN
        session = config.get("session", "") or (_session_string or "")
        management = config.get("management", MANAGEMENT_SITE)

        if not all([api_id, api_hash, phone, endpoint, session]):
            return web.json_response({"error": "Incomplete existing config"}, status=400)

        if management == MANAGEMENT_BOT and selected_groups:
            # В bot-режиме группы выбираются в боте — локальный набор не меняем
            selected_groups = _read_selected_groups()
        log.info("reconfigure saving: %d groups, management=%s", len(selected_groups), management)
    else:
        token = _AGENT_TOKEN
        api_id = body.get("api_id")
        api_hash = body.get("api_hash", "").strip()
        phone = body.get("phone", "").strip()
        endpoint = body.get("endpoint", "").strip() or (_read_existing_config().get("endpoint", ""))

        if not all([api_id, api_hash, phone, endpoint]):
            return web.json_response({"error": "missing fields"}, status=400)
        if not _session_string:
            return web.json_response({"error": "authenticate first"}, status=400)

        if not await _fetch_credentials(endpoint):
            return web.json_response(
                {"error": "Failed to get agent credentials from server. Check token."}, status=400)

        api_key, api_secret = _credentials["api_key"], _credentials["api_secret"]
        session = _session_string
        management = _MANAGEMENT

        if management == MANAGEMENT_BOT:
            # набор групп в bot-режиме назначает сервер — до первой синхронизации пусто
            selected_groups = []

    _write_ini({
        "api_id": api_id, "api_hash": api_hash, "phone": phone, "session": session,
        "token": token, "api_key": api_key, "api_secret": api_secret,
        "endpoint": endpoint, "management": management,
        "groups": ",".join(str(g) for g in selected_groups),
    })

    GROUPS_PATH.write_text(json.dumps({"groups": [int(g) for g in selected_groups]}),
                           encoding="utf-8")
    os.chmod(GROUPS_PATH, 0o600)

    for cmd in (["systemctl", "enable", "telegram-agent"],
                ["systemctl", "restart", "telegram-agent"]):
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.wait()

    if _telethon_client:
        try:
            await _telethon_client.disconnect()
        except Exception:
            pass

    return web.json_response({"ok": True, "management": management})


async def handle_finish(request: web.Request) -> web.Response:
    """Корректно завершить работу веб-сервера (только POST)."""
    app = request.app
    asyncio.get_event_loop().call_later(2, lambda: app.loop.stop())
    # self-disable setup-unit (если нас запустили через systemd при авто-установке)
    try:
        proc = await asyncio.create_subprocess_exec("systemctl", "disable", "telegram-agent-setup")
        await proc.wait()
    except Exception:
        pass
    return web.json_response({"ok": True, "message": "Shutting down..."})


# ── App ───────────────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_post("/api/send-code", handle_send_code)
    app.router.add_post("/api/verify-code", handle_verify_code)
    app.router.add_post("/api/verify-2fa", handle_verify_2fa)
    app.router.add_get("/api/groups", handle_groups)
    app.router.add_get("/api/selected-groups", handle_selected_groups)
    app.router.add_post("/api/save", handle_save)
    app.router.add_post("/api/finish", handle_finish)
    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", default="", help="Agent token for server auth")
    parser.add_argument("--reconfigure", action="store_true", help="Reconfigure mode (skip auth steps)")
    parser.add_argument("--management", default=None, help="Group management: bot|site (default from agent.ini)")
    args = parser.parse_args()
    _AGENT_TOKEN = args.token
    _RECONFIGURE_MODE = args.reconfigure
    base = _read_existing_config()
    _MANAGEMENT = normalize_management(args.management or base.get("management"))
    mode = "reconfigure" if _RECONFIGURE_MODE else "setup"
    log.info("Starting %s web server on http://127.0.0.1:8080 (management=%s)", mode, _MANAGEMENT)
    web.run_app(create_app(), host="127.0.0.1", port=8080, print=None)
