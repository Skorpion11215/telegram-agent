"""
TransferStats Agent — чистая логика (без сетевых/тяжёлых зависимостей).

Модуль намеренно не импортирует telethon/httpx/aiohttp:
вся клиентская логика (дедупликация, набор транслируемых групп,
формирование payload'ов, парсинг URL туннеля) проверяется тестами здесь.

Агент НЕ выполняет функций бота: только чтение списка диалогов,
пересылка сообщений выбранных групп и приём команды «какие группы
транслировать» от сервера по подписанному каналу (heartbeat/HMAC).
"""

import configparser
import json
import os
import re
import tempfile
import time
from pathlib import Path

MANAGEMENT_BOT = "bot"
MANAGEMENT_SITE = "site"
MANAGEMENT_VALUES = (MANAGEMENT_BOT, MANAGEMENT_SITE)

MAX_GROUPS = 1000
MAX_TITLE_LEN = 200

TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def normalize_management(raw: str | None) -> str:
    """management=bot|site; любые посторонние значения → site (обратная совместимость)."""
    if raw and str(raw).strip().lower() == MANAGEMENT_BOT:
        return MANAGEMENT_BOT
    return MANAGEMENT_SITE


def normalize_group_ids(ids) -> list[int]:
    """Очистка/дедупликация списка id групп с сохранением порядка."""
    out: list[int] = []
    for x in ids or []:
        try:
            gid = int(x)
        except (TypeError, ValueError):
            continue
        if gid not in out:
            out.append(gid)
    return out


def sanitize_groups(groups) -> list[dict]:
    """[{id,title}] → чистый список (только int id, title-cut, дедупликация, cap)."""
    seen: dict[int, dict] = {}
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        try:
            gid = int(g.get("id"))
        except (TypeError, ValueError):
            continue
        title = str(g.get("title", ""))[:MAX_TITLE_LEN]
        if gid not in seen:
            seen[gid] = {"id": gid, "title": title}
        if len(seen) >= MAX_GROUPS:
            break
    return list(seen.values())


def groups_sync_payload(groups, selected, management: str) -> dict:
    """
    Тело POST /api/groups-sync.

    bot: selected всегда [] — набор транслируемых групп владеет сервер.
    site: selected — локально выбранный список (зеркалим на сервер для отображения).
    """
    return {
        "groups": sanitize_groups(groups),
        "selected": normalize_group_ids(selected) if management == MANAGEMENT_SITE else [],
    }


def apply_heartbeat_directive(resp_data, management: str) -> list[int] | None:
    """
    Что сказал сервер в heartbeat-ответе по набору групп.
    Возвращает новый список ids либо None (ничего не менять).
    """
    if management != MANAGEMENT_BOT or not isinstance(resp_data, dict):
        return None
    forward_ids = resp_data.get("forward_ids")
    if forward_ids is None:
        # Конфигурация синхронизирована, но сервер ещё ничего не выбирал
        # (available_groups пусты) — не трогаем локальный набор.
        return None
    return normalize_group_ids(forward_ids)


def should_sync_requested(resp_data) -> bool:
    return bool(isinstance(resp_data, dict) and resp_data.get("sync"))


def should_reconfigure_requested(resp_data) -> bool:
    return bool(isinstance(resp_data, dict) and resp_data.get("reconfigure"))


def port_is_open(host: str = "127.0.0.1", port: int = 8080,
                 timeout: float = 0.5) -> bool:
    """Занят ли порт (сайт reconfigure уже поднят другим процессом)."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_tunnel_url(text: str) -> str | None:
    """Достать trycloudflare URL из вывода cloudflared."""
    match = TUNNEL_URL_RE.search(text or "")
    return match.group() if match else None


class ActiveGroups:
    """Динамический набор транслируемых групп с атомарной записью на диск."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> set[int]:
        if not self.path.exists():
            return set()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return set(normalize_group_ids(data.get("groups", [])))
        except (ValueError, OSError):
            return set()

    def save(self, ids: set[int]) -> None:
        payload = json.dumps({"groups": sorted(ids)}, ensure_ascii=False)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp_path, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


class MessageDedup:
    """Окно дедупликации id сообщений (TTL)."""

    def __init__(self, ttl_seconds: float = 300.0):
        self.ttl = ttl_seconds
        self._seen: dict[int, float] = {}

    def is_duplicate(self, message_id: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        expired = [k for k, v in self._seen.items() if now - v > self.ttl]
        for k in expired:
            del self._seen[k]
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        return False


def is_forwardable_message(message) -> bool:
    """Пустые/ответные сообщения не пересылаются (историческое поведение, инкапсулировано)."""
    if getattr(message, "reply_to", None):
        return False
    text = getattr(message, "text", None) or ""
    return bool(text.strip())


def build_ingest_body(chat_id: int, message_id: int, text: str, sender_id) -> str:
    return json.dumps({
        "group_id": chat_id,
        "message_id": message_id,
        "text": text,
        "sender_id": sender_id,
    })


def read_config(config_path: Path | str) -> dict:
    """Прочитать agent.ini в плоский dict ключей, релевантных рантайму.

    interpolation=None: session-строка Telethon может содержать '%'.
    """
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(str(config_path))
    tg = cfg["telegram"] if cfg.has_section("telegram") else {}
    ag = cfg["agent"] if cfg.has_section("agent") else {}
    return {
        "api_id": cfg.get("telegram", "api_id", fallback="") or "",
        "api_hash": cfg.get("telegram", "api_hash", fallback="") or "",
        "phone": cfg.get("telegram", "phone", fallback="") or "",
        "session": cfg.get("telegram", "session", fallback="") or "",
        "token": cfg.get("agent", "token", fallback="") or "",
        "api_key": cfg.get("agent", "api_key", fallback="") or "",
        "api_secret": cfg.get("agent", "api_secret", fallback="") or "",
        "endpoint": (cfg.get("agent", "endpoint", fallback="") or "").rstrip("/"),
        "management": normalize_management(cfg.get("agent", "management", fallback=MANAGEMENT_SITE)),
        "_tg": tg, "_ag": ag,
    }


def apply_heartbeat_management(resp_data, current: str) -> str | None:
    """Сервер авторитетен по management (heartbeat 'management' field). None — не менять."""
    if not isinstance(resp_data, dict):
        return None
    want = normalize_management(resp_data.get("management"))
    if resp_data.get("management") is None or want == current:
        return None
    return want


def write_management_ini(ini_path, mode: str) -> None:
    """Обновить [agent] management в agent.ini, не трогая остальные поля."""
    from pathlib import Path
    path = Path(ini_path)
    text = path.read_text(encoding="utf-8")
    m = re.search(r"(?m)^management\s*=.*$", text)
    line = f"management = {mode}"
    if m:
        text = text[:m.start()] + line + text[m.end():]
    else:
        # секция [agent] без ключа — вставить после заголовка
        text = re.sub(r"(?m)^\[agent\][ \t]*$", lambda mo: mo.group() + "\n" + line,
                      text, count=1)
    path.write_text(text, encoding="utf-8")
    try:
        import os
        os.chmod(path, 0o600)
    except OSError:
        pass


def apply_server_groups(resp_data: dict | None, current: set[int], management: str,
                        store: ActiveGroups) -> set[int]:
    """
    Обработать heartbeat-ответ сервера: обновить набор групп (bot-режим).
    Возвращает актуальный набор.
    """
    new_ids = apply_heartbeat_directive(resp_data, management)
    if new_ids is None:
        return current
    new_set = set(new_ids)
    if new_set != current:
        store.save(new_set)
        return new_set
    return current
