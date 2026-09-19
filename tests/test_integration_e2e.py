"""
E2E интеграция агент ↔ фейковый сервер бота.

Фейковый сервер воспроизводит контракты bot/ingest_server.py (проверка
HMAC с разделителями |, challenge как в db.verify_challenge_response)
и проверяет:
 - подпись агента валидна для сервера;
 - heartbeat с forward_ids (bot-режим) меняет набор групп и persist-ится;
 - on_message пересылает только активные группы и подписывает /ingest;
 - groups-sync при старте отправляет ВСЕ диалоги с selected=[] (bot-режим).
"""

import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import agent as agent_mod
from core._fallback import handle_challenge
from telethon.sessions import StringSession

import base64
import ipaddress
import struct

VALID_EMPTY_SESSION = "1" + base64.urlsafe_b64encode(struct.pack(
    ">B4sH256s", 2, ipaddress.ip_address("149.154.175.53").packed, 443, b"\x00" * 256
)).decode("ascii")
StringSession(VALID_EMPTY_SESSION)  # self-check: валидный формат

API_KEY = "test-api-key"
API_SECRET = "test-api-secret"


class FakeServer:
    def __init__(self):
        self.app = web.Application()
        self.received_ingest = []
        self.received_sync = None
        self.heartbeat_response = {"ok": True}
        self._nonce_seen = set()

    async def _validate(self, request):
        api_key = request.headers.get("X-API-Key", "")
        ts = request.headers.get("X-Timestamp", "")
        nonce = request.headers.get("X-Nonce", "")
        sig = request.headers.get("X-Signature", "")
        body = await request.read()
        if api_key != API_KEY:
            return False
        if abs(time.time() - int(ts)) > 300:
            return False
        message = f"{api_key}|{ts}|{nonce}".encode() + body
        expected = hmac.new(API_SECRET.encode(), message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False
        if nonce in self._nonce_seen:
            return False
        self._nonce_seen.add(nonce)
        return True

    async def handle_ingest(self, request):
        if not await self._validate(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        self.received_ingest.append(await request.json())
        return web.json_response({"ok": True})

    async def handle_heartbeat(self, request):
        if not await self._validate(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(self.heartbeat_response)

    async def handle_challenge(self, request):
        if not await self._validate(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        data = await request.json()
        nonce = data["challenge_nonce"]
        key_bytes = API_KEY.encode()
        result = bytearray(b ^ key_bytes[i % len(key_bytes)]
                           for i, b in enumerate(nonce.encode()))
        rotated = bytes([(b << 3 | b >> 5) & 0xFF for b in result])
        expected = hashlib.sha256(rotated).hexdigest()
        ok = data["response"] == expected
        return web.json_response({"ok": ok}, status=200 if ok else 401)

    async def handle_groups_sync(self, request):
        if not await self._validate(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        self.received_sync = await request.json()
        return web.json_response({"ok": True, "forward": [g["id"] for g in self.received_sync["groups"]]})

    def app_instance(self):
        self.app.router.add_post("/ingest", self.handle_ingest)
        self.app.router.add_post("/heartbeat", self.handle_heartbeat)
        self.app.router.add_post("/challenge/respond", self.handle_challenge)
        self.app.router.add_post("/api/groups-sync", self.handle_groups_sync)
        return self.app


class FakeDialog:
    def __init__(self, id, title, is_group=True):
        self.id = id
        self.title = title
        self.is_group = is_group
        self.is_channel = False


class FakeClient:
    def __init__(self, dialogs):
        self._dialogs = dialogs

    async def iter_dialogs(self):
        for d in self._dialogs:
            yield d


@pytest.fixture
def tmp_agent_home(tmp_path):
    home = tmp_path / "agent"
    home.mkdir()
    (home / "agent.ini").write_text(f"""
[telegram]
api_id = 1
api_hash = hash
phone = +70000000000
session = {VALID_EMPTY_SESSION}

[agent]
token = t-1
api_key = {API_KEY}
api_secret = {API_SECRET}
endpoint = http://127.0.0.1:1
management = bot

[groups]
ids =
""")
    (home / "agent_groups.json").write_text('{"groups": []}')
    return home


def _run(coro):
    return asyncio.run(coro)


async def _start_server():
    srv = FakeServer()
    server = TestServer(srv.app_instance())
    await server.start_server()
    return srv, server


def test_agent_heartbeat_applies_server_groups(tmp_agent_home):
    async def scenario():
        srv, server = await _start_server()
        try:
            agent_mod.configure(tmp_agent_home)
            agent_mod.ST.endpoint = str(server.make_url(""))
            srv.heartbeat_response = {"ok": True, "forward_ids": [111, 222]}
            await agent_mod.heartbeat_once()
            assert agent_mod.ST.active_ids == {111, 222}
            persisted = json.loads((tmp_agent_home / "agent_groups.json").read_text())
            assert persisted["groups"] == [111, 222]
        finally:
            await server.close()
    _run(scenario())


def test_agent_ingest_only_active_groups_signed(tmp_agent_home):
    async def scenario():
        srv, server = await _start_server()
        try:
            agent_mod.configure(tmp_agent_home)
            agent_mod.ST.endpoint = str(server.make_url(""))
            agent_mod.ST.active_ids = {111}

            async def event(chat_id, msg_id, text):
                return SimpleNamespace(
                    chat_id=chat_id,
                    message=SimpleNamespace(id=msg_id, text=text, reply_to=None),
                    sender_id=7,
                )

            await agent_mod.on_message(await event(111, 1, "Краснодар - Москва 5000"))
            await agent_mod.on_message(await event(999, 2, "другая группа"))
            await agent_mod.on_message(await event(111, 3, "Краснодар - Сочи 3000"))
            await agent_mod.on_message(await event(111, 3, "дубль"))
            assert [m["message_id"] for m in srv.received_ingest] == [1, 3]
            assert all(isinstance(m["group_id"], int) for m in srv.received_ingest)
        finally:
            await server.close()
    _run(scenario())


def test_agent_groups_sync_sends_all_dialogs_bot_mode(tmp_agent_home):
    async def scenario():
        srv, server = await _start_server()
        try:
            agent_mod.configure(tmp_agent_home)
            agent_mod.ST.endpoint = str(server.make_url(""))
            agent_mod.ST.client = FakeClient([
                FakeDialog(111, "Группа A"),
                FakeDialog(222, "Канал B"),
            ])
            await agent_mod.sync_dialogs()
            assert srv.received_sync is not None
            assert srv.received_sync["groups"] == [
                {"id": 111, "title": "Группа A"},
                {"id": 222, "title": "Канал B"},
            ]
            assert srv.received_sync["selected"] == []  # bot-режим: всем рулит сервер
        finally:
            await server.close()
    _run(scenario())


def test_agent_challenge_response(tmp_agent_home):
    async def scenario():
        srv, server = await _start_server()
        try:
            agent_mod.configure(tmp_agent_home)
            agent_mod.ST.endpoint = str(server.make_url(""))
            nonce = "cafe1234"
            # heartbeat_once проглотит ответ challenge/respond; проверяем прямой
            # генерацией того же вызова, который умеет сервер валидировать
            resp = await agent_mod._signed_post("/challenge/respond", {
                "challenge_nonce": nonce,
                "response": handle_challenge(nonce, API_KEY),
            })
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
        finally:
            await server.close()
    _run(scenario())


def test_site_mode_ignores_server_group_directives(tmp_agent_home):
    async def scenario():
        srv, server = await _start_server()
        try:
            agent_mod.configure(tmp_agent_home)
            (tmp_agent_home / "agent.ini").write_text(
                (tmp_agent_home / "agent.ini").read_text().replace("management = bot",
                                                                   "management = site"))
            agent_mod.configure(tmp_agent_home)
            agent_mod.ST.endpoint = str(server.make_url(""))
            agent_mod.ST.active_ids = {5}
            srv.heartbeat_response = {"ok": True, "forward_ids": [77]}
            await agent_mod.heartbeat_once()
            assert agent_mod.ST.active_ids == {5}  # site-режим не подчиняется
        finally:
            await server.close()
    _run(scenario())
