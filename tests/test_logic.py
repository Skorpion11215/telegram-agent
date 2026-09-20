"""Тесты чистой клиентской логики (agent_logic)."""

import json

import pytest

from agent_logic import (
    ActiveGroups,
    MessageDedup,
    apply_heartbeat_directive,
    apply_heartbeat_management,
    apply_server_groups,
    build_ingest_body,
    find_tunnel_url,
    groups_sync_payload,
    is_forwardable_message,
    normalize_group_ids,
    normalize_management,
    read_config,
    sanitize_groups,
    should_reconfigure_requested,
    should_sync_requested,
    write_management_ini,
)


# ── management mode ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("bot", "bot"), ("BOT ", "bot"), ("site", "site"),
    ("", "site"), (None, "site"), ("garbage", "site"),
])
def test_normalize_management(raw, expected):
    assert normalize_management(raw) == expected


# ── очистка списков ──────────────────────────────────────────────────────────

def test_normalize_group_ids_dedup_and_clean():
    assert normalize_group_ids([1, "2", 1, None, "x", -100]) == [1, 2, -100]


def test_sanitize_groups_limits_and_dedup():
    groups = [{"id": 1, "title": "a"}, {"id": "2", "title": "b"},
              {"id": 1, "title": "dup"}, {"id": "bad"}, "junk"]
    out = sanitize_groups(groups)
    assert out == [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]


def test_sanitize_groups_cap_and_title_cut():
    groups = [{"id": i, "title": "т" * 500} for i in range(1500)]
    out = sanitize_groups(groups)
    assert len(out) == 1000
    assert len(out[0]["title"]) == 200


# ── payload groups-sync ──────────────────────────────────────────────────────

def test_groups_sync_payload_bot_mode_no_selection():
    payload = groups_sync_payload([{"id": 5, "title": "X"}], [5], "bot")
    assert payload["groups"] == [{"id": 5, "title": "X"}]
    assert payload["selected"] == []


def test_groups_sync_payload_site_mode_mirrors_selection():
    payload = groups_sync_payload(
        [{"id": 5, "title": "X"}, {"id": 6, "title": "Y"}], ["6", 6], "site")
    assert payload["selected"] == [6]


# ── heartbeat-директивы ──────────────────────────────────────────────────────

def test_apply_heartbeat_directive_only_bot_mode():
    assert apply_heartbeat_directive({"forward_ids": [1, 2]}, "site") is None
    assert apply_heartbeat_directive({"forward_ids": [1, 2]}, "bot") == [1, 2]
    assert apply_heartbeat_directive({}, "bot") is None


def test_apply_server_groups_persists_changes(tmp_path):
    store = ActiveGroups(tmp_path / "agent_groups.json")
    current = {1, 2}
    upd = apply_server_groups({"forward_ids": [2, 3]}, current, "bot", store)
    assert upd == {2, 3}
    assert store.load() == {2, 3}
    same = apply_server_groups({"forward_ids": [2, 3]}, upd, "bot", store)
    assert same == {2, 3}


def test_apply_server_groups_site_never_changes(tmp_path):
    store = ActiveGroups(tmp_path / "agent_groups.json")
    store.save({9})
    out = apply_server_groups({"forward_ids": [7]}, {9}, "site", store)
    assert out == {9}


def test_flags():
    assert should_sync_requested({"sync": True}) is True
    assert should_sync_requested({}) is False
    assert should_reconfigure_requested({"reconfigure": True}) is True
    assert should_reconfigure_requested(None) is False


def test_apply_heartbeat_management_authoritative_mode():
    assert apply_heartbeat_management({"management": "bot"}, "site") == "bot"
    assert apply_heartbeat_management({"management": "site"}, "bot") == "site"
    assert apply_heartbeat_management({"management": "bot"}, "bot") is None
    assert apply_heartbeat_management({}, "site") is None
    assert apply_heartbeat_management({"management": "junk"}, "site") is None


def test_write_management_ini_replaces_and_keeps(tmp_path):
    ini = tmp_path / "agent.ini"
    ini.write_text("[telegram]\nsession = x\n\n[agent]\ntoken = t\nmanagement = site\n")
    write_management_ini(ini, "bot")
    cfg = read_config(ini)
    assert cfg["management"] == "bot" and cfg["token"] == "t" and cfg["session"] == "x"
    ini.write_text("[agent]\ntoken = t2\n")
    write_management_ini(ini, "bot")
    assert read_config(ini)["management"] == "bot" and read_config(ini)["token"] == "t2"


# ── дедупликация ─────────────────────────────────────────────────────────────

def test_dedup_ttl():
    d = MessageDedup(ttl_seconds=10)
    assert d.is_duplicate(1, now=100) is False
    assert d.is_duplicate(1, now=105) is True
    assert d.is_duplicate(1, now=120) is False  # ttl истёк


# ── фильтрация сообщений ─────────────────────────────────────────────────────

class _Msg:
    def __init__(self, text=None, reply_to=None):
        self.text = text
        self.reply_to = reply_to


def test_forwardable_message():
    assert is_forwardable_message(_Msg("Краснодар - Москва 5000")) is True
    assert is_forwardable_message(_Msg("   ")) is False
    assert is_forwardable_message(_Msg(None)) is False
    assert is_forwardable_message(_Msg("ок", reply_to=1)) is False


def test_build_ingest_body():
    body = json.loads(build_ingest_body(123, 456, "текст", 789))
    assert body == {"group_id": 123, "message_id": 456, "text": "текст", "sender_id": 789}


# ── URL туннеля ──────────────────────────────────────────────────────────────

def test_find_tunnel_url():
    text = "inf Registered tunnel connection https://abc-123-def.trycloudflare.com ..."
    assert find_tunnel_url(text).endswith("trycloudflare.com")
    assert find_tunnel_url("нет ссылки") is None


# ── read_config ──────────────────────────────────────────────────────────────

def test_read_config(tmp_path):
    p = tmp_path / "agent.ini"
    p.write_text("""
[telegram]
api_id = 777
api_hash = hah
phone = +700
session = ssss

[agent]
token = t-1
api_key = k
api_secret = s
endpoint = https://example.com/
management = BOT

[groups]
ids = 1,2
""")
    cfg = read_config(p)
    assert cfg["api_id"] == "777"
    assert cfg["endpoint"] == "https://example.com"  # rstrip slash
    assert cfg["management"] == "bot"
