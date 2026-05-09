from __future__ import annotations as _annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import TypeAlias, cast
from unittest.mock import AsyncMock

import pytest
from acp import plan_entry, start_tool_call, tool_diff_content, update_plan
from acp.exceptions import RequestError
from acp.interfaces import Agent as AcpAgent
from acp.schema import (
    AgentMessageChunk,
    AudioContentBlock,
    AvailableCommandsUpdate,
    BlobResourceContents,
    ConfigOptionUpdate,
    CurrentModeUpdate,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    RequestPermissionResponse,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SetSessionConfigOptionResponse,
    SetSessionModelResponse,
    SetSessionModeResponse,
    TextContentBlock,
    ToolCallUpdate,
)
from pyrogram.client import Client
from pyrogram.types import CallbackQuery, Message

from acprouter.settings import AppSettings
from acprouter.telegram_gateway import TelegramGateway
from acprouter.types import ChatBinding, PendingApproval, ResolvedApproval, SelectionCommand

_PromptSideEffect: TypeAlias = Callable[[str, list[object]], Awaitable[None]]
_PromptBlock: TypeAlias = (
    TextContentBlock | ImageContentBlock | AudioContentBlock | EmbeddedResourceContentBlock
)


class _FakeApp:
    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs
        self.message_handlers: list[object] = []
        self.callback_handlers: list[object] = []
        self.started = False
        self.stopped = False
        self.commands: list[object] | None = None
        self.sent_messages: list[tuple[int, str]] = []
        self.sent_message_kwargs: list[dict[str, object]] = []
        self.sent_checklists: list[tuple[int, str, list[tuple[int, str]]]] = []
        self.sent_checklist_kwargs: list[dict[str, object]] = []
        self.added_checklist_tasks: list[tuple[int, int, list[tuple[int, str]]]] = []
        self.edited_checklists: list[tuple[int, int, str, list[tuple[int, str]]]] = []
        self.edited_checklist_kwargs: list[dict[str, object]] = []
        self.marked_checklists: list[tuple[int, int, list[int], list[int]]] = []
        self.created_forum_topics: list[tuple[int, str]] = []
        self.sent_documents: list[tuple[int, str, str]] = []
        self.sent_document_kwargs: list[dict[str, object]] = []
        self.edited_messages: list[tuple[int, int, str]] = []
        self.chat_actions: list[tuple[int, object]] = []

    def on_message(self, _filter: object):
        def register(handler: object) -> object:
            self.message_handlers.append(handler)
            return handler

        return register

    def on_callback_query(self):
        def register(handler: object) -> object:
            self.callback_handlers.append(handler)
            return handler

        return register

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def set_bot_commands(self, commands: list[object]) -> None:
        self.commands = commands

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent_messages.append((chat_id, text))
        self.sent_message_kwargs.append(dict(kwargs))

        @dataclass
        class _Message:
            id: int = 1

        return _Message()

    async def send_document(
        self, chat_id: int, document: object, caption: str | None = None, **kwargs
    ):
        read = getattr(document, "read", None)
        raw = read() if callable(read) else b""
        seek = getattr(document, "seek", None)
        if callable(seek):
            seek(0)
        decoded = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else ""
        self.sent_document_kwargs.append(dict(kwargs))
        self.sent_documents.append(
            (
                chat_id,
                decoded,
                caption or "",
            )
        )

        @dataclass
        class _Message:
            id: int = 1

        return _Message()

    async def send_checklist(self, chat_id: int, checklist: object, **kwargs):
        title = getattr(checklist, "title", "")
        tasks = [
            (int(getattr(task, "id", 0)), str(getattr(task, "text", "")))
            for task in getattr(checklist, "tasks", [])
        ]
        self.sent_checklists.append((chat_id, str(title), tasks))
        self.sent_checklist_kwargs.append(dict(kwargs))

        @dataclass
        class _Message:
            id: int = 1

        return _Message()

    async def edit_message_checklist(
        self, chat_id: int, message_id: int, checklist: object, **kwargs
    ):
        title = getattr(checklist, "title", "")
        tasks = [
            (int(getattr(task, "id", 0)), str(getattr(task, "text", "")))
            for task in getattr(checklist, "tasks", [])
        ]
        self.edited_checklists.append((chat_id, message_id, str(title), tasks))
        self.edited_checklist_kwargs.append(dict(kwargs))

        @dataclass
        class _Message:
            id: int = message_id

        return _Message()

    async def add_checklist_tasks(self, chat_id: int, message_id: int, tasks: list[object]) -> int:
        serialized = [
            (int(getattr(task, "id", 0)), str(getattr(task, "text", ""))) for task in tasks
        ]
        self.added_checklist_tasks.append((chat_id, message_id, serialized))
        return len(serialized)

    async def mark_checklist_tasks_as_done(
        self,
        chat_id: int,
        message_id: int,
        *,
        marked_as_done_task_ids: list[int] | None = None,
        marked_as_not_done_task_ids: list[int] | None = None,
    ) -> int:
        self.marked_checklists.append(
            (
                chat_id,
                message_id,
                list(marked_as_done_task_ids or []),
                list(marked_as_not_done_task_ids or []),
            )
        )
        return len(marked_as_done_task_ids or []) + len(marked_as_not_done_task_ids or [])

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs) -> None:
        del kwargs
        self.edited_messages.append((chat_id, message_id, text))

    async def send_chat_action(self, chat_id: int, action: object) -> None:
        self.chat_actions.append((chat_id, action))

    async def create_forum_topic(self, chat_id: int, title: str):
        self.created_forum_topics.append((chat_id, title))

        @dataclass
        class _Topic:
            id: int = 77
            title: str = ""

        return _Topic(title=title)

    async def download_media(self, media: object, *, in_memory: bool = False):
        assert in_memory is True
        payload_source = media
        if isinstance(media, _FakeMessage):
            media_name = TelegramGateway._prompt_media_name(_message(media))
            if media_name is None:
                raise AssertionError("message did not expose prompt media")
            payload_source = getattr(media, media_name)
        payload = cast(_FakeMedia, payload_source)
        file_obj = BytesIO(payload.data)
        file_obj.name = payload.file_name
        return file_obj


@dataclass
class _FakeAgent:
    initialized_protocols: list[int]
    connected_clients: list[object]

    async def initialize(self, protocol_version: int):
        self.initialized_protocols.append(protocol_version)
        return None

    def on_connect(self, client: object) -> None:
        self.connected_clients.append(client)


@dataclass
class _FakeChat:
    id: int
    type: object = "supergroup"
    is_forum: bool = True


@dataclass
class _FakeChatType:
    value: str | None = None
    name: str | None = None


@dataclass
class _FakeMedia:
    file_name: str
    data: bytes
    mime_type: str | None = None


@dataclass
class _FakeMessageMediaType:
    value: str | None = None
    name: str | None = None


@dataclass
class _FakeMessage:
    text: str | None
    chat: _FakeChat | None
    caption: str | None = None
    id: int = 1
    message_thread_id: int | None = None
    media: object | None = None
    reply_to_message: object | None = None
    photo: object | None = None
    voice: object | None = None
    audio: object | None = None
    document: object | None = None
    video: object | None = None
    video_note: object | None = None
    animation: object | None = None
    sticker: object | None = None
    replies: list[str] = field(default_factory=list)

    async def reply(self, text: str) -> None:
        self.replies.append(text)


