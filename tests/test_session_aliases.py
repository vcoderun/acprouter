from __future__ import annotations as _annotations

from pathlib import Path

import pytest

from acprouter.session_aliases import SessionAliasStore
from acprouter.types import ChatBinding


def test_session_alias_store_roundtrip(tmp_path):
    store = SessionAliasStore(path=tmp_path / "sessions.json")
    store.save_binding(
        "123",
        ChatBinding(
            active_session_id="session-1",
            aliases={"acpkit": "session-1"},
            available_mode_ids=["ask", "agent"],
            current_mode_id="ask",
            streaming_enabled=True,
        ),
    )

    loaded = store.load_binding("123")

    assert loaded.active_session_id == "session-1"
    assert loaded.aliases == {"acpkit": "session-1"}
    assert loaded.available_mode_ids == ["ask", "agent"]
    assert loaded.current_mode_id == "ask"
    assert loaded.streaming_enabled is True
    assert store.aliases_for_session("session-1") == ["acpkit"]


def test_session_alias_store_handles_missing_and_invalid_payloads(tmp_path: Path) -> None:
    store = SessionAliasStore(path=tmp_path / "sessions.json")

    assert store.load_binding("missing") == ChatBinding()

    store.path.write_text("{not-json", encoding="utf-8")
    assert store.load_binding("broken") == ChatBinding()


def test_session_alias_store_filters_invalid_alias_and_binding_payloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = SessionAliasStore(path=tmp_path / "sessions.json")
    payload = {
        123: [],
        "999": {"active_session_id": "other"},
        "123:7": {"active_session_id": "session-1", "aliases": {"named": "session-1"}},
        "123:8": [],
    }
    monkeypatch.setattr(SessionAliasStore, "_load", lambda self: payload)

    assert store.aliases_for_session("session-1") == ["named"]
    assert store.bindings_for_chat(123) == [
        ("123:7", ChatBinding(active_session_id="session-1", aliases={"named": "session-1"}))
    ]
