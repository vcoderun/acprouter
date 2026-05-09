from __future__ import annotations as _annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from acp.schema import (
    PermissionOption,
    RequestPermissionResponse,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
)

__all__ = (
    "ChatBinding",
    "ChatState",
    "PendingApproval",
    "ResolvedApproval",
    "SessionWorkspace",
    "SelectionCommand",
    "SelectionOption",
)

SelectionOption = SessionConfigOptionBoolean | SessionConfigOptionSelect


@dataclass(slots=True, kw_only=True)
class ChatBinding:
    active_session_id: str | None = None
    aliases: dict[str, str] = field(default_factory=dict)
    available_mode_ids: list[str] = field(default_factory=list)
    current_mode_id: str | None = None
    streaming_enabled: bool | None = None


@dataclass(slots=True, kw_only=True)
class ChatState:
    status_message_id: int | None = None
    status_text: str = ""
    response_message_id: int | None = None
    response_text: str = ""
    plan_message_id: int | None = None
    plan_task_ids: dict[str, int] = field(default_factory=dict)
    plan_task_order: list[str] = field(default_factory=list)
    plan_task_texts: dict[str, str] = field(default_factory=dict)
    next_plan_task_id: int = 1
    tool_message_ids: dict[str, int] = field(default_factory=dict)
    tool_texts: dict[str, str] = field(default_factory=dict)
    agent_text: str = ""
    prompt_in_flight: bool = False
    current_approval_id: str | None = None
    resolved_approval: ResolvedApproval | None = None
    current_model_id: str | None = None
    available_model_ids: list[str] = field(default_factory=list)
    selection_options: list[SelectionOption] = field(default_factory=list)
    dynamic_commands: dict[str, SelectionCommand] = field(default_factory=dict)
    last_response_stream_edit_monotonic: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True, kw_only=True)
class PendingApproval:
    approval_id: str
    session_id: str
    tool_call_id: str
    tool_title: str
    preview_text: str
    options: list[PermissionOption]
    future: asyncio.Future[RequestPermissionResponse]


@dataclass(slots=True, frozen=True, kw_only=True)
class ResolvedApproval:
    tool_call_id: str
    tool_title: str
    label: str
    message_id: int


@dataclass(slots=True, frozen=True, kw_only=True)
class SessionWorkspace:
    cwd: Path


@dataclass(slots=True, frozen=True, kw_only=True)
class SelectionCommand:
    acp_name: str
    telegram_name: str
    description: str
    hint: str | None = None
    source: Literal["mode", "model", "config", "command"] = "command"