_ONE_PIXEL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\xdac`\xf8\xcfP"
    b"\x0f\x00\x03\x86\x01\x80Z4}k\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass
class _FakeCallbackQuery:
    data: str
    message: _FakeMessage | None
    answers: list[tuple[str, bool]] = field(default_factory=list)
    edited_texts: list[str] = field(default_factory=list)

    async def answer(self, text: str = "", *, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text: str, **kwargs) -> None:
        del kwargs
        self.edited_texts.append(text)


@dataclass
class _FakeConn:
    prompt_response: PromptResponse
    new_session_response: NewSessionResponse
    load_session_response: LoadSessionResponse
    list_sessions_response: ListSessionsResponse
    set_session_mode_response: SetSessionModeResponse
    set_session_model_response: SetSessionModelResponse = field(
        default_factory=lambda: SetSessionModelResponse.model_validate({})
    )
    set_config_option_response: SetSessionConfigOptionResponse | None = field(default=None)
    prompt_error: Exception | None = None
    load_session_result: LoadSessionResponse | None = None
    prompt_calls: list[tuple[str, list[object]]] = field(default_factory=list)
    cancelled_sessions: list[str] = field(default_factory=list)
    set_mode_calls: list[tuple[str, str]] = field(default_factory=list)
    set_model_calls: list[tuple[str, str]] = field(default_factory=list)
    set_config_calls: list[tuple[str, str, str | bool]] = field(default_factory=list)
    new_session_calls: list[tuple[str, list[object]]] = field(default_factory=list)
    load_session_calls: list[tuple[str, str, list[object]]] = field(default_factory=list)
    list_session_calls: list[str] = field(default_factory=list)
    prompt_side_effect: _PromptSideEffect | None = None

    def __post_init__(self) -> None:
        if self.load_session_result is None:
            self.load_session_result = self.load_session_response

    async def prompt(self, *, prompt: list[object], session_id: str) -> PromptResponse:
        self.prompt_calls.append((session_id, prompt))
        if self.prompt_side_effect is not None:
            await self.prompt_side_effect(session_id, prompt)
        if self.prompt_error is not None:
            raise self.prompt_error
        return self.prompt_response

    async def cancel(self, session_id: str) -> None:
        self.cancelled_sessions.append(session_id)

    async def new_session(self, *, cwd: str, mcp_servers: list[object]) -> NewSessionResponse:
        self.new_session_calls.append((cwd, mcp_servers))
        return self.new_session_response

    async def load_session(
        self,
        *,
        cwd: str,
        session_id: str,
        mcp_servers: list[object],
    ) -> LoadSessionResponse | None:
        self.load_session_calls.append((cwd, session_id, mcp_servers))
        return self.load_session_result

    async def list_sessions(self, *, cwd: str) -> ListSessionsResponse:
        self.list_session_calls.append(cwd)
        return self.list_sessions_response

    async def set_session_mode(self, mode_id: str, session_id: str) -> SetSessionModeResponse:
        self.set_mode_calls.append((mode_id, session_id))
        return self.set_session_mode_response

    async def set_session_model(self, model_id: str, session_id: str) -> SetSessionModelResponse:
        self.set_model_calls.append((model_id, session_id))
        return self.set_session_model_response

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
    ) -> SetSessionConfigOptionResponse | None:
        self.set_config_calls.append((config_id, session_id, value))
        return self.set_config_option_response


def _settings(
    tmp_path: Path,
    *,
    enable_host_tools: bool = True,
    telegram_business_connection_id: str | None = None,
) -> AppSettings:
    return AppSettings(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        telegram_session_name="acprouter-bot",
        telegram_business_connection_id=telegram_business_connection_id,
        acp_command=("python", "agent.py"),
        workspace_root=tmp_path,
        state_dir=tmp_path / ".acprouter-state",
        acp_cwd=tmp_path,
        acp_stdio_buffer_limit_bytes=1024,
        enable_host_tools=enable_host_tools,
        streaming_default=False,
        streaming_edit_interval_seconds=1.0,
    )


def _new_session_response(session_id: str = "session-1") -> NewSessionResponse:
    return NewSessionResponse.model_validate(
        {
            "sessionId": session_id,
            "modes": {
                "availableModes": [
                    {"id": "ask", "name": "Ask"},
                    {"id": "agent", "name": "Agent"},
                ],
                "currentModeId": "ask",
            },
        }
    )


def _load_session_response() -> LoadSessionResponse:
    return LoadSessionResponse.model_validate(
        {
            "modes": {
                "availableModes": [
                    {"id": "ask", "name": "Ask"},
                    {"id": "agent", "name": "Agent"},
                ],
                "currentModeId": "ask",
            },
        }
    )


def _list_sessions_response(tmp_path: Path) -> ListSessionsResponse:
    return ListSessionsResponse.model_validate(
        {
            "sessions": [
                {
                    "cwd": str(tmp_path),
                    "sessionId": "session-1",
                    "title": "Primary session",
                }
            ]
        }
    )


def _prompt_response(stop_reason: str = "end_turn") -> PromptResponse:
    return PromptResponse.model_validate({"stopReason": stop_reason})


def _set_mode_response() -> SetSessionModeResponse:
    return SetSessionModeResponse.model_validate({})


def _set_config_response() -> SetSessionConfigOptionResponse:
    return SetSessionConfigOptionResponse.model_validate(
        {
            "configOptions": [
                {
                    "id": "thinking",
                    "name": "Thinking",
                    "type": "select",
                    "currentValue": "high",
                    "options": [
                        {"name": "Medium", "value": "medium"},
                        {"name": "High", "value": "high"},
                    ],
                }
            ]
        }
    )


def _client(value: _FakeApp) -> Client:
    return cast(Client, value)


def _message(value: _FakeMessage) -> Message:
    return cast(Message, value)


def _callback_query(value: _FakeCallbackQuery) -> CallbackQuery:
    return cast(CallbackQuery, value)


def _agent(value: _FakeConn) -> AcpAgent:
    return cast(AcpAgent, value)


def _permission_option(*, kind: str, name: str, option_id: str) -> PermissionOption:
    return PermissionOption.model_validate(
        {
            "kind": kind,
            "name": name,
            "optionId": option_id,
        }
    )


def test_approval_keyboard_rows_use_canonical_two_column_layout():
    options = [
        _permission_option(kind="reject_always", name="Deny Always", option_id="4"),
        _permission_option(kind="allow_always", name="Allow Always", option_id="2"),
        _permission_option(kind="reject_once", name="Deny Once", option_id="3"),
        _permission_option(kind="allow_once", name="Allow Once", option_id="1"),
    ]

    rows = TelegramGateway._approval_keyboard_rows("approval-1", options)

    assert [[button.text for button in row] for row in rows] == [
        ["Allow Once ✅", "Allow Always ✅"],
        ["Deny Once ❌", "Deny Always ❌"],
    ]


def test_approval_keyboard_rows_keep_last_odd_option_on_own_row():
    options = [
        _permission_option(kind="allow_once", name="Allow Once", option_id="1"),
        _permission_option(kind="allow_always", name="Allow Always", option_id="2"),
        _permission_option(kind="reject_once", name="Deny Once", option_id="3"),
    ]

    rows = TelegramGateway._approval_keyboard_rows("approval-2", options)

    assert [[button.text for button in row] for row in rows] == [
        ["Allow Once ✅", "Allow Always ✅"],
        ["Deny Once ❌"],
    ]


def test_from_settings_builds_gateway_components(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)

    gateway = TelegramGateway.from_settings(_settings(tmp_path))

    assert gateway.app is fake_app
    assert gateway.alias_store.path == tmp_path / ".acprouter-state" / "sessions.json"
    assert gateway.workspace_manager is not None
    assert gateway.terminal_manager is not None
    assert gateway.conn is None


def test_from_acp_agent_connects_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    fake_app = _FakeApp()
    agent = _FakeAgent(initialized_protocols=[], connected_clients=[])
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)

    gateway = TelegramGateway.from_acp_agent(cast(AcpAgent, agent), _settings(tmp_path))

    assert gateway.conn is agent
    assert agent.connected_clients == [gateway]


@pytest.mark.asyncio
async def test_run_starts_app_and_registers_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    agent = _FakeAgent(initialized_protocols=[], connected_clients=[])
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)

    async def _fake_idle() -> None:
        return None

    monkeypatch.setattr("acprouter.telegram_gateway.idle", _fake_idle)
    gateway = TelegramGateway.from_acp_agent(cast(AcpAgent, agent), _settings(tmp_path))

    await gateway.run()

    assert fake_app.started is True
    assert fake_app.stopped is True
    assert fake_app.commands == gateway._bot_commands()
    assert len(fake_app.message_handlers) == 1
    assert len(fake_app.callback_handlers) == 1
    assert agent.initialized_protocols


@pytest.mark.asyncio
async def test_session_update_projects_completed_tool_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"

    update = start_tool_call(
        "tool-1",
        "write file",
        status="completed",
        content=[tool_diff_content("README.md", "# new", "# old")],
    )

    await gateway.session_update("session-1", update)

    assert fake_app.sent_messages
    assert "Tool update" in fake_app.sent_messages[0][1]
    assert "--- a/README.md" in fake_app.sent_messages[0][1]
    assert '<pre language="diff">' in fake_app.sent_messages[0][1]


@pytest.mark.asyncio
async def test_session_update_edits_existing_tool_card_for_inflight_updates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"

    await gateway.session_update(
        "session-1",
        ToolCallUpdate.model_validate(
            {
                "toolCallId": "tool-1",
                "title": "run command",
                "status": "in_progress",
                "rawInput": {"command": "python", "args": ["-V"]},
            }
        ),
    )
    await gateway.session_update(
        "session-1",
        ToolCallUpdate.model_validate(
            {
                "toolCallId": "tool-1",
                "title": "run command",
                "status": "completed",
                "rawInput": {"command": "python", "args": ["-V"]},
                "rawOutput": {"returncode": 0, "stdout": "Python 3.12.0"},
            }
        ),
    )

    assert len(fake_app.sent_messages) == 1
    assert fake_app.sent_messages[0][0] == 123
    assert '<pre language="bash">' in fake_app.sent_messages[0][1]
    assert "python -V" in fake_app.sent_messages[0][1]
    assert len(fake_app.edited_messages) == 1
    assert fake_app.edited_messages[0][0] == 123
    assert "Python 3.12.0" in fake_app.edited_messages[0][2]


@pytest.mark.asyncio
async def test_session_update_projects_plan_updates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"

    await gateway.session_update(
        "session-1",
        update_plan([plan_entry("first", status="pending")]),
    )

    assert fake_app.sent_messages
    assert "<b>Current plan</b>" in fake_app.sent_messages[0][1]
    assert "<pre>" in fake_app.sent_messages[0][1]
    assert fake_app.sent_checklists == []
    assert fake_app.marked_checklists == []


@pytest.mark.asyncio
async def test_session_update_edits_existing_checklist_and_marks_completed_tasks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(
        _settings(tmp_path, telegram_business_connection_id="biz-123")
    )
    gateway.session_to_chat["session-1"] = "123"

    await gateway.session_update(
        "session-1",
        update_plan(
            [
                plan_entry("first", status="pending"),
                plan_entry("second", status="in_progress"),
            ]
        ),
    )
    await gateway.session_update(
        "session-1",
        update_plan(
            [
                plan_entry("first", status="completed"),
                plan_entry("second", status="pending"),
                plan_entry("third", status="pending"),
            ]
        ),
    )

    assert fake_app.sent_checklists == [
        (123, "Current plan", [(1, "first"), (2, "[in progress] second")])
    ]
    assert fake_app.sent_checklist_kwargs == [
        {"business_connection_id": "biz-123", "message_thread_id": None}
    ]
    assert fake_app.edited_checklists == [
        (123, 1, "Current plan", [(1, "first"), (2, "second"), (3, "third")])
    ]
    assert fake_app.edited_checklist_kwargs == [{"business_connection_id": "biz-123"}]
    assert fake_app.marked_checklists == [
        (123, 1, [], [1, 2]),
        (123, 1, [1], [2, 3]),
    ]


@pytest.mark.asyncio
async def test_session_update_appends_new_checklist_tasks_without_full_edit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(
        _settings(tmp_path, telegram_business_connection_id="biz-123")
    )
    gateway.session_to_chat["session-1"] = "123"

    await gateway.session_update(
        "session-1",
        update_plan([plan_entry("first", status="pending")]),
    )
    await gateway.session_update(
        "session-1",
        update_plan(
            [
                plan_entry("first", status="pending"),
                plan_entry("second", status="pending"),
                plan_entry("third", status="completed"),
            ]
        ),
    )

    assert fake_app.sent_checklists == [(123, "Current plan", [(1, "first")])]
    assert fake_app.sent_checklist_kwargs == [
        {"business_connection_id": "biz-123", "message_thread_id": None}
    ]
    assert fake_app.added_checklist_tasks == [(123, 1, [(2, "second"), (3, "third")])]
    assert fake_app.edited_checklists == []
    assert fake_app.marked_checklists == [
        (123, 1, [], [1]),
        (123, 1, [3], [1, 2]),
    ]


@pytest.mark.asyncio
async def test_session_update_streams_agent_message_chunks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"

    chunk = AgentMessageChunk.model_validate(
        {
            "sessionId": "session-1",
            "messageId": "msg-1",
            "rawContent": {"type": "text", "text": "hello"},
            "content": {"type": "text", "text": "hello"},
            "sessionUpdate": "agent_message_chunk",
        }
    )

    await gateway.session_update("session-1", chunk)

    assert fake_app.sent_messages == []
    assert gateway._state("123").agent_text == "hello"


@pytest.mark.asyncio
async def test_session_update_streams_agent_message_chunks_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"
    gateway.alias_store.save_binding(
        "123", ChatBinding(active_session_id="session-1", streaming_enabled=True)
    )

    chunk = AgentMessageChunk.model_validate(
        {
            "sessionId": "session-1",
            "messageId": "msg-1",
            "rawContent": {"type": "text", "text": "hello"},
            "content": {"type": "text", "text": "hello"},
            "sessionUpdate": "agent_message_chunk",
        }
    )

    await gateway.session_update("session-1", chunk)

    assert fake_app.sent_messages == [(123, "hello")]


@pytest.mark.asyncio
async def test_session_update_throttles_streaming_edits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monotonic_values = iter([10.0, 10.1, 11.2])
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    monkeypatch.setattr(
        "acprouter.telegram_gateway.time.monotonic",
        lambda: next(monotonic_values, 11.2),
    )
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"
    gateway.alias_store.save_binding(
        "123", ChatBinding(active_session_id="session-1", streaming_enabled=True)
    )

    chunk_one = AgentMessageChunk.model_validate(
        {
            "sessionId": "session-1",
            "messageId": "msg-1",
            "rawContent": {"type": "text", "text": "hello"},
            "content": {"type": "text", "text": "hello"},
            "sessionUpdate": "agent_message_chunk",
        }
    )
    chunk_two = AgentMessageChunk.model_validate(
        {
            "sessionId": "session-1",
            "messageId": "msg-1",
            "rawContent": {"type": "text", "text": " world"},
            "content": {"type": "text", "text": " world"},
            "sessionUpdate": "agent_message_chunk",
        }
    )
    chunk_three = AgentMessageChunk.model_validate(
        {
            "sessionId": "session-1",
            "messageId": "msg-1",
            "rawContent": {"type": "text", "text": "!"},
            "content": {"type": "text", "text": "!"},
            "sessionUpdate": "agent_message_chunk",
        }
    )

    await gateway.session_update("session-1", chunk_one)
    await gateway.session_update("session-1", chunk_two)
    await gateway.session_update("session-1", chunk_three)

    assert fake_app.sent_messages == [(123, "hello")]
    assert (123, 1, "hello world!") in fake_app.edited_messages


@pytest.mark.asyncio
async def test_handle_message_sends_final_reply_once_when_streaming_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))

    async def _emit_chunks(session_id: str, prompt: list[object]) -> None:
        del prompt
        await gateway.session_update(
            session_id,
            AgentMessageChunk.model_validate(
                {
                    "sessionId": session_id,
                    "messageId": "msg-1",
                    "rawContent": {"type": "text", "text": "hello"},
                    "content": {"type": "text", "text": "hello"},
                    "sessionUpdate": "agent_message_chunk",
                }
            ),
        )
        await gateway.session_update(
            session_id,
            AgentMessageChunk.model_validate(
                {
                    "sessionId": session_id,
                    "messageId": "msg-1",
                    "rawContent": {"type": "text", "text": " world"},
                    "content": {"type": "text", "text": " world"},
                    "sessionUpdate": "agent_message_chunk",
                }
            ),
        )

    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
        prompt_side_effect=_emit_chunks,
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    message = _FakeMessage(text="hello", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(message))

    assert fake_app.sent_messages.count((123, "hello world")) == 1
    assert (123, 1, "hello") not in fake_app.edited_messages


@pytest.mark.asyncio
async def test_client_host_tools_are_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path, enable_host_tools=False))
    gateway.session_to_chat["session-1"] = "123"

    with pytest.raises(RequestError, match="Client-owned host tools are disabled"):
        await gateway.read_text_file(path="README.md", session_id="session-1")


def test_from_settings_can_enable_client_host_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)

    gateway = TelegramGateway.from_settings(_settings(tmp_path, enable_host_tools=True))

    assert gateway.workspace_manager is not None
    assert gateway.terminal_manager is not None


@pytest.mark.asyncio
async def test_handle_message_requires_session_before_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.conn = _agent(
        _FakeConn(
            prompt_response=_prompt_response(),
            new_session_response=_new_session_response(),
            load_session_response=_load_session_response(),
            list_sessions_response=_list_sessions_response(tmp_path),
            set_session_mode_response=_set_mode_response(),
        )
    )

    message = _FakeMessage(text="hello", chat=_FakeChat(id=123))

    await gateway.handle_message(_client(fake_app), _message(message))

    assert message.replies == ["Create a session first with /new or /new <name>."]


@pytest.mark.asyncio
async def test_handle_message_runs_prompt_for_active_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    message = _FakeMessage(text="hello", chat=_FakeChat(id=123))

    await gateway.handle_message(_client(fake_app), _message(message))

    assert conn.prompt_calls
    assert conn.prompt_calls[0][0] == "session-1"
    assert fake_app.chat_actions
    assert fake_app.sent_messages[0] == (123, "Running...")
    assert fake_app.edited_messages[-1] == (123, 1, "Completed.")


@pytest.mark.asyncio
async def test_handle_message_sends_photo_prompt_block_when_image_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.prompt_capabilities = PromptCapabilities.model_validate({"image": True})
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    message = _FakeMessage(
        text=None,
        caption="review this image",
        chat=_FakeChat(id=123),
        photo=_FakeMedia(file_name="diagram.png", data=b"png-bytes"),
    )

    await gateway.handle_message(_client(fake_app), _message(message))

    prompt = cast(list[_PromptBlock], conn.prompt_calls[0][1])
    assert len(prompt) == 2
    assert prompt[0].type == "text"
    assert prompt[0].text == "review this image"
    assert prompt[1].type == "image"
    assert prompt[1].mime_type == "image/png"


@pytest.mark.asyncio
async def test_handle_message_sniffs_photo_png_mime_when_metadata_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.prompt_capabilities = PromptCapabilities.model_validate({"image": True})
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    message = _FakeMessage(
        text="explain image",
        chat=_FakeChat(id=123),
        photo=_FakeMedia(file_name="media", data=_ONE_PIXEL_PNG, mime_type=None),
    )

    await gateway.handle_message(_client(fake_app), _message(message))

    prompt = cast(list[_PromptBlock], conn.prompt_calls[0][1])
    assert len(prompt) == 2
    assert prompt[1].type == "image"
    assert prompt[1].mime_type == "image/png"


@pytest.mark.asyncio
async def test_handle_message_uses_message_media_value_for_photo_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.prompt_capabilities = PromptCapabilities.model_validate(
        {"image": True, "embeddedContext": True}
    )
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    message = _FakeMessage(
        text=None,
        caption="review this media",
        chat=_FakeChat(id=123),
        media=_FakeMessageMediaType(value="photo"),
        photo=_FakeMedia(file_name="diagram.png", data=b"png-bytes"),
        document=_FakeMedia(file_name="notes.md", data=b"# note\nhello"),
    )

    await gateway.handle_message(_client(fake_app), _message(message))

    prompt = cast(list[_PromptBlock], conn.prompt_calls[0][1])
    assert len(prompt) == 2
    assert prompt[0].type == "text"
    assert prompt[1].type == "image"
    assert prompt[1].mime_type == "image/png"


@pytest.mark.asyncio
async def test_handle_message_uses_message_media_name_for_document_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.prompt_capabilities = PromptCapabilities.model_validate({"embeddedContext": True})
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    message = _FakeMessage(
        text="summarize",
        chat=_FakeChat(id=123),
        id=44,
        media=_FakeMessageMediaType(name="DOCUMENT"),
        document=_FakeMedia(file_name="notes.md", data=b"# note\nhello"),
    )

    await gateway.handle_message(_client(fake_app), _message(message))

    prompt = cast(list[_PromptBlock], conn.prompt_calls[0][1])
    assert len(prompt) == 2
    assert prompt[0].type == "text"
    assert prompt[1].type == "resource"
    assert prompt[1].resource.uri == "telegram://chat/123/message/44/notes.md"
    assert prompt[1].resource.text == "# note\nhello"


@pytest.mark.asyncio
async def test_handle_message_treats_image_document_as_image_when_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.prompt_capabilities = PromptCapabilities.model_validate({"image": True})
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    message = _FakeMessage(
        text="explain attachment",
        chat=_FakeChat(id=123),
        id=49,
        media=_FakeMessageMediaType(value="document"),
        document=_FakeMedia(file_name="image.bin", data=_ONE_PIXEL_PNG, mime_type=None),
    )

    await gateway.handle_message(_client(fake_app), _message(message))

    prompt = cast(list[_PromptBlock], conn.prompt_calls[0][1])
    assert len(prompt) == 2
    assert prompt[0].type == "text"
    assert prompt[1].type == "image"
    assert prompt[1].mime_type == "image/png"


def test_normalize_image_bytes_uses_sips_fallback_when_pillow_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    def _raise_import_error(name: str):
        raise ImportError(name)

    monkeypatch.setattr("acprouter.telegram_gateway.importlib.import_module", _raise_import_error)
    monkeypatch.setattr("acprouter.telegram_gateway.sys.platform", "darwin")
    monkeypatch.setattr(
        TelegramGateway,
        "_normalize_image_bytes_with_sips",
        staticmethod(lambda data, mime: (b"png-data", "image/png")),
    )

    result = TelegramGateway._normalize_image_bytes(b"raw-image", "image/webp")

    assert result == (b"png-data", "image/png")


@pytest.mark.asyncio
async def test_handle_message_includes_replied_photo_and_caption_when_image_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.prompt_capabilities = PromptCapabilities.model_validate({"image": True})
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    replied_image = _FakeMessage(
        text=None,
        caption="system architecture",
        chat=_FakeChat(id=123),
        photo=_FakeMedia(file_name="diagram.png", data=b"png-bytes"),
    )
    message = _FakeMessage(
        text="explain image",
        chat=_FakeChat(id=123),
        reply_to_message=replied_image,
    )

    await gateway.handle_message(_client(fake_app), _message(message))

    prompt = cast(list[_PromptBlock], conn.prompt_calls[0][1])
    assert len(prompt) == 3
    assert prompt[0].type == "text"
    assert prompt[0].text == "explain image"
    assert prompt[1].type == "text"
    assert prompt[1].text == "system architecture"
    assert prompt[2].type == "image"
    assert prompt[2].mime_type == "image/png"


@pytest.mark.asyncio
async def test_handle_message_rejects_photo_when_image_prompt_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    message = _FakeMessage(
        text=None,
        chat=_FakeChat(id=123),
        photo=_FakeMedia(file_name="diagram.png", data=b"png-bytes"),
    )

    await gateway.handle_message(_client(fake_app), _message(message))

    assert conn.prompt_calls == []
    assert fake_app.sent_messages[0] == (
        123,
        "Runtime error: Image prompts are unsupported.\nreason=This ACP agent did not advertise image prompt support.",
    )


@pytest.mark.asyncio
async def test_handle_message_sends_audio_prompt_block_when_audio_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.prompt_capabilities = PromptCapabilities.model_validate({"audio": True})
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    message = _FakeMessage(
        text="transcribe",
        chat=_FakeChat(id=123),
        voice=_FakeMedia(file_name="note.ogg", data=b"ogg-bytes"),
    )

    await gateway.handle_message(_client(fake_app), _message(message))

    prompt = cast(list[_PromptBlock], conn.prompt_calls[0][1])
    assert len(prompt) == 2
    assert prompt[1].type == "audio"
    assert prompt[1].mime_type == "audio/ogg"


@pytest.mark.asyncio
async def test_handle_message_includes_replied_document_and_caption_when_context_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.prompt_capabilities = PromptCapabilities.model_validate({"embeddedContext": True})
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    replied_document = _FakeMessage(
        text=None,
        caption="project notes",
        chat=_FakeChat(id=123),
        id=44,
        document=_FakeMedia(file_name="notes.md", data=b"# note\nhello"),
    )
    message = _FakeMessage(
        text="summarize this",
        chat=_FakeChat(id=123),
        reply_to_message=replied_document,
    )

    await gateway.handle_message(_client(fake_app), _message(message))

    prompt = cast(list[_PromptBlock], conn.prompt_calls[0][1])
    assert len(prompt) == 3
    assert prompt[0].type == "text"
    assert prompt[0].text == "summarize this"
    assert prompt[1].type == "text"
    assert prompt[1].text == "project notes"
    assert prompt[2].type == "resource"
    assert prompt[2].resource.uri == "telegram://chat/123/message/44/notes.md"
    assert prompt[2].resource.text == "# note\nhello"


@pytest.mark.asyncio
async def test_handle_message_embeds_text_document_when_context_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.prompt_capabilities = PromptCapabilities.model_validate({"embeddedContext": True})
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    message = _FakeMessage(
        text=None,
        caption="use this file",
        chat=_FakeChat(id=123),
        id=44,
        document=_FakeMedia(file_name="notes.md", data=b"# note\nhello"),
    )

    await gateway.handle_message(_client(fake_app), _message(message))

    prompt = cast(list[_PromptBlock], conn.prompt_calls[0][1])
    assert len(prompt) == 2
    assert prompt[0].type == "text"
    assert prompt[1].type == "resource"
    assert prompt[1].resource.uri == "telegram://chat/123/message/44/notes.md"
    assert prompt[1].resource.text == "# note\nhello"


@pytest.mark.asyncio
async def test_handle_message_embeds_binary_document_blob_when_context_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.prompt_capabilities = PromptCapabilities.model_validate({"embeddedContext": True})
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    message = _FakeMessage(
        text="summarize this pdf",
        chat=_FakeChat(id=123),
        id=45,
        document=_FakeMedia(file_name="notes.pdf", data=b"%PDF-\xff\x00"),
    )

    await gateway.handle_message(_client(fake_app), _message(message))

    prompt = cast(list[_PromptBlock], conn.prompt_calls[0][1])
    assert len(prompt) == 2
    assert prompt[0].type == "text"
    assert prompt[1].type == "resource"
    assert isinstance(prompt[1].resource, BlobResourceContents)
    assert prompt[1].resource.uri == "telegram://chat/123/message/45/notes.pdf"
    assert prompt[1].resource.mime_type == "application/pdf"


@pytest.mark.asyncio
async def test_handle_message_embeds_photo_without_caption_when_image_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.prompt_capabilities = PromptCapabilities.model_validate({"image": True})
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    message = _FakeMessage(
        text=None,
        caption=None,
        chat=_FakeChat(id=123),
        id=46,
        photo=_FakeMedia(file_name="photo.jpg", data=b"\xff\xd8\xff", mime_type="image/jpeg"),
    )

    await gateway.handle_message(_client(fake_app), _message(message))

    prompt = cast(list[_PromptBlock], conn.prompt_calls[0][1])
    assert len(prompt) == 1
    assert prompt[0].type == "image"
    assert prompt[0].mime_type == "image/jpeg"


@pytest.mark.asyncio
async def test_handle_message_embeds_video_blob_when_context_is_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.prompt_capabilities = PromptCapabilities.model_validate({"embeddedContext": True})
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    message = _FakeMessage(
        text="summarize this clip",
        chat=_FakeChat(id=123),
        id=47,
        video=_FakeMedia(file_name="clip.mp4", data=b"\x00\x00\x00\x18", mime_type="video/mp4"),
    )

    await gateway.handle_message(_client(fake_app), _message(message))

    prompt = cast(list[_PromptBlock], conn.prompt_calls[0][1])
    assert len(prompt) == 2
    assert prompt[0].type == "text"
    assert prompt[1].type == "resource"
    assert isinstance(prompt[1].resource, BlobResourceContents)
    assert prompt[1].resource.uri == "telegram://chat/123/message/47/clip.mp4"
    assert prompt[1].resource.mime_type == "video/mp4"


@pytest.mark.asyncio
async def test_handle_message_embeds_static_sticker_as_image_when_supported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.prompt_capabilities = PromptCapabilities.model_validate({"image": True})
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    message = _FakeMessage(
        text="explain sticker",
        chat=_FakeChat(id=123),
        id=48,
        sticker=_FakeMedia(file_name="sticker.webp", data=b"RIFF", mime_type="image/webp"),
    )

    await gateway.handle_message(_client(fake_app), _message(message))

    prompt = cast(list[_PromptBlock], conn.prompt_calls[0][1])
    assert len(prompt) == 2
    assert prompt[0].type == "text"
    assert prompt[1].type == "image"
    assert prompt[1].mime_type == "image/webp"


@pytest.mark.asyncio
async def test_handle_message_reports_request_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
        prompt_error=RequestError(500, "Internal error", {"details": "broken"}),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    await gateway.handle_message(
        _client(fake_app),
        _message(_FakeMessage(text="hello", chat=_FakeChat(id=123))),
    )

    assert fake_app.edited_messages[-1] == (123, 1, "Runtime error: Internal error\ndetails=broken")


@pytest.mark.asyncio
async def test_handle_message_reports_missing_session_as_create_session_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
        prompt_error=RequestError(404, "Resource not found"),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway.session_to_chat["session-1"] = "123"

    await gateway.handle_message(
        _client(fake_app),
        _message(_FakeMessage(text="hello", chat=_FakeChat(id=123))),
    )

    assert fake_app.edited_messages[-1] == (
        123,
        1,
        "Create a session first with /new or /new <name>.",
    )
    assert gateway.alias_store.load_binding("123").active_session_id is None


@pytest.mark.asyncio
async def test_handle_message_blocks_while_prompt_in_flight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.conn = _agent(
        _FakeConn(
            prompt_response=_prompt_response(),
            new_session_response=_new_session_response(),
            load_session_response=_load_session_response(),
            list_sessions_response=_list_sessions_response(tmp_path),
            set_session_mode_response=_set_mode_response(),
        )
    )
    state = gateway._state("123")
    state.prompt_in_flight = True
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))

    message = _FakeMessage(text="hello", chat=_FakeChat(id=123))

    await gateway.handle_message(_client(fake_app), _message(message))

    assert message.replies == ["A run is already in progress. Wait for it to finish or use /stop."]


@pytest.mark.asyncio
async def test_handle_message_supports_new_session_status_and_mode_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)

    session_message = _FakeMessage(text="/new", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(session_message))
    assert session_message.replies == ["Active session: `session-1`"]

    status_message = _FakeMessage(text="/session", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(status_message))
    assert status_message.replies == [
        "Surface: `chat:123`\nTopic Key: `123`\nStreaming: `false`\nPlan Projection: `html card`\nSession: `session-1`\nMode: `ask`"
    ]

    mode_message = _FakeMessage(text="/agent", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(mode_message))
    assert mode_message.replies == ["Mode: `agent`"]
    assert conn.set_mode_calls == [("agent", "session-1")]


@pytest.mark.asyncio
async def test_handle_message_supports_bind_and_unbind_for_topic_surfaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response("session-2"),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding(
        "123:7",
        ChatBinding(active_session_id="session-1", aliases={"docs": "session-2"}),
    )
    gateway.alias_store.save_binding("123:8", ChatBinding(active_session_id="session-3"))
    gateway.session_to_chat["session-1"] = "123:7"
    gateway.session_to_chat["session-3"] = "123:8"

    bind_message = _FakeMessage(text="/bind docs", chat=_FakeChat(id=123), message_thread_id=7)
    await gateway.handle_message(_client(fake_app), _message(bind_message))

    assert bind_message.replies == ["Bound this surface to session: `session-2`"]
    assert gateway.alias_store.load_binding("123:7").active_session_id == "session-2"
    assert gateway.alias_store.load_binding("123:8").active_session_id == "session-3"

    unbind_message = _FakeMessage(text="/unbind", chat=_FakeChat(id=123), message_thread_id=7)
    await gateway.handle_message(_client(fake_app), _message(unbind_message))

    assert unbind_message.replies == ["Unbound this surface from session: `session-2`"]
    assert gateway.alias_store.load_binding("123:7").active_session_id is None
    assert gateway.alias_store.load_binding("123:8").active_session_id == "session-3"
    assert "session-2" not in gateway.session_to_chat


@pytest.mark.asyncio
async def test_handle_message_supports_topic_session_and_topics_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.alias_store.save_binding(
        "123:7",
        ChatBinding(active_session_id="session-1", aliases={"docs": "session-1"}),
    )
    gateway.alias_store.save_binding("123:8", ChatBinding(active_session_id="session-2"))

    topic_session_message = _FakeMessage(
        text="/topic_session",
        chat=_FakeChat(id=123),
        message_thread_id=7,
    )
    await gateway.handle_message(_client(fake_app), _message(topic_session_message))

    assert topic_session_message.replies == [
        "Surface: `topic:7`\nTopic key: `123:7`\nActive session: `session-1`\nAliases: `docs` -> `session-1`"
    ]

    topics_message = _FakeMessage(text="/topics", chat=_FakeChat(id=123), message_thread_id=7)
    await gateway.handle_message(_client(fake_app), _message(topics_message))

    assert topics_message.replies == [
        "Surface bindings:\n- topic:7 | session=session-1 | aliases=1\n- topic:8 | session=session-2 | aliases=0"
    ]


@pytest.mark.asyncio
async def test_handle_message_supports_new_topic_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response("session-9"),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)

    message = _FakeMessage(text="/new_topic docs triage", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(message))

    assert fake_app.created_forum_topics == [(123, "docs triage")]
    assert message.replies == ["Created topic `docs triage` and bound it to session: `session-9`"]
    assert gateway.alias_store.load_binding("123:77").active_session_id == "session-9"
    assert gateway.session_to_chat["session-9"] == "123:77"
    assert fake_app.sent_message_kwargs[-2]["message_thread_id"] == 77
    assert fake_app.sent_message_kwargs[-1]["message_thread_id"] == 77


@pytest.mark.asyncio
async def test_handle_message_supports_new_topic_with_enum_like_supergroup_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response("session-10"),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)

    message = _FakeMessage(
        text="/new_topic infra",
        chat=_FakeChat(id=123, type=_FakeChatType(name="SUPERGROUP", value="supergroup")),
    )
    await gateway.handle_message(_client(fake_app), _message(message))

    assert fake_app.created_forum_topics == [(123, "infra")]
    assert message.replies == ["Created topic `infra` and bound it to session: `session-10`"]


@pytest.mark.asyncio
async def test_handle_message_rejects_new_topic_outside_forum_supergroup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))

    private_message = _FakeMessage(
        text="/new_topic docs triage",
        chat=_FakeChat(id=123, type="private", is_forum=False),
    )
    await gateway.handle_message(_client(fake_app), _message(private_message))

    assert private_message.replies == ["This command requires a forum-enabled supergroup."]
    assert fake_app.created_forum_topics == []

    non_forum_message = _FakeMessage(
        text="/new_topic docs triage",
        chat=_FakeChat(id=456, type="supergroup", is_forum=False),
    )
    await gateway.handle_message(_client(fake_app), _message(non_forum_message))

    assert non_forum_message.replies == ["This supergroup does not have forum topics enabled."]
    assert fake_app.created_forum_topics == []


@pytest.mark.asyncio
async def test_handle_message_supports_repair_command_for_stale_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
        load_session_result=None,
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123:7", ChatBinding(active_session_id="session-stale"))
    gateway.session_to_chat["session-stale"] = "123:7"

    async def _raise_missing(*, cwd: str, session_id: str, mcp_servers: list[object]):
        del cwd, session_id, mcp_servers
        raise RequestError(404, "Resource not found")

    monkeypatch.setattr(conn, "load_session", _raise_missing)

    message = _FakeMessage(text="/repair", chat=_FakeChat(id=123), message_thread_id=7)
    await gateway.handle_message(_client(fake_app), _message(message))

    assert message.replies == ["Cleared stale binding for session: `session-stale`"]
    assert gateway.alias_store.load_binding("123:7").active_session_id is None
    assert "session-stale" not in gateway.session_to_chat


@pytest.mark.asyncio
async def test_handle_message_supports_repair_command_for_existing_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123:7", ChatBinding(active_session_id="session-1"))

    message = _FakeMessage(text="/repair", chat=_FakeChat(id=123), message_thread_id=7)
    await gateway.handle_message(_client(fake_app), _message(message))

    assert message.replies == ["Repaired binding for session: `session-1`"]
    assert gateway.alias_store.load_binding("123:7").active_session_id == "session-1"


@pytest.mark.asyncio
async def test_prompt_approval_sends_message_into_current_topic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))

    approval_task = asyncio.create_task(
        gateway._prompt_approval(
            chat_key="123:7",
            session_id="session-1",
            tool_call_id="tool-1",
            tool_title="Write file",
            preview_text="<b>Approval required</b>",
            options=[
                _permission_option(kind="allow_once", name="Allow Once", option_id="allow_once")
            ],
        )
    )
    await asyncio.sleep(0)

    assert fake_app.sent_messages == [(123, "<b>Approval required</b>")]
    assert fake_app.sent_message_kwargs[-1]["message_thread_id"] == 7

    pending = next(iter(gateway.pending_approvals.values()))
    pending.future.set_result(
        RequestPermissionResponse.model_validate(
            {"outcome": {"outcome": "selected", "optionId": "allow_once"}}
        )
    )
    response = await approval_task

    assert response.outcome.option_id == "allow_once"


@pytest.mark.asyncio
async def test_handle_message_supports_sessions_and_stop_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding(
        "123",
        ChatBinding(active_session_id="session-1", aliases={"named": "session-1"}),
    )

    list_message = _FakeMessage(text="/sessions", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(list_message))
    assert "Primary session" in list_message.replies[0]
    assert "named" in list_message.replies[0]

    stop_message = _FakeMessage(text="/stop", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(stop_message))
    assert conn.cancelled_sessions == ["session-1"]
    assert fake_app.sent_messages[-1] == (123, "Run cancelled.")


@pytest.mark.asyncio
async def test_request_permission_and_callback_complete_allow_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"
    options = [
        _permission_option(kind="allow_once", name="Allow Once", option_id="1"),
        _permission_option(kind="reject_once", name="Deny Once", option_id="2"),
    ]
    tool_call = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-1",
            "title": "write file",
            "status": "in_progress",
        }
    )

    task = asyncio.create_task(
        gateway.request_permission(
            options,
            session_id="session-1",
            tool_call=tool_call,
        )
    )
    await asyncio.sleep(0)
    approval_id = next(iter(gateway.pending_approvals))
    query_message = _FakeMessage(text=None, chat=_FakeChat(id=123), id=99)
    query = _FakeCallbackQuery(data=f"appr:{approval_id}:0", message=query_message)

    await gateway.handle_callback_query(_client(fake_app), _callback_query(query))
    response = await task

    assert response.outcome.outcome == "selected"
    assert response.outcome.option_id == "1"
    assert query.edited_texts
    assert "waiting for execution result" in query.edited_texts[-1]


@pytest.mark.asyncio
async def test_request_permission_and_callback_mark_deny_as_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"
    options = [
        _permission_option(kind="allow_once", name="Allow Once", option_id="1"),
        _permission_option(kind="reject_once", name="Deny Once", option_id="2"),
    ]
    tool_call = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-2",
            "title": "read file",
            "status": "in_progress",
        }
    )

    task = asyncio.create_task(
        gateway.request_permission(
            options,
            session_id="session-1",
            tool_call=tool_call,
        )
    )
    await asyncio.sleep(0)
    approval_id = next(iter(gateway.pending_approvals))
    query_message = _FakeMessage(text=None, chat=_FakeChat(id=123), id=100)
    query = _FakeCallbackQuery(data=f"appr:{approval_id}:1", message=query_message)

    await gateway.handle_callback_query(_client(fake_app), _callback_query(query))
    response = await task

    assert response.outcome.outcome == "selected"
    assert response.outcome.option_id == "2"
    assert query.edited_texts
    assert "rejected" in query.edited_texts[-1]


@pytest.mark.asyncio
async def test_session_update_finalizes_matching_approval_and_updates_current_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway._state("123").resolved_approval = ResolvedApproval(
        tool_call_id="tool-1",
        tool_title="write file",
        label="Allow Once",
        message_id=77,
    )

    await gateway.session_update(
        "session-1",
        start_tool_call(
            "tool-1",
            "write file",
            status="completed",
            content=[tool_diff_content("README.md", "# new", "# old")],
        ),
    )
    await gateway.session_update(
        "session-1",
        CurrentModeUpdate.model_validate(
            {
                "currentModeId": "agent",
                "sessionUpdate": "current_mode_update",
            }
        ),
    )

    assert any(
        message_id == 77 and "completed" in text for _, message_id, text in fake_app.edited_messages
    )
    assert any("allow once" in text.lower() for _, _, text in fake_app.edited_messages)
    binding = gateway.alias_store.load_binding("123")
    assert binding.current_mode_id == "agent"


@pytest.mark.asyncio
async def test_session_update_tracks_available_commands_and_config_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))

    await gateway.session_update(
        "session-1",
        AvailableCommandsUpdate.model_validate(
            {
                "sessionUpdate": "available_commands_update",
                "availableCommands": [
                    {"name": "tools", "description": "List tools"},
                    {
                        "name": "thinking",
                        "description": "Change thinking",
                        "input": {"hint": "medium|high"},
                    },
                ],
            }
        ),
    )
    await gateway.session_update(
        "session-1",
        ConfigOptionUpdate.model_validate(
            {
                "sessionUpdate": "config_option_update",
                "configOptions": [
                    {
                        "id": "thinking",
                        "name": "Thinking",
                        "type": "select",
                        "currentValue": "medium",
                        "options": [
                            {"name": "Medium", "value": "medium"},
                            {"name": "High", "value": "high"},
                        ],
                    }
                ],
            }
        ),
    )

    state = gateway._state("123")
    assert "tools" in state.dynamic_commands
    assert "thinking" in state.dynamic_commands
    assert fake_app.sent_messages == []
    assert fake_app.commands is not None
    assert any(getattr(command, "command", None) == "thinking" for command in fake_app.commands)


@pytest.mark.asyncio
async def test_dynamic_model_and_config_commands_call_acp_selection_methods(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
        set_config_option_response=_set_config_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    state = gateway._state("123")
    state.current_model_id = "model-a"
    state.available_model_ids = ["model-a", "model-b"]
    state.selection_options = list(_set_config_response().config_options)
    gateway._rebuild_dynamic_commands("123")

    model_message = _FakeMessage(text="/model model-b", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(model_message))
    assert conn.set_model_calls == [("model-b", "session-1")]
    assert model_message.replies == ["Model: `model-b`"]

    config_message = _FakeMessage(text="/thinking high", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(config_message))
    assert conn.set_config_calls == [("thinking", "session-1", "high")]
    assert config_message.replies == ["thinking: `high`"]


def test_rebuild_dynamic_commands_flattens_grouped_select_hints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    state = gateway._state("123")
    state.selection_options = [
        SessionConfigOptionSelect.model_validate(
            {
                "id": "model_family",
                "name": "Model Family",
                "description": "Pick a model family.",
                "type": "select",
                "currentValue": "gpt-5-mini",
                "options": [
                    {
                        "group": "openai",
                        "name": "OpenAI",
                        "options": [
                            {"name": "GPT-5", "value": "gpt-5"},
                            {"name": "GPT-5 Mini", "value": "gpt-5-mini"},
                        ],
                    }
                ],
            }
        )
    ]

    gateway._rebuild_dynamic_commands("123")

    assert gateway._state("123").dynamic_commands["model_family"].hint == "gpt-5|gpt-5-mini"


@pytest.mark.asyncio
async def test_dynamic_server_command_forwards_as_slash_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))
    gateway._state("123").dynamic_commands["tools"] = SelectionCommand(
        acp_name="tools",
        telegram_name="tools",
        description="List tools",
        source="command",
    )

    message = _FakeMessage(text="/tools", chat=_FakeChat(id=123))

    await gateway.handle_message(_client(fake_app), _message(message))

    assert conn.prompt_calls
    assert conn.prompt_calls[0][0] == "session-1"


@pytest.mark.asyncio
async def test_dynamic_command_aliases_preserve_non_telegram_safe_acp_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"
    gateway.alias_store.save_binding(
        "123",
        ChatBinding(active_session_id="session-1", available_mode_ids=["full-access"]),
    )

    await gateway.session_update(
        "session-1",
        AvailableCommandsUpdate.model_validate(
            {
                "sessionUpdate": "available_commands_update",
                "availableCommands": [
                    {"name": "read-only", "description": "Switch to read only"},
                ],
            }
        ),
    )

    state = gateway._state("123")
    assert "read_only" in state.dynamic_commands
    assert state.dynamic_commands["read_only"].acp_name == "read-only"
    assert "full_access" in state.dynamic_commands
    assert state.dynamic_commands["full_access"].acp_name == "full-access"
    assert fake_app.commands is not None
    assert any(getattr(command, "command", None) == "read_only" for command in fake_app.commands)
    assert any(getattr(command, "command", None) == "full_access" for command in fake_app.commands)


@pytest.mark.asyncio
async def test_mode_alias_command_routes_back_to_real_mode_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding(
        "123",
        ChatBinding(active_session_id="session-1", available_mode_ids=["full-access"]),
    )
    gateway._rebuild_dynamic_commands("123")

    message = _FakeMessage(text="/full_access", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(message))

    assert conn.set_mode_calls == [("full-access", "session-1")]
    assert message.replies == ["Mode: `full-access`"]


@pytest.mark.asyncio
async def test_streaming_command_updates_chat_local_setting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))

    enable_message = _FakeMessage(text="/streaming true", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(enable_message))
    assert enable_message.replies == [
        "Streaming enabled. Agent replies will update incrementally with throttled Telegram edits."
    ]
    assert gateway.alias_store.load_binding("123").streaming_enabled is True

    disable_message = _FakeMessage(text="/streaming false", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(disable_message))
    assert disable_message.replies == [
        "Streaming disabled. ACP Router will buffer agent chunks and send the final reply once the run completes."
    ]
    assert gateway.alias_store.load_binding("123").streaming_enabled is False


@pytest.mark.asyncio
async def test_streaming_command_reports_current_state_and_usage_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))

    state_message = _FakeMessage(text="/streaming", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(state_message))
    assert state_message.replies == ["Streaming: `false`"]

    invalid_message = _FakeMessage(text="/streaming maybe", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(invalid_message))
    assert invalid_message.replies == ["Usage: /streaming <true|false>"]


def test_streaming_helpers_cover_defaults_and_zero_interval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))

    assert gateway._streaming_enabled("123") is False
    assert TelegramGateway._parse_toggle_argument("on") is True
    assert TelegramGateway._parse_toggle_argument("off") is False
    assert TelegramGateway._parse_toggle_argument("maybe") is None

    gateway._state("123").response_message_id = 1
    gateway.settings = AppSettings(
        telegram_api_id=gateway.settings.telegram_api_id,
        telegram_api_hash=gateway.settings.telegram_api_hash,
        telegram_bot_token=gateway.settings.telegram_bot_token,
        telegram_session_name=gateway.settings.telegram_session_name,
        telegram_business_connection_id=gateway.settings.telegram_business_connection_id,
        acp_command=gateway.settings.acp_command,
        workspace_root=gateway.settings.workspace_root,
        state_dir=gateway.settings.state_dir,
        acp_cwd=gateway.settings.acp_cwd,
        acp_stdio_buffer_limit_bytes=gateway.settings.acp_stdio_buffer_limit_bytes,
        enable_host_tools=gateway.settings.enable_host_tools,
        streaming_default=gateway.settings.streaming_default,
        streaming_edit_interval_seconds=0.0,
    )
    assert gateway._should_stream_response_edit("123") is True


@pytest.mark.asyncio
async def test_enabled_client_host_tools_can_read_write_and_execute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path, enable_host_tools=True))
    gateway.session_to_chat["session-1"] = "123"
    assert gateway.workspace_manager is not None
    assert gateway.terminal_manager is not None
    gateway.workspace_manager.bind_session("session-1", tmp_path)

    async def _allow_file(self, **kwargs):
        del self, kwargs
        return "123", "host-file"

    async def _allow_command(self, **kwargs):
        del self, kwargs
        return "123", "host-command"

    monkeypatch.setattr(TelegramGateway, "_approve_host_file_request", _allow_file)
    monkeypatch.setattr(TelegramGateway, "_approve_host_command_request", _allow_command)

    await gateway.write_text_file("hello", "note.txt", "session-1")
    read_response = await gateway.read_text_file("note.txt", "session-1")

    created = await gateway.create_terminal(
        sys.executable,
        session_id="session-1",
        args=["-c", "print('ok')"],
        cwd=str(tmp_path),
    )
    wait = await gateway.wait_for_terminal_exit("session-1", created.terminal_id)
    output = await gateway.terminal_output("session-1", created.terminal_id)
    await gateway.release_terminal("session-1", created.terminal_id)

    assert read_response.content == "hello"
    assert "ok" in output.output
    assert wait.exit_code == 0


def test_parse_command_handles_plain_text_and_mentions() -> None:
    assert TelegramGateway._parse_command(None) is None
    assert TelegramGateway._parse_command("hello") is None
    assert TelegramGateway._parse_command("/ask@bot extra") == ["ask", "extra"]


def test_approval_button_label_keeps_unknown_labels_plain() -> None:
    assert TelegramGateway._approval_button_label("Maybe Later") == "Maybe Later"


def test_suppress_request_error_logging_filters_semantic_request_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    recorded: list[BaseException] = []

    def _record(task: asyncio.Task[object], exc: BaseException) -> None:
        del task
        recorded.append(exc)

    class _Supervisor:
        def __init__(self) -> None:
            self._error_handlers = [_record]

    class _RawConn:
        def __init__(self) -> None:
            self._tasks = _Supervisor()

    class _Conn:
        def __init__(self) -> None:
            self._conn = _RawConn()

    conn = _Conn()
    gateway._suppress_request_error_logging(cast(AcpAgent, conn))

    handler = conn._conn._tasks._error_handlers[0]
    handler(cast(asyncio.Task[object], object()), RequestError(400, "Denied", {"reason": "no"}))
    assert recorded == []

    runtime_error = RuntimeError("boom")
    handler(cast(asyncio.Task[object], object()), runtime_error)
    assert recorded == [runtime_error]


@pytest.mark.asyncio
async def test_approve_host_requests_accept_selected_allow_option(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"

    async def _allow(**kwargs):
        del kwargs
        return RequestPermissionResponse.model_validate(
            {
                "outcome": {
                    "outcome": "selected",
                    "optionId": "allow_once",
                }
            }
        )

    monkeypatch.setattr(gateway, "_prompt_approval", _allow)

    chat_key, file_tool_call_id = await gateway._approve_host_file_request(
        session_id="session-1",
        action="Read file",
        path="note.txt",
        resolved_path=tmp_path / "note.txt",
        preview_content="preview",
    )
    command_chat_key, command_tool_call_id = await gateway._approve_host_command_request(
        session_id="session-1",
        command="python",
        args=["-V"],
        cwd=tmp_path,
    )

    assert chat_key == "123"
    assert command_chat_key == "123"
    assert file_tool_call_id != ""
    assert command_tool_call_id != ""


def test_host_tool_permission_options_and_render_helpers(tmp_path: Path) -> None:
    options = TelegramGateway._host_tool_permission_options()
    assert [option.option_id for option in options] == ["allow_once", "deny_once"]

    file_preview = TelegramGateway._render_host_file_approval(
        action="Write file",
        path="note.py",
        resolved_path=tmp_path / "note.py",
        preview_content="hello",
    )
    command_preview = TelegramGateway._render_host_command_approval(
        command_line="python -V",
        cwd=tmp_path,
    )

    assert "requested path" in file_preview
    assert "resolved path" in file_preview
    assert "hello" in file_preview
    assert "\\n" not in file_preview
    assert '<pre language="python">' in file_preview
    assert "Execute command" in command_preview
    assert str(tmp_path) in command_preview
    assert "\\n" not in command_preview
    assert '<pre language="bash">' in command_preview


@pytest.mark.asyncio
async def test_host_file_request_reuses_prior_tool_approval_without_prompting_again(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"
    gateway._state("123").resolved_approval = ResolvedApproval(
        tool_call_id="tool-1",
        tool_title="mcp_host_write_workspace_file",
        label="Allow Once",
        message_id=77,
    )

    unexpected_prompt = AsyncMock(
        return_value=RequestPermissionResponse.model_validate(
            {"outcome": {"outcome": "selected", "optionId": "allow_once"}}
        )
    )
    monkeypatch.setattr(gateway, "_prompt_approval", unexpected_prompt)

    chat_key, tool_call_id = await gateway._approve_host_file_request(
        session_id="session-1",
        action="Write file",
        path="note.txt",
        resolved_path=tmp_path / "note.txt",
        preview_content="hello",
    )

    unexpected_prompt.assert_not_awaited()
    assert chat_key == "123"
    assert tool_call_id == "tool-1"


def test_resolve_guarded_workspace_path_enforces_workspace_and_session_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    assert gateway.workspace_manager is not None
    session_cwd = tmp_path / "project"
    session_cwd.mkdir()
    gateway.workspace_manager.bind_session("session-1", session_cwd)

    resolved = gateway._resolve_guarded_workspace_path(
        workspace_manager=gateway.workspace_manager,
        session_id="session-1",
        path="note.txt",
    )
    assert resolved == session_cwd / "note.txt"

    with pytest.raises(RequestError, match="File access rejected"):
        gateway._resolve_guarded_workspace_path(
            workspace_manager=gateway.workspace_manager,
            session_id="session-1",
            path="../outside.txt",
        )

    with pytest.raises(RequestError, match="File access rejected"):
        gateway._resolve_guarded_workspace_path(
            workspace_manager=gateway.workspace_manager,
            session_id="session-1",
            path="../../escape.txt",
        )


def test_resolve_guarded_command_cwd_enforces_workspace_and_session_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    assert gateway.workspace_manager is not None
    session_cwd = tmp_path / "project"
    session_cwd.mkdir()
    gateway.workspace_manager.bind_session("session-1", session_cwd)

    resolved = gateway._resolve_guarded_command_cwd(
        workspace_manager=gateway.workspace_manager,
        session_id="session-1",
        cwd=None,
    )
    assert resolved == session_cwd

    with pytest.raises(RequestError, match="Command execution rejected"):
        gateway._resolve_guarded_command_cwd(
            workspace_manager=gateway.workspace_manager,
            session_id="session-1",
            cwd=str(tmp_path),
        )

    with pytest.raises(RequestError, match="Command execution rejected"):
        gateway._resolve_guarded_command_cwd(
            workspace_manager=gateway.workspace_manager,
            session_id="session-1",
            cwd=str(tmp_path.parent),
        )


def test_ensure_host_tool_allowed_handles_invalid_cancel_and_deny() -> None:
    allow_response = RequestPermissionResponse.model_validate(
        {
            "outcome": {
                "outcome": "selected",
                "optionId": "allow_once",
            }
        }
    )
    TelegramGateway._ensure_host_tool_allowed(
        response=allow_response,
        action="Write file",
        details={"path": "note.txt"},
    )

    cancel_response = RequestPermissionResponse.model_validate(
        {
            "outcome": {
                "outcome": "cancelled",
            }
        }
    )
    with pytest.raises(RequestError, match="Write file cancelled"):
        TelegramGateway._ensure_host_tool_allowed(
            response=cancel_response,
            action="Write file",
            details={"path": "note.txt"},
        )

    deny_response = RequestPermissionResponse.model_validate(
        {
            "outcome": {
                "outcome": "selected",
                "optionId": "deny_once",
            }
        }
    )
    with pytest.raises(RequestError, match="Write file rejected"):
        TelegramGateway._ensure_host_tool_allowed(
            response=deny_response,
            action="Write file",
            details={"path": "note.txt"},
        )


def test_ensure_host_tool_allowed_rejects_invalid_payload_shape() -> None:
    class _BadResponse:
        def model_dump(self, **kwargs):
            del kwargs
            return {"outcome": "bad"}

    with pytest.raises(RequestError, match="Write file rejected"):
        TelegramGateway._ensure_host_tool_allowed(
            response=cast(RequestPermissionResponse, _BadResponse()),
            action="Write file",
            details={"path": "note.txt"},
        )


def test_is_subpath_handles_commonpath_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "acprouter.telegram_gateway.os.path.commonpath",
        lambda paths: (_ for _ in ()).throw(ValueError("boom")),
    )
    assert TelegramGateway._is_subpath(Path("/root"), Path("/target")) is False


@pytest.mark.asyncio
async def test_run_can_spawn_agent_process_from_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    conn = _FakeAgent(initialized_protocols=[], connected_clients=[])
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)

    @asynccontextmanager
    async def _fake_spawn_agent_process(*args, **kwargs):
        del args, kwargs
        yield conn, object()

    async def _fake_idle() -> None:
        return None

    monkeypatch.setattr("acprouter.telegram_gateway.spawn_agent_process", _fake_spawn_agent_process)
    monkeypatch.setattr("acprouter.telegram_gateway.idle", _fake_idle)

    gateway = TelegramGateway.from_settings(_settings(tmp_path))

    await gateway.run()

    assert conn.initialized_protocols
    assert fake_app.started is True
    assert fake_app.stopped is True


@pytest.mark.asyncio
async def test_run_telegram_gateway_builds_and_runs_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    recorded: dict[str, object] = {}

    @dataclass
    class _Gateway:
        async def run(self) -> None:
            recorded["ran"] = True

    def _fake_from_settings(settings: AppSettings) -> _Gateway:
        recorded["settings"] = settings
        return _Gateway()

    monkeypatch.setattr(
        "acprouter.telegram_gateway.TelegramGateway.from_settings", _fake_from_settings
    )

    from acprouter.telegram_gateway import run_telegram_gateway

    settings = _settings(tmp_path)
    await run_telegram_gateway(settings)

    assert recorded == {"settings": settings, "ran": True}


@pytest.mark.asyncio
async def test_help_and_start_commands_reply_with_help_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))

    help_message = _FakeMessage(text="/help", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(help_message))
    assert "Commands:" in help_message.replies[0]
    assert "/snapshot" in help_message.replies[0]

    start_message = _FakeMessage(text="/start", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(start_message))
    assert "Commands:" in start_message.replies[0]
    assert "/snapshot" in start_message.replies[0]


@pytest.mark.asyncio
async def test_snapshot_command_exports_current_session_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"
    gateway.alias_store.save_binding(
        "123",
        ChatBinding(
            active_session_id="session-1",
            aliases={"named": "session-1"},
            available_mode_ids=["ask", "agent"],
            current_mode_id="agent",
            streaming_enabled=True,
        ),
    )
    state = gateway._state("123")
    state.status_message_id = 10
    state.response_message_id = 11
    state.tool_message_ids["tool-1"] = 12
    state.agent_text = "partial reply"
    state.current_model_id = "openai:gpt-5"
    state.available_model_ids = ["openai:gpt-5", "openai:gpt-5-mini"]
    state.resolved_approval = ResolvedApproval(
        tool_call_id="tool-1",
        tool_title="write file",
        label="Allow Once",
        message_id=88,
    )
    approval_future = asyncio.get_running_loop().create_future()
    gateway.pending_approvals["approval-1"] = PendingApproval(
        approval_id="approval-1",
        session_id="session-1",
        tool_call_id="tool-2",
        tool_title="run command",
        preview_text="preview",
        options=[_permission_option(kind="allow_once", name="Allow Once", option_id="1")],
        future=approval_future,
    )

    message = _FakeMessage(text="/snapshot", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(message))

    assert len(fake_app.sent_documents) == 1
    chat_id, raw_snapshot, caption = fake_app.sent_documents[0]
    payload = json.loads(raw_snapshot)
    assert chat_id == 123
    assert caption == "Current session snapshot"
    assert payload["chat_key"] == "123"
    assert payload["binding"]["active_session_id"] == "session-1"
    assert payload["binding"]["aliases"] == {"named": "session-1"}
    assert payload["state"]["tool_message_ids"] == {"tool-1": 12}
    assert payload["state"]["resolved_approval"]["label"] == "Allow Once"
    assert payload["pending_approvals"][0]["tool_call_id"] == "tool-2"


@pytest.mark.asyncio
async def test_topic_messages_bind_and_reply_with_thread_aware_chat_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response("topic-session"),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)

    message = _FakeMessage(text="/new topic", chat=_FakeChat(id=-100123), message_thread_id=42)
    await gateway.handle_message(_client(fake_app), _message(message))

    binding = gateway.alias_store.load_binding("-100123:42")
    assert binding.active_session_id == "topic-session"
    assert gateway.session_to_chat["topic-session"] == "-100123:42"
    assert message.replies == ["Active session: `topic-session`"]
    assert any(kwargs.get("message_thread_id") == 42 for kwargs in fake_app.sent_message_kwargs)


@pytest.mark.asyncio
async def test_command_flow_covers_pending_approval_and_switch_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding(
        "123",
        ChatBinding(
            active_session_id="session-1",
            aliases={"named": "session-1"},
        ),
    )
    gateway._state("123").current_approval_id = "approval-1"
    gateway.pending_approvals["approval-1"] = PendingApproval(
        approval_id="approval-1",
        session_id="session-1",
        tool_call_id="tool-1",
        tool_title="write file",
        preview_text="preview",
        options=[],
        future=asyncio.get_running_loop().create_future(),
    )

    blocked = _FakeMessage(text="/switch named", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(blocked))
    assert blocked.replies == ["Approval pending. Select an option first."]

    gateway.pending_approvals.clear()
    gateway._state("123").current_approval_id = None

    load_message = _FakeMessage(text="/switch named", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(load_message))
    assert load_message.replies == ["Loaded session: `session-1`"]


@pytest.mark.asyncio
async def test_mode_command_reports_unavailable_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.conn = _agent(
        _FakeConn(
            prompt_response=_prompt_response(),
            new_session_response=_new_session_response(),
            load_session_response=_load_session_response(),
            list_sessions_response=_list_sessions_response(tmp_path),
            set_session_mode_response=_set_mode_response(),
        )
    )
    gateway.alias_store.save_binding(
        "123",
        ChatBinding(active_session_id="session-1", available_mode_ids=["ask"]),
    )

    mode_message = _FakeMessage(text="/mode agent", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(mode_message))

    assert mode_message.replies == ["Mode `agent` is unavailable."]


@pytest.mark.asyncio
async def test_callback_exception_path_reports_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.pending_approvals["approval-1"] = PendingApproval(
        approval_id="approval-1",
        session_id="session-1",
        tool_call_id="tool-1",
        tool_title="write file",
        preview_text="preview",
        options=[_permission_option(kind="allow_once", name="Allow Once", option_id="1")],
        future=asyncio.get_running_loop().create_future(),
    )
    gateway.session_to_chat["session-1"] = "123"
    query = _FakeCallbackQuery(
        data="appr:approval-1:not-an-int",
        message=_FakeMessage(text=None, chat=_FakeChat(id=123)),
    )

    await gateway.handle_callback_query(_client(fake_app), _callback_query(query))

    assert query.answers[-1] == ("Action failed", True)
    assert "Runtime error:" in fake_app.sent_messages[-1][1]


@pytest.mark.asyncio
async def test_handle_command_reports_missing_mode_argument_and_no_active_session_for_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.conn = _agent(
        _FakeConn(
            prompt_response=_prompt_response(),
            new_session_response=_new_session_response(),
            load_session_response=_load_session_response(),
            list_sessions_response=_list_sessions_response(tmp_path),
            set_session_mode_response=_set_mode_response(),
        )
    )

    mode_message = _FakeMessage(text="/mode", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(mode_message))
    assert mode_message.replies == ["Usage: /mode <mode-id>"]

    stop_message = _FakeMessage(text="/stop", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(stop_message))
    assert stop_message.replies == ["No active session."]


@pytest.mark.asyncio
async def test_try_set_mode_rejects_request_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )

    async def _failing_set_mode(mode_id: str, session_id: str) -> SetSessionModeResponse:
        del mode_id, session_id
        raise RequestError(400, "bad mode")

    monkeypatch.setattr(conn, "set_session_mode", _failing_set_mode)
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(active_session_id="session-1"))

    allowed = await gateway._try_set_mode(
        chat_key="123",
        session_id="session-1",
        mode_id="ask",
    )

    assert allowed is False


@pytest.mark.asyncio
async def test_report_runtime_error_handles_chunk_limit_messages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))

    await gateway._report_runtime_error(
        chat_key="123",
        error=ValueError("chunk is longer than limit"),
    )

    assert "ACP connection failed" in fake_app.sent_messages[-1][1]


def test_chat_for_session_raises_when_binding_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))

    with pytest.raises(RuntimeError, match="No Telegram chat binding"):
        gateway._chat_for_session("missing-session")


@pytest.mark.asyncio
async def test_handle_callback_query_rejects_unknown_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    query = _FakeCallbackQuery(
        data="other", message=_FakeMessage(text=None, chat=_FakeChat(id=123))
    )

    await gateway.handle_callback_query(_client(fake_app), _callback_query(query))

    assert query.answers == [("Unknown action", True)]


@pytest.mark.asyncio
async def test_handle_callback_query_handles_missing_pending_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    query = _FakeCallbackQuery(
        data="appr:missing:0", message=_FakeMessage(text=None, chat=_FakeChat(id=123))
    )

    await gateway.handle_callback_query(_client(fake_app), _callback_query(query))

    assert query.answers == [("Approval is no longer active.", True)]


@pytest.mark.asyncio
async def test_handle_callback_query_supports_cancel_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    gateway.session_to_chat["session-1"] = "123"
    task = asyncio.create_task(
        gateway.request_permission(
            [_permission_option(kind="allow_once", name="Allow Once", option_id="1")],
            session_id="session-1",
            tool_call=ToolCallUpdate.model_validate(
                {"toolCallId": "tool-1", "title": "write file", "status": "in_progress"}
            ),
        )
    )
    await asyncio.sleep(0)
    approval_id = next(iter(gateway.pending_approvals))
    query = _FakeCallbackQuery(
        data=f"appr:{approval_id}:cancel",
        message=_FakeMessage(text=None, chat=_FakeChat(id=123), id=88),
    )

    await gateway.handle_callback_query(_client(fake_app), _callback_query(query))
    response = await task

    assert response.outcome.outcome == "cancelled"
    assert query.edited_texts
    assert "cancelled" in query.edited_texts[-1].lower()


@pytest.mark.asyncio
async def test_helper_methods_cover_default_paths_and_deduping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)
    binding = ChatBinding(active_session_id="session-1")
    gateway.alias_store.save_binding("123", binding)

    assert await gateway.ext_method("method", {"x": 1}) == {}
    assert await gateway.ext_notification("method", {"x": 1}) is None
    gateway.on_connect(_agent(conn))
    assert gateway.conn is conn

    await gateway._ensure_session(chat_key="123", alias=None, create_new=False)
    assert conn.load_session_calls

    await gateway._upsert_status_message(chat_key="123", text="Running...")
    await gateway._upsert_status_message(chat_key="123", text="Running...")
    await gateway._upsert_response_message(chat_key="123", text="first")
    await gateway._upsert_response_message(chat_key="123", text="first")
    await gateway._upsert_response_message(chat_key="123", text="second")

    assert fake_app.sent_messages.count((123, "Running...")) == 1
    assert fake_app.sent_messages.count((123, "first")) == 1
    assert (123, 1, "second") in fake_app.edited_messages


@pytest.mark.asyncio
async def test_alias_reuse_and_none_mode_sync_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path, enable_host_tools=True))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
        load_session_result=LoadSessionResponse.model_validate({}),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding("123", ChatBinding(aliases={"named": "session-1"}))
    assert gateway.workspace_manager is not None

    async def _allow_command(self, **kwargs):
        del self, kwargs
        return "123", "host-command"

    monkeypatch.setattr(TelegramGateway, "_approve_host_command_request", _allow_command)

    session_id = await gateway._ensure_session(chat_key="123", alias="named", create_new=True)
    loaded_session_id = await gateway._load_named_session(chat_key="123", name="named")
    binding = ChatBinding()
    gateway._sync_binding_modes_from_response(binding, LoadSessionResponse.model_validate({}))
    created = await gateway.create_terminal(
        sys.executable,
        session_id="session-1",
        args=["-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
    )
    await gateway.kill_terminal("session-1", created.terminal_id)
    await gateway.release_terminal("session-1", created.terminal_id)

    assert session_id == "session-1"
    assert loaded_session_id == "session-1"
    assert gateway.workspace_manager.session_cwd("session-1") == tmp_path
    assert binding.available_mode_ids == []


@pytest.mark.asyncio
async def test_fake_app_download_media_rejects_messages_without_prompt_media() -> None:
    fake_app = _FakeApp()
    message = _FakeMessage(text=None, chat=_FakeChat(id=123))

    with pytest.raises(AssertionError, match="prompt media"):
        await fake_app.download_media(message, in_memory=True)


@pytest.mark.asyncio
async def test_host_tool_failure_paths_finalize_failed_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path, enable_host_tools=True))
    gateway.session_to_chat["session-1"] = "123"
    finalize = AsyncMock()
    monkeypatch.setattr(gateway, "_finalize_host_tool_approval", finalize)
    monkeypatch.setattr(
        gateway,
        "_approve_host_file_request",
        AsyncMock(return_value=("123", "file-tool")),
    )
    monkeypatch.setattr(
        gateway,
        "_approve_host_command_request",
        AsyncMock(return_value=("123", "command-tool")),
    )

    async def _failing_write(self, session_id: str, path: str, content: str) -> None:
        del self, session_id, path, content
        raise OSError("write failed")

    async def _failing_read(self, session_id: str, path: str, **kwargs: object):
        del self, session_id, path, kwargs
        raise OSError("read failed")

    async def _failing_terminal(self, command: str, **kwargs: object):
        del self, command, kwargs
        raise OSError("terminal failed")

    monkeypatch.setattr(
        "acprouter.telegram_gateway.WorkspaceManager.write_text_file", _failing_write
    )
    monkeypatch.setattr("acprouter.telegram_gateway.WorkspaceManager.read_text_file", _failing_read)
    monkeypatch.setattr(
        "acprouter.telegram_gateway.TerminalManager.create_terminal",
        _failing_terminal,
    )

    with pytest.raises(OSError, match="write failed"):
        await gateway.write_text_file("content", "note.txt", "session-1")
    with pytest.raises(OSError, match="read failed"):
        await gateway.read_text_file("note.txt", "session-1", line=2, limit=5)
    with pytest.raises(OSError, match="terminal failed"):
        await gateway.create_terminal("python", "session-1", cwd=str(tmp_path))

    assert [call.kwargs["state"] for call in finalize.await_args_list] == [
        "failed",
        "failed",
        "failed",
    ]


@pytest.mark.asyncio
async def test_handle_message_and_command_edge_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)

    await gateway.handle_message(_client(fake_app), _message(_FakeMessage(text="hello", chat=None)))
    await gateway.handle_message(
        _client(fake_app),
        _message(_FakeMessage(text="hello", chat=_FakeChat(id=None))),
    )
    await gateway.handle_message(
        _client(fake_app),
        _message(_FakeMessage(text=None, chat=_FakeChat(id=123))),
    )

    gateway._state("123").current_approval_id = "approval-1"
    gateway.pending_approvals["approval-1"] = PendingApproval(
        approval_id="approval-1",
        session_id="session-1",
        tool_call_id="tool-1",
        tool_title="write file",
        preview_text="preview",
        options=[],
        future=asyncio.get_running_loop().create_future(),
    )
    pending_message = _FakeMessage(text="hello", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(pending_message))
    gateway.pending_approvals.clear()
    gateway._state("123").current_approval_id = None

    monkeypatch.setattr(gateway, "_build_prompt_blocks", AsyncMock(side_effect=ValueError("boom")))
    error_message = _FakeMessage(text="hello", chat=_FakeChat(id=123))
    await gateway.handle_message(_client(fake_app), _message(error_message))

    await gateway._handle_command(_message(_FakeMessage(text="/help", chat=None)), ["help"])
    await gateway._handle_command(
        _message(_FakeMessage(text="/help", chat=_FakeChat(id=None))),
        ["help"],
    )
    topic_usage = _FakeMessage(text="/new_topic", chat=_FakeChat(id=123))
    await gateway._handle_command(_message(topic_usage), ["new_topic"])
    bind_usage = _FakeMessage(text="/bind", chat=_FakeChat(id=123))
    await gateway._handle_command(_message(bind_usage), ["bind"])
    unbind_empty = _FakeMessage(text="/unbind", chat=_FakeChat(id=123))
    await gateway._handle_command(_message(unbind_empty), ["unbind"])
    mode_no_session = _FakeMessage(text="/mode ask", chat=_FakeChat(id=456))
    await gateway._handle_command(_message(mode_no_session), ["mode", "ask"])

    gateway.alias_store.save_binding(
        "123",
        ChatBinding(active_session_id="session-1", available_mode_ids=["ask"]),
    )
    mode_ok = _FakeMessage(text="/mode ask", chat=_FakeChat(id=123))
    await gateway._handle_command(_message(mode_ok), ["mode", "ask"])
    new_auto = _FakeMessage(text="/new auto", chat=_FakeChat(id=123))
    await gateway._handle_command(_message(new_auto), ["new", "auto"])
    switch_usage = _FakeMessage(text="/switch", chat=_FakeChat(id=123))
    await gateway._handle_command(_message(switch_usage), ["switch"])
    no_session = _FakeMessage(text="/unknown", chat=_FakeChat(id=789))
    await gateway._handle_command(_message(no_session), ["unknown"])
    unknown = _FakeMessage(text="/unknown", chat=_FakeChat(id=123))
    await gateway._handle_command(_message(unknown), ["unknown"])

    assert pending_message.replies == ["Approval pending. Select an option first."]
    assert any("Runtime error: boom" in text for _chat_id, text in fake_app.sent_messages)
    assert topic_usage.replies == ["Usage: /new_topic <title>"]
    assert bind_usage.replies == ["Usage: /bind <session-id-or-alias>"]
    assert unbind_empty.replies == ["No active session is bound here."]
    assert mode_no_session.replies == ["Create a session first with /new or /new <name>."]
    assert mode_ok.replies == ["Mode: `ask`"]
    assert switch_usage.replies == ["/switch <session-id-or-alias>"]
    assert no_session.replies == ["Create a session first with /new or /new <name>."]
    assert unknown.replies == ["Unknown command."]


@pytest.mark.asyncio
async def test_topic_repair_selection_and_projection_edge_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(
        _settings(tmp_path, telegram_business_connection_id="biz-123")
    )
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
    )
    gateway.conn = _agent(conn)

    monkeypatch.setattr(fake_app, "create_forum_topic", None)
    with pytest.raises(RequestError, match="Forum topics are unavailable"):
        await gateway._create_topic_session(chat_id=123, title="Topic")

    async def _bad_topic(**kwargs: object) -> object:
        del kwargs
        return SimpleNamespace(id="bad")

    monkeypatch.setattr(fake_app, "create_forum_topic", _bad_topic)
    with pytest.raises(RequestError, match="Forum topic creation failed"):
        await gateway._create_topic_session(chat_id=123, title="Topic")

    assert TelegramGateway._forum_command_guard(None) is not None
    assert TelegramGateway._normalize_chat_type("") is None
    assert TelegramGateway._normalize_chat_type("ChatType.SUPERGROUP") == "supergroup"
    assert TelegramGateway._normalize_chat_type("ChatType.PRIVATE") == "private"
    assert TelegramGateway._normalize_chat_type("ChatType.GROUP") == "group"
    assert TelegramGateway._normalize_chat_type(object()) is None

    assert await gateway._repair_chat_binding(chat_key="123", name=None) == (
        "No active session is bound here."
    )

    async def _load_session_error(**kwargs: object):
        del kwargs
        raise RequestError(500, "temporary failure")

    monkeypatch.setattr(conn, "load_session", _load_session_error)
    with pytest.raises(RequestError, match="temporary failure"):
        await gateway._repair_chat_binding(chat_key="123", name="session-1")

    async def _load_session_missing(**kwargs: object):
        del kwargs
        raise RequestError(404, "resource not found")

    monkeypatch.setattr(conn, "load_session", _load_session_missing)
    assert await gateway._repair_chat_binding(chat_key="123", name="session-1") == (
        "No active session is bound here."
    )

    await gateway._finalize_prompt("123", _prompt_response("cancelled"))
    response = NewSessionResponse.model_validate(
        {
            "sessionId": "session-1",
            "models": {
                "currentModelId": "openai:gpt-5",
                "availableModels": [{"modelId": "openai:gpt-5", "name": "GPT-5"}],
            },
            "configOptions": [
                {
                    "id": "web",
                    "name": "Web",
                    "type": "boolean",
                    "currentValue": True,
                }
            ],
        }
    )
    gateway._sync_selection_state_from_response("123", response)
    state = gateway._state("123")
    assert state.current_model_id == "openai:gpt-5"
    assert state.available_model_ids == ["openai:gpt-5"]
    assert state.selection_options[0].id == "web"

    await gateway._upsert_status_message(chat_key="123", text="Needs input")
    await gateway._upsert_status_message(
        chat_key="123",
        text="Needs input",
        reply_markup=object(),
    )
    await gateway._upsert_tool_message(chat_key="123", tool_call_id="tool-1", text="Tool")
    await gateway._upsert_tool_message(chat_key="123", tool_call_id="tool-1", text="Tool")

    await gateway._upsert_plan_message(chat_key="123", update=update_plan([]))
    gateway._state("123").plan_message_id = 99
    await gateway._upsert_plan_message(chat_key="123", update=update_plan([]))
    assert gateway._state("123").plan_message_id is None

    plan_state = gateway._state("plan")
    plan_state.plan_task_order = ["a", "b"]
    plan_state.plan_task_texts = {"a": "A", "b": "B"}
    assert (
        TelegramGateway._can_append_plan_tasks(plan_state, [SimpleNamespace(key="a", text="A")])
        is False
    )
    assert (
        TelegramGateway._can_append_plan_tasks(
            plan_state,
            [SimpleNamespace(key="a", text="A"), SimpleNamespace(key="c", text="C")],
        )
        is False
    )

    await gateway._send_snapshot(chat_key="123:42", chat_id=123)
    assert fake_app.sent_document_kwargs[-1]["message_thread_id"] == 42
    assert gateway._unbind_chat_key("missing") is None
    assert TelegramGateway._parse_command("/") is None


@pytest.mark.asyncio
async def test_dynamic_command_and_rendering_edge_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))
    conn = _FakeConn(
        prompt_response=_prompt_response(),
        new_session_response=_new_session_response(),
        load_session_response=_load_session_response(),
        list_sessions_response=_list_sessions_response(tmp_path),
        set_session_mode_response=_set_mode_response(),
        set_config_option_response=_set_config_response(),
    )
    gateway.conn = _agent(conn)
    gateway.alias_store.save_binding(
        "123",
        ChatBinding(
            active_session_id="session-1",
            aliases={"named": "session-1"},
            available_mode_ids=["ask"],
            current_mode_id="ask",
            streaming_enabled=False,
        ),
    )
    state = gateway._state("123")
    state.current_model_id = "model-a"
    state.available_model_ids = ["model-a", "model-b"]
    state.dynamic_commands = {
        "model": SelectionCommand(
            acp_name="model",
            telegram_name="model",
            description="Set model",
            source="model",
        ),
        "missing": SelectionCommand(
            acp_name="missing",
            telegram_name="missing",
            description="Missing config",
            source="config",
        ),
        "agent": SelectionCommand(
            acp_name="agent",
            telegram_name="agent",
            description="Agent mode",
            source="mode",
        ),
        "tools": SelectionCommand(
            acp_name="tools",
            telegram_name="tools",
            description="List tools",
            hint="now",
            source="command",
        ),
        "1bad": SelectionCommand(
            acp_name="1bad",
            telegram_name="1bad",
            description="Invalid command",
            source="command",
        ),
    }
    state.selection_options = [
        SessionConfigOptionBoolean.model_validate(
            {"id": "web", "name": "Web", "type": "boolean", "currentValue": False}
        )
    ]

    model_message = _FakeMessage(text="/model", chat=_FakeChat(id=123))
    assert await gateway._try_run_dynamic_command(
        chat_key="123",
        session_id="session-1",
        command_name="model",
        argument=None,
        message=_message(model_message),
    )
    missing_config = _FakeMessage(text="/missing", chat=_FakeChat(id=123))
    assert await gateway._try_run_dynamic_command(
        chat_key="123",
        session_id="session-1",
        command_name="missing",
        argument=None,
        message=_message(missing_config),
    )
    state.dynamic_commands["web"] = SelectionCommand(
        acp_name="web",
        telegram_name="web",
        description="Web",
        source="config",
    )
    web_status = _FakeMessage(text="/web", chat=_FakeChat(id=123))
    assert await gateway._try_run_dynamic_command(
        chat_key="123",
        session_id="session-1",
        command_name="web",
        argument=None,
        message=_message(web_status),
    )
    invalid_config = _FakeMessage(text="/web maybe", chat=_FakeChat(id=123))
    assert await gateway._try_run_dynamic_command(
        chat_key="123",
        session_id="session-1",
        command_name="web",
        argument="maybe",
        message=_message(invalid_config),
    )
    valid_config = _FakeMessage(text="/web true", chat=_FakeChat(id=123))
    assert await gateway._try_run_dynamic_command(
        chat_key="123",
        session_id="session-1",
        command_name="web",
        argument="true",
        message=_message(valid_config),
    )
    unavailable_mode = _FakeMessage(text="/agent", chat=_FakeChat(id=123))
    assert await gateway._try_run_dynamic_command(
        chat_key="123",
        session_id="session-1",
        command_name="agent",
        argument=None,
        message=_message(unavailable_mode),
    )
    run_prompt = AsyncMock()
    monkeypatch.setattr(gateway, "_run_prompt_text", run_prompt)
    tools_message = _FakeMessage(text="/tools now", chat=_FakeChat(id=123))
    assert await gateway._try_run_dynamic_command(
        chat_key="123",
        session_id="session-1",
        command_name="tools",
        argument="now",
        message=_message(tools_message),
    )

    assert model_message.replies == ["Model: `model-a`\nAvailable: `model-a, model-b`"]
    assert missing_config.replies == ["Selection is unavailable."]
    assert web_status.replies == ["Web: `False`"]
    assert invalid_config.replies == ["Invalid value for `web`."]
    assert valid_config.replies == ["web: `true`"]
    assert unavailable_mode.replies == ["Mode `agent` is unavailable."]
    assert run_prompt.await_args.kwargs["prompt_text"] == "/tools now"
    state.selection_options = [
        SessionConfigOptionBoolean.model_validate(
            {"id": "web", "name": "Web", "type": "boolean", "currentValue": False}
        )
    ]
    assert gateway._selection_option("123", "missing") is None
    assert gateway._parse_config_value(chat_key="123", config_id="missing", raw="true") is None
    assert gateway._parse_config_value(chat_key="123", config_id="web", raw="off") is False
    assert gateway._parse_config_value(chat_key="123", config_id="web", raw="maybe") is None
    assert TelegramGateway._selection_hint(state.selection_options[0]) == "true|false"
    assert TelegramGateway._selection_hint(object()) is None
    assert TelegramGateway._telegram_command_alias("!!!") == "cmd"
    assert TelegramGateway._telegram_command_alias("123 run") == "cmd_123_run"

    summary = gateway._render_session_summary("123")
    topic_empty = gateway._render_topic_session_summary("456")
    topics_empty = gateway._render_topics_summary(456)
    help_text = gateway._help_text("123")
    commands = gateway._bot_commands()

    assert "Model" in summary
    assert "Aliases" in summary
    assert "Active session: `-`" in topic_empty
    assert topics_empty == "No saved surface bindings for this chat."
    assert "Dynamic commands:" in help_text
    assert all(command.command != "1bad" for command in commands)


@pytest.mark.asyncio
async def test_host_approval_and_handler_edge_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path, enable_host_tools=False))

    with pytest.raises(RequestError, match="disabled"):
        gateway._require_terminal_manager()

    gateway._state("123").current_approval_id = "approval-1"
    gateway.pending_approvals["approval-1"] = PendingApproval(
        approval_id="approval-1",
        session_id="session-1",
        tool_call_id="tool-1",
        tool_title="write file",
        preview_text="preview",
        options=[],
        future=asyncio.get_running_loop().create_future(),
    )
    with pytest.raises(RequestError, match="Approval pending"):
        await gateway._prompt_approval(
            chat_key="123",
            session_id="session-1",
            tool_call_id="tool-2",
            tool_title="write file",
            preview_text="preview",
            options=[],
        )
    gateway.pending_approvals.clear()
    gateway._state("123").current_approval_id = None

    gateway.session_to_chat["session-1"] = "123"
    gateway._state("123").resolved_approval = ResolvedApproval(
        tool_call_id="command-tool",
        tool_title="mcp_host_run_command",
        label="Allow Once",
        message_id=7,
    )
    chat_key, tool_call_id = await gateway._approve_host_command_request(
        session_id="session-1",
        command="python",
        args=["-V"],
        cwd=tmp_path,
    )
    assert (chat_key, tool_call_id) == ("123", "command-tool")

    gateway._state("123").resolved_approval = ResolvedApproval(
        tool_call_id="tool-1",
        tool_title="mcp_host_write_workspace_file",
        label="Deny Once",
        message_id=7,
    )
    assert gateway._reuse_host_tool_approval(chat_key="123", action="Write file") is None
    gateway._state("123").resolved_approval = ResolvedApproval(
        tool_call_id="tool-1",
        tool_title="mcp_host_read_workspace_file",
        label="Allow Once",
        message_id=7,
    )
    assert gateway._reuse_host_tool_approval(chat_key="123", action="Write file") is None
    assert TelegramGateway._host_tool_titles_for_action("Read file") == {
        "Read file",
        "mcp_host_read_workspace_file",
    }
    assert TelegramGateway._host_tool_titles_for_action("Execute command") == {
        "Execute command",
        "mcp_host_run_command",
    }
    assert TelegramGateway._host_tool_titles_for_action("Other") == {"Other"}

    gateway._suppress_request_error_logging(SimpleNamespace())
    gateway._suppress_request_error_logging(
        SimpleNamespace(_conn=SimpleNamespace(_tasks=SimpleNamespace(_error_handlers=object())))
    )
    raw = SimpleNamespace(_tasks=SimpleNamespace(_error_handlers=[object()]))
    gateway._suppress_request_error_logging(SimpleNamespace(_conn=raw))
    assert raw._acprouter_request_errors_filtered is True
    gateway._handlers_bound = True
    gateway._bind_handlers()


@pytest.mark.asyncio
async def test_media_edge_paths_and_download_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))

    assert TelegramGateway._normalize_prompt_media_name("MessageMediaType.PHOTO") == "photo"
    assert TelegramGateway._normalize_prompt_media_name("unknown") is None
    assert (
        await gateway._append_message_media_blocks(
            [],
            _message(_FakeMessage(text=None, chat=_FakeChat(id=123), media="photo")),
        )
        is False
    )
    monkeypatch.setattr(gateway, "_prompt_media_name", lambda message: "photo")
    assert (
        await gateway._append_message_media_blocks(
            [],
            _message(_FakeMessage(text=None, chat=_FakeChat(id=123), media="photo")),
        )
        is False
    )
    monkeypatch.setattr(gateway, "_prompt_media_name", TelegramGateway._prompt_media_name)

    with pytest.raises(RequestError, match="Audio prompts are unsupported"):
        await gateway._append_message_media_blocks(
            [],
            _message(
                _FakeMessage(
                    text=None,
                    chat=_FakeChat(id=123),
                    voice=_FakeMedia(file_name="voice.ogg", data=b"OggS"),
                )
            ),
        )
    with pytest.raises(RequestError, match="Document prompts are unsupported"):
        await gateway._append_message_media_blocks(
            [],
            _message(
                _FakeMessage(
                    text=None,
                    chat=_FakeChat(id=123),
                    document=_FakeMedia(
                        file_name="data.bin",
                        data=b"binary",
                        mime_type="application/octet-stream",
                    ),
                )
            ),
        )

    with pytest.raises(RequestError, match="Image prompts are unsupported"):
        await gateway._append_message_media_blocks(
            [],
            _message(
                _FakeMessage(
                    text=None,
                    chat=_FakeChat(id=123),
                    document=_FakeMedia(file_name="image.png", data=_ONE_PIXEL_PNG),
                )
            ),
        )

    with pytest.raises(RequestError, match="Audio prompts are unsupported"):
        await gateway._append_message_media_blocks(
            [],
            _message(
                _FakeMessage(
                    text=None,
                    chat=_FakeChat(id=123),
                    document=_FakeMedia(file_name="sound.mp3", data=b"ID3abc", mime_type=None),
                )
            ),
        )

    gateway.prompt_capabilities = PromptCapabilities.model_validate({"audio": True})
    audio_blocks: list[_PromptBlock] = []
    assert await gateway._append_message_media_blocks(
        audio_blocks,
        _message(
            _FakeMessage(
                text=None,
                chat=_FakeChat(id=123),
                document=_FakeMedia(file_name="sound.mp3", data=b"ID3abc", mime_type=None),
            )
        ),
    )
    assert audio_blocks[0].type == "audio"

    gateway.prompt_capabilities = PromptCapabilities.model_validate({})
    with pytest.raises(RequestError, match="Video prompts are unsupported"):
        await gateway._append_message_media_blocks(
            [],
            _message(
                _FakeMessage(
                    text=None,
                    chat=_FakeChat(id=123),
                    video=_FakeMedia(file_name="clip.mp4", data=b"0000ftyp"),
                )
            ),
        )
    with pytest.raises(RequestError, match="Animation prompts are unsupported"):
        await gateway._append_message_media_blocks(
            [],
            _message(
                _FakeMessage(
                    text=None,
                    chat=_FakeChat(id=123),
                    animation=_FakeMedia(file_name="clip.mp4", data=b"0000ftyp"),
                )
            ),
        )
    with pytest.raises(RequestError, match="Sticker prompts are unsupported"):
        await gateway._append_message_media_blocks(
            [],
            _message(
                _FakeMessage(
                    text=None,
                    chat=_FakeChat(id=123),
                    sticker=_FakeMedia(
                        file_name="sticker.bin",
                        data=b"binary",
                        mime_type="application/octet-stream",
                    ),
                )
            ),
        )
    with pytest.raises(RequestError, match="Sticker prompts are unsupported"):
        await gateway._append_message_media_blocks(
            [],
            _message(
                _FakeMessage(
                    text=None,
                    chat=_FakeChat(id=123),
                    sticker=_FakeMedia(
                        file_name="sticker.webp", data=b"RIFF", mime_type="image/webp"
                    ),
                )
            ),
        )

    gateway.prompt_capabilities = PromptCapabilities.model_validate({"embeddedContext": True})
    animation_blocks: list[_PromptBlock] = []
    assert await gateway._append_message_media_blocks(
        animation_blocks,
        _message(
            _FakeMessage(
                text=None,
                chat=_FakeChat(id=123),
                animation=_FakeMedia(file_name="clip.mp4", data=b"0000ftyp"),
            )
        ),
    )
    sticker_blocks: list[_PromptBlock] = []
    assert await gateway._append_message_media_blocks(
        sticker_blocks,
        _message(
            _FakeMessage(
                text=None,
                chat=_FakeChat(id=123),
                sticker=_FakeMedia(
                    file_name="sticker.bin",
                    data=b"binary",
                    mime_type="application/octet-stream",
                ),
            )
        ),
    )
    monkeypatch.setattr(gateway, "_prompt_media_name", lambda message: "unknown")
    unknown_message = _FakeMessage(text=None, chat=_FakeChat(id=123))
    unknown_message.unknown = object()
    assert await gateway._append_message_media_blocks([], _message(unknown_message)) is False

    payload = SimpleNamespace(
        data=b"\xff\xfe",
        file_name="broken.txt",
        mime_type="text/plain",
    )
    embedded = gateway._embedded_resource_from_payload(
        _message(_FakeMessage(text=None, chat=_FakeChat(id=123), id=55)),
        payload,
    )
    assert isinstance(embedded.resource, BlobResourceContents)

    monkeypatch.setattr(gateway, "app", object())
    with pytest.raises(RequestError, match="download is unavailable"):
        await gateway._download_media_bytes(
            _message(_FakeMessage(text=None, chat=_FakeChat(id=1))), object()
        )

    class _DownloadApp:
        def __init__(self, responses: list[object]) -> None:
            self.responses = responses

        async def download_media(self, media: object, *, in_memory: bool = False) -> object:
            del media, in_memory
            return self.responses.pop(0)

    class _BadFile:
        name = "bad.txt"

        def read(self) -> str:
            return "not bytes"

    fallback_file = BytesIO(b"fallback")
    fallback_file.name = "fallback.txt"
    gateway.app = cast(Client, _DownloadApp([[], [fallback_file]]))
    assert await gateway._download_media_bytes(
        _message(_FakeMessage(text=None, chat=_FakeChat(id=1))),
        _FakeMedia(file_name="fallback.txt", data=b"fallback"),
    ) == (b"fallback", "fallback.txt")
    gateway.app = cast(Client, _DownloadApp([[], []]))
    with pytest.raises(RequestError, match="download failed"):
        await gateway._download_media_bytes(
            _message(_FakeMessage(text=None, chat=_FakeChat(id=1))),
            _FakeMedia(file_name="missing.txt", data=b""),
        )
    gateway.app = cast(Client, _DownloadApp([object()]))
    with pytest.raises(RequestError, match="download failed"):
        await gateway._download_media_bytes(
            _message(_FakeMessage(text=None, chat=_FakeChat(id=1))),
            _FakeMedia(file_name="bad.txt", data=b""),
        )
    gateway.app = cast(Client, _DownloadApp([_BadFile()]))
    with pytest.raises(RequestError, match="download failed"):
        await gateway._download_media_bytes(
            _message(_FakeMessage(text=None, chat=_FakeChat(id=1))),
            _FakeMedia(file_name="bad.txt", data=b""),
        )


def test_media_type_sniffing_image_normalization_and_capability_sync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_app = _FakeApp()
    monkeypatch.setattr("acprouter.telegram_gateway.Client", lambda *args, **kwargs: fake_app)
    gateway = TelegramGateway.from_settings(_settings(tmp_path))

    assert TelegramGateway._guess_media_type(
        "unknown.nope", fallback="application/octet-stream"
    ) == ("application/octet-stream")
    assert (
        TelegramGateway._media_file_name(SimpleNamespace(file_name=" report.txt "))
        == " report.txt "
    )
    assert TelegramGateway._media_file_name(SimpleNamespace(file_name="")) == "media"
    assert TelegramGateway._sniff_media_type(b"GIF89a") == "image/gif"
    assert TelegramGateway._sniff_media_type(b"RIFFxxxxWEBP") == "image/webp"
    assert TelegramGateway._sniff_media_type(b"BMdata") == "image/bmp"
    assert TelegramGateway._sniff_media_type(b"II*\x00data") == "image/tiff"
    assert TelegramGateway._sniff_media_type(b"OggSdata") == "audio/ogg"
    assert TelegramGateway._sniff_media_type(b"RIFFxxxxWAVE") == "audio/wav"
    assert TelegramGateway._sniff_media_type(b"ID3data") == "audio/mpeg"
    assert TelegramGateway._sniff_media_type(b"xxxxftypdata") == "video/mp4"
    assert TelegramGateway._sniff_media_type(b"\x1a\x45\xdf\xa3data") == "video/webm"

    class _FakeImage:
        mode = "P"

        def __enter__(self) -> _FakeImage:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def getbands(self) -> tuple[str, ...]:
            return ("R", "G", "B")

        def convert(self, mode: str) -> _FakeImage:
            self.mode = mode
            return self

        def save(self, output: BytesIO, *, format: str) -> None:
            assert format == "PNG"
            output.write(b"png")

    image_module = SimpleNamespace(
        UnidentifiedImageError=OSError,
        open=lambda data: _FakeImage(),
    )
    image_ops_module = SimpleNamespace(exif_transpose=lambda image: image)

    def _import_image_module(name: str) -> object:
        return image_module if name == "PIL.Image" else image_ops_module

    monkeypatch.setattr("acprouter.telegram_gateway.importlib.import_module", _import_image_module)
    assert TelegramGateway._normalize_image_bytes(b"raw", "image/bmp") == (b"png", "image/png")
    monkeypatch.setattr("acprouter.telegram_gateway.sys.platform", "linux")
    assert TelegramGateway._normalize_image_bytes_with_sips(b"raw", "image/webp") == (
        b"raw",
        "image/webp",
    )
    monkeypatch.setattr("acprouter.telegram_gateway.sys.platform", "darwin")

    def _fake_run(args: tuple[object, ...], **kwargs: object) -> object:
        del kwargs
        Path(args[-1]).write_bytes(b"sips-png")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("acprouter.telegram_gateway.subprocess.run", _fake_run)
    assert TelegramGateway._normalize_image_bytes_with_sips(b"raw", "image/webp") == (
        b"sips-png",
        "image/png",
    )

    def _raise_tempdir(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OSError("tempdir failed")

    monkeypatch.setattr("acprouter.telegram_gateway.tempfile.TemporaryDirectory", _raise_tempdir)
    assert TelegramGateway._normalize_image_bytes_with_sips(b"raw", "image/webp") == (
        b"raw",
        "image/webp",
    )

    gateway._sync_prompt_capabilities(
        InitializeResponse.model_validate(
            {"protocolVersion": 1, "agentCapabilities": {"promptCapabilities": None}}
        )
    )
    gateway._sync_prompt_capabilities(
        InitializeResponse.model_validate(
            {
                "protocolVersion": 1,
                "agentCapabilities": {"promptCapabilities": {"image": True}},
            }
        )
    )
    assert gateway.prompt_capabilities.image is True
