from __future__ import annotations as _annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

from .types import ChatBinding

__all__ = ("SessionAliasStore",)


class _BindingPayload(TypedDict, total=False):
    active_session_id: str
    aliases: dict[str, str]
    available_mode_ids: list[str]
    current_mode_id: str
    streaming_enabled: bool


@dataclass(slots=True, kw_only=True)
class SessionAliasStore:
    path: Path

    def load_binding(self, chat_key: str) -> ChatBinding:
        payload = self._load()
        raw = self._binding_payload(payload.get(chat_key))
        if raw is None:
            return ChatBinding()
        active = raw.get("active_session_id")
        aliases_raw = raw.get("aliases", {})
        aliases = {
            str(alias): str(session_id)
            for alias, session_id in aliases_raw.items()
            if isinstance(alias, str) and isinstance(session_id, str)
        }
        available_modes_raw = raw.get("available_mode_ids", [])
        available_mode_ids = [
            str(mode_id) for mode_id in available_modes_raw if isinstance(mode_id, str)
        ]
        current_mode_id = raw.get("current_mode_id")
        streaming_enabled = raw.get("streaming_enabled")
        return ChatBinding(
            active_session_id=active if isinstance(active, str) else None,
            aliases=aliases,
            available_mode_ids=available_mode_ids,
            current_mode_id=current_mode_id if isinstance(current_mode_id, str) else None,
            streaming_enabled=(streaming_enabled if isinstance(streaming_enabled, bool) else None),
        )

    def save_binding(self, chat_key: str, binding: ChatBinding) -> None:
        payload = self._load()
        payload[chat_key] = {
            "active_session_id": binding.active_session_id,
            "aliases": dict(sorted(binding.aliases.items())),
            "available_mode_ids": list(binding.available_mode_ids),
            "current_mode_id": binding.current_mode_id,
            "streaming_enabled": binding.streaming_enabled,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def aliases_for_session(self, session_id: str) -> list[str]:
        payload = self._load()
        aliases: list[str] = []
        for raw in payload.values():
            binding_payload = self._binding_payload(raw)
            if binding_payload is None:
                continue
            aliases_raw = binding_payload.get("aliases", {})
            for alias, mapped in aliases_raw.items():
                if mapped == session_id and isinstance(alias, str):
                    aliases.append(alias)
        return sorted(set(aliases))

    def bindings_for_chat(self, chat_id: int) -> list[tuple[str, ChatBinding]]:
        payload = self._load()
        prefix = f"{chat_id}:"
        bindings: list[tuple[str, ChatBinding]] = []
        for chat_key, raw in payload.items():
            if not isinstance(chat_key, str):
                continue
            if chat_key != str(chat_id) and not chat_key.startswith(prefix):
                continue
            binding_payload = self._binding_payload(raw)
            if binding_payload is None:
                continue
            bindings.append((chat_key, self.load_binding(chat_key)))
        return sorted(bindings, key=lambda item: item[0])

    def _load(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return raw if isinstance(raw, dict) else {}

    def _binding_payload(self, raw: object) -> _BindingPayload | None:
        if not isinstance(raw, Mapping):
            return None
        return cast(_BindingPayload, dict(raw))
