from __future__ import annotations as _annotations

import asyncio
import base64
import importlib
import io
import json
import logging
import mimetypes
import os
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Protocol, TypeAlias, cast

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.exceptions import RequestError
from acp.interfaces import Agent as AcpAgent
from acp.interfaces import Client as AcpClient
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AudioContentBlock,
    AvailableCommand,
    AvailableCommandInput,
    AvailableCommandsUpdate,
    BlobResourceContents,
    ConfigOptionUpdate,
    CreateTerminalResponse,
    CurrentModeUpdate,
    EmbeddedResourceContentBlock,
    EnvVariable,
    ImageContentBlock,
    InitializeResponse,
    KillTerminalResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    ResourceContentBlock,
    SessionConfigSelectGroup,
    SessionConfigSelectOption,
    TerminalOutputResponse,
    TextContentBlock,
    TextResourceContents,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UnstructuredCommandInput,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)
from pyrogram import filters
from pyrogram.client import Client
from pyrogram.enums import ChatAction, ParseMode
from pyrogram.methods.utilities.idle import idle
from pyrogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputChecklist,
    InputChecklistTask,
    LinkPreviewOptions,
    Message,
)

from .projection import (
    PlanChecklistTask,
    append_text_chunk,
    build_plan_checklist,
    render_approval_preview,
    render_approval_resolution,
    render_plan_update,
    render_selection_surface,
    render_tool_update,
    should_project_tool_update,
)
from .session_aliases import SessionAliasStore
from .settings import AppSettings
from .terminals import TerminalManager
from .types import (
    ChatBinding,
    ChatState,
    PendingApproval,
    ResolvedApproval,
    SelectionCommand,
    SelectionOption,
)
from .workspace import WorkspaceManager

__all__ = ("run_telegram_gateway",)

logger = logging.getLogger(__name__)
_LINK_PREVIEW_DISABLED = LinkPreviewOptions(is_disabled=True)
_DYNAMIC_COMMAND_MAX = 16
_CREATE_SESSION_FIRST_TEXT = "Create a session first with /new or /new <name>."
_DISABLED_HOST_TOOLS_TEXT = (
    "Client-owned host tools are disabled in this ACP Router instance. "
    "Use server-owned tools instead."
)
PromptBlock: TypeAlias = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)
_PROMPT_MEDIA_FIELDS = (
    "photo",
    "voice",
    "audio",
    "document",
    "video",
    "video_note",
    "animation",
    "sticker",
)


class _SendChecklist(Protocol):
    async def __call__(
        self,
        *,
        chat_id: int | str,
        checklist: InputChecklist,
        business_connection_id: str | None = None,
        message_thread_id: int | None = None,
    ) -> Message: ...


class _EditMessageChecklist(Protocol):
    async def __call__(
        self,
        *,
        chat_id: int | str,
        message_id: int,
        checklist: InputChecklist,
        business_connection_id: str | None = None,
    ) -> Message: ...


class _MarkChecklistTasksAsDone(Protocol):
    async def __call__(
        self,
        *,
        chat_id: int | str,
        message_id: int,
        marked_as_done_task_ids: list[int] | None = None,
        marked_as_not_done_task_ids: list[int] | None = None,
    ) -> int: ...


class _AddChecklistTasks(Protocol):
    async def __call__(
        self,
        chat_id: int | str,
        message_id: int,
        tasks: list[InputChecklistTask],
    ) -> int: ...


class _CreateForumTopicResult(Protocol):
    id: int


class _CreateForumTopic(Protocol):
    async def __call__(
        self,
        *,
        chat_id: int | str,
        title: str,
    ) -> _CreateForumTopicResult: ...


class _DownloadMedia(Protocol):
    async def __call__(
        self,
        message: object,
        *,
        in_memory: bool = False,
    ) -> object | None: ...


@dataclass(slots=True, frozen=True)
class _DownloadedMediaPayload:
    data: bytes
    file_name: str
    mime_type: str


@dataclass(slots=True, kw_only=True)
class TelegramGateway(AcpClient):
    app: Client
    settings: AppSettings
    alias_store: SessionAliasStore
    workspace_manager: WorkspaceManager | None = None
    terminal_manager: TerminalManager | None = None
    chat_states: dict[str, ChatState] = field(default_factory=dict)
    session_to_chat: dict[str, str] = field(default_factory=dict)
    pending_approvals: dict[str, PendingApproval] = field(default_factory=dict)
    conn: AcpAgent | None = None
    prompt_capabilities: PromptCapabilities = field(default_factory=PromptCapabilities)
    _handlers_bound: bool = False

    @classmethod
    def from_settings(cls, settings: AppSettings) -> TelegramGateway:
        app = Client(
            settings.telegram_session_name,
            api_id=settings.telegram_api_id,
            api_hash=settings.telegram_api_hash,
            bot_token=settings.telegram_bot_token,
        )
        return cls(
            app=app,
            settings=settings,
            alias_store=SessionAliasStore(path=settings.state_dir / "sessions.json"),
            workspace_manager=(
                WorkspaceManager(root=settings.workspace_root)
                if settings.enable_host_tools
                else None
            ),
            terminal_manager=(
                TerminalManager(default_cwd=settings.workspace_root)
                if settings.enable_host_tools
                else None
            ),
        )

    @classmethod
    def from_acp_agent(
        cls,
        acp: AcpAgent,
        settings: AppSettings,
    ) -> TelegramGateway:
        gateway = cls.from_settings(settings)
        gateway.conn = acp
        acp.on_connect(gateway)
        return gateway

    async def run(self) -> None:
        self._bind_handlers()
        if self.conn is not None:
            await self._run_with_connection(self.conn)
            return
        command, *args = self.settings.acp_command
        async with spawn_agent_process(
            self,
            command,
            *args,
            cwd=self.settings.acp_cwd,
            transport_kwargs={"limit": self.settings.acp_stdio_buffer_limit_bytes},
        ) as (
            conn,
            _process,
        ):
            await self._run_with_connection(conn)

    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,
        tool_call: ToolCallUpdate,
        **kwargs: object,
    ) -> RequestPermissionResponse:
        del kwargs
        chat_key = self._chat_for_session(session_id)
        preview_text = render_approval_preview(tool_call)
        return await self._prompt_approval(
            chat_key=chat_key,
            session_id=session_id,
            tool_call_id=tool_call.tool_call_id,
            tool_title=tool_call.title or "Tool update",
            preview_text=preview_text,
            options=list(options),
        )

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        del kwargs
        chat_key = self._chat_for_session(session_id)
        state = self._state(chat_key)
        if isinstance(update, AgentMessageChunk):
            text = getattr(update.content, "text", None)
            if isinstance(text, str):
                state.agent_text = append_text_chunk(state.agent_text, text)
                if self._streaming_enabled(chat_key) and self._should_stream_response_edit(
                    chat_key
                ):
                    await self._upsert_response_message(chat_key=chat_key, text=state.agent_text)
            return
        if isinstance(update, ToolCallUpdate | ToolCallStart | ToolCallProgress):
            approval_label: str | None = None
            if update.status in {"completed", "failed", "cancelled"}:
                approval = self._consume_resolved_approval(
                    chat_key,
                    tool_call_id=update.tool_call_id,
                )
                approval_label = approval.label if approval is not None else None
                await self._finalize_resolved_approval(
                    chat_key=chat_key,
                    approval=approval,
                    state=update.status,
                )
            if should_project_tool_update(update):
                await self._upsert_tool_message(
                    chat_key=chat_key,
                    tool_call_id=update.tool_call_id,
                    text=render_tool_update(update, approval_label=approval_label),
                )
            return
        if isinstance(update, AgentPlanUpdate):
            await self._upsert_plan_message(chat_key=chat_key, update=update)
            return
        if isinstance(update, AvailableCommandsUpdate):
            self._sync_available_commands(chat_key, update.available_commands)
            await self._refresh_bot_commands()
            return
        if isinstance(update, ConfigOptionUpdate):
            state.selection_options = list(update.config_options)
            self._rebuild_dynamic_commands(chat_key)
            await self._refresh_bot_commands()
            return
        if isinstance(update, CurrentModeUpdate):
            binding = self.alias_store.load_binding(chat_key)
            binding.current_mode_id = update.current_mode_id
            self.alias_store.save_binding(chat_key, binding)
            return

    async def write_text_file(
        self,
        content: str,
        path: str,
        session_id: str,
        **kwargs: object,
    ) -> WriteTextFileResponse:
        del kwargs
        workspace_manager = self._require_workspace_manager()
        resolved_path = self._resolve_guarded_workspace_path(
            workspace_manager=workspace_manager,
            session_id=session_id,
            path=path,
        )
        chat_key, tool_call_id = await self._approve_host_file_request(
            session_id=session_id,
            action="Write file",
            path=path,
            resolved_path=resolved_path,
            preview_content=content,
        )
        try:
            await workspace_manager.write_text_file(session_id, path, content)
        except Exception:
            await self._finalize_host_tool_approval(
                chat_key=chat_key,
                tool_call_id=tool_call_id,
                state="failed",
            )
            raise
        await self._finalize_host_tool_approval(
            chat_key=chat_key,
            tool_call_id=tool_call_id,
            state="completed",
        )
        return WriteTextFileResponse()

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: int | None = None,
        line: int | None = None,
        **kwargs: object,
    ) -> ReadTextFileResponse:
        del kwargs
        workspace_manager = self._require_workspace_manager()
        resolved_path = self._resolve_guarded_workspace_path(
            workspace_manager=workspace_manager,
            session_id=session_id,
            path=path,
        )
        preview_content = None
        if line is not None or limit is not None:
            preview_content = f"line={line or 1}, limit={limit or 'all'}"
        chat_key, tool_call_id = await self._approve_host_file_request(
            session_id=session_id,
            action="Read file",
            path=path,
            resolved_path=resolved_path,
            preview_content=preview_content,
        )
        try:
            response = await workspace_manager.read_text_file(
                session_id,
                path,
                limit=limit,
                line=line,
            )
        except Exception:
            await self._finalize_host_tool_approval(
                chat_key=chat_key,
                tool_call_id=tool_call_id,
                state="failed",
            )
            raise
        await self._finalize_host_tool_approval(
            chat_key=chat_key,
            tool_call_id=tool_call_id,
            state="completed",
        )
        return response

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[EnvVariable] | None = None,
        output_byte_limit: int | None = None,
        **kwargs: object,
    ) -> CreateTerminalResponse:
        del output_byte_limit, kwargs
        workspace_manager = self._require_workspace_manager()
        terminal_manager = self._require_terminal_manager()
        resolved_cwd = self._resolve_guarded_command_cwd(
            workspace_manager=workspace_manager,
            session_id=session_id,
            cwd=cwd,
        )
        chat_key, tool_call_id = await self._approve_host_command_request(
            session_id=session_id,
            command=command,
            args=args,
            cwd=resolved_cwd,
        )
        try:
            response = await terminal_manager.create_terminal(
                command,
                args=args,
                cwd=str(resolved_cwd),
                env=env,
            )
        except Exception:
            await self._finalize_host_tool_approval(
                chat_key=chat_key,
                tool_call_id=tool_call_id,
                state="failed",
            )
            raise
        await self._finalize_host_tool_approval(
            chat_key=chat_key,
            tool_call_id=tool_call_id,
            state="completed",
        )
        return response

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: object
    ) -> TerminalOutputResponse:
        del session_id, kwargs
        terminal_manager = self._require_terminal_manager()
        return await terminal_manager.terminal_output(terminal_id)

    async def release_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: object,
    ) -> ReleaseTerminalResponse:
        del session_id, kwargs
        terminal_manager = self._require_terminal_manager()
        return await terminal_manager.release_terminal(terminal_id)

    async def wait_for_terminal_exit(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: object,
    ) -> WaitForTerminalExitResponse:
        del session_id, kwargs
        terminal_manager = self._require_terminal_manager()
        return await terminal_manager.wait_for_terminal_exit(terminal_id)

    async def kill_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: object,
    ) -> KillTerminalResponse:
        del session_id, kwargs
        terminal_manager = self._require_terminal_manager()
        return await terminal_manager.kill_terminal(terminal_id)

    async def ext_method(self, method: str, params: dict[str, object]) -> dict[str, object]:
        del method, params
        return {}

    async def ext_notification(self, method: str, params: dict[str, object]) -> None:
        del method, params

    def on_connect(self, conn: AcpAgent) -> None:
        self.conn = conn

    async def handle_message(self, _: Client, message: Message) -> None:
        chat = message.chat
        if chat is None:
            return
        chat_id = chat.id
        if chat_id is None:
            return
        chat_key = self._chat_key(chat_id, self._message_thread_id(message))
        state = self._state(chat_key)
        try:
            command = self._parse_command(message.text)
            if command is not None:
                await self._handle_command(message, command)
                return
            prompt_blocks = await self._build_prompt_blocks(message)
            if not prompt_blocks:
                return
            if self._has_pending_approval(chat_key):
                await message.reply("Approval pending. Select an option first.")
                return
            if state.prompt_in_flight:
                await message.reply(
                    "A run is already in progress. Wait for it to finish or use /stop."
                )
                return
            session_id = self._active_session_id(chat_key)
            if session_id is None:
                await message.reply(_CREATE_SESSION_FIRST_TEXT)
                return
            await self._run_prompt(
                chat_key=chat_key,
                chat_id=chat_id,
                session_id=session_id,
                prompt_blocks=prompt_blocks,
            )
        except RequestError as exc:
            await self._report_runtime_error(chat_key=chat_key, error=exc)
        except Exception as exc:
            logger.exception("Telegram message handling failed")
            await self._report_runtime_error(chat_key=chat_key, error=exc)
        finally:
            state.prompt_in_flight = False

    async def handle_callback_query(self, _: Client, query: CallbackQuery) -> None:
        try:
            raw_data = query.data
            data = raw_data.decode() if isinstance(raw_data, bytes) else (raw_data or "")
            if not data.startswith("appr:"):
                await query.answer("Unknown action", show_alert=True)
                return
            _prefix, approval_id, action = data.split(":", 2)
            pending = self.pending_approvals.get(approval_id)
            if pending is None:
                await query.answer("Approval is no longer active.", show_alert=True)
                return
            state = self._state(self._chat_for_session(pending.session_id))
            if action == "cancel":
                payload = {"outcome": {"outcome": "cancelled"}}
                label = "cancelled"
                resolution_state = "cancelled"
            else:
                option = pending.options[int(action)]
                payload = {"outcome": {"outcome": "selected", "optionId": option.option_id}}
                label = option.name
                resolution_state = (
                    "waiting for execution result"
                    if label.lower().startswith("allow")
                    else "rejected"
                )
            pending.future.set_result(RequestPermissionResponse.model_validate(payload))
            self.pending_approvals.pop(approval_id, None)
            if state.current_approval_id == approval_id:
                state.current_approval_id = None
            if action != "cancel" and label.lower().startswith("allow"):
                state.resolved_approval = ResolvedApproval(
                    tool_call_id=pending.tool_call_id,
                    tool_title=pending.tool_title,
                    label=label,
                    message_id=query.message.id if query.message is not None else 0,
                )
            else:
                state.resolved_approval = None
            await query.answer()
            if query.message is not None:
                await query.edit_message_text(
                    render_approval_resolution(
                        tool_title=pending.tool_title,
                        selected_option=label,
                        state=resolution_state,
                    ),
                    parse_mode=ParseMode.HTML,
                    link_preview_options=_LINK_PREVIEW_DISABLED,
                )
        except Exception as exc:
            logger.exception("Telegram callback handling failed")
            if (
                query.message is not None
                and query.message.chat is not None
                and query.message.chat.id is not None
            ):
                await self._report_runtime_error(
                    chat_key=self._chat_key(
                        query.message.chat.id,
                        self._message_thread_id(query.message),
                    ),
                    error=exc,
                )
            with suppress(Exception):
                await query.answer("Action failed", show_alert=True)

    async def _handle_command(self, message: Message, command: list[str]) -> None:
        chat = message.chat
        if chat is None:
            return
        chat_id = chat.id
        if chat_id is None:
            return
        name = command[0].lower()
        args = command[1:]
        chat_key = self._chat_key(chat_id, self._message_thread_id(message))
        if self._has_pending_approval(chat_key) and name not in {
            "help",
            "start",
            "stop",
        }:
            await message.reply("Approval pending. Select an option first.")
            return
        if name in {"help", "start"}:
            await message.reply(self._help_text(chat_key))
            return
        if name == "new_topic":
            forum_guard = self._forum_command_guard(chat)
            if forum_guard is not None:
                await message.reply(forum_guard)
                return
            title = " ".join(args).strip()
            if title == "":
                await message.reply("Usage: /new_topic <title>")
                return
            session_id = await self._create_topic_session(chat_id=chat_id, title=title)
            await message.reply(f"Created topic `{title}` and bound it to session: `{session_id}`")
            return
        if name == "bind":
            if not args:
                await message.reply("Usage: /bind <session-id-or-alias>")
                return
            session_id = await self._load_named_session(chat_key=chat_key, name=args[0])
            await message.reply(f"Bound this surface to session: `{session_id}`")
            await self._send_projection_message(
                chat_key=chat_key, text=self._render_selection_surface(chat_key)
            )
            return
        if name == "unbind":
            unbound_session_id = self._unbind_chat_key(chat_key)
            if unbound_session_id is None:
                await message.reply("No active session is bound here.")
                return
            await message.reply(f"Unbound this surface from session: `{unbound_session_id}`")
            return
        if name == "repair":
            repaired = await self._repair_chat_binding(
                chat_key=chat_key, name=args[0] if args else None
            )
            await message.reply(repaired)
            return
        if name == "topic_session":
            await message.reply(self._render_topic_session_summary(chat_key))
            return
        if name == "topics":
            await message.reply(self._render_topics_summary(chat_id))
            return
        if name == "mode":
            if not args:
                await message.reply("Usage: /mode <mode-id>")
                return
            session_id = self._active_session_id(chat_key)
            if session_id is None:
                await message.reply(_CREATE_SESSION_FIRST_TEXT)
                return
            if await self._try_set_mode(chat_key=chat_key, session_id=session_id, mode_id=args[0]):
                await message.reply(f"Mode: `{args[0]}`")
                return
            await message.reply(f"Mode `{args[0]}` is unavailable.")
            return
        if name == "stop":
            binding = self.alias_store.load_binding(chat_key)
            if binding.active_session_id is None:
                await message.reply("No active session.")
                return
            assert self.conn is not None
            await self.conn.cancel(binding.active_session_id)
            self._state(chat_key).prompt_in_flight = False
            await self._upsert_status_message(chat_key=chat_key, text="Run cancelled.")
            return
        if name == "snapshot":
            await self._send_snapshot(chat_key=chat_key, chat_id=chat_id)
            return
        if name == "streaming":
            if not args:
                enabled = self._streaming_enabled(chat_key)
                await message.reply(f"Streaming: `{str(enabled).lower()}`")
                return
            parsed = self._parse_toggle_argument(args[0])
            if parsed is None:
                await message.reply("Usage: /streaming <true|false>")
                return
            binding = self.alias_store.load_binding(chat_key)
            binding.streaming_enabled = parsed
            self.alias_store.save_binding(chat_key, binding)
            if parsed:
                await message.reply(
                    "Streaming enabled. Agent replies will update incrementally with throttled Telegram edits."
                )
                return
            await message.reply(
                "Streaming disabled. ACP Router will buffer agent chunks and send the final reply once the run completes."
            )
            return
        if name in {"new", "session"}:
            if name == "session" and not args:
                await message.reply(self._render_session_summary(chat_key))
                return
            alias = args[0] if args else None
            if alias == "auto":
                alias = None
            session_id = await self._ensure_session(chat_key=chat_key, alias=alias, create_new=True)
            await message.reply(f"Active session: `{session_id}`")
            await self._send_projection_message(
                chat_key=chat_key, text=self._render_selection_surface(chat_key)
            )
            return
        if name in {"switch", "load_session"}:
            if not args:
                usage = (
                    "/switch <session-id-or-alias>"
                    if name == "switch"
                    else "Usage: /load_session <session-id-or-alias>"
                )
                await message.reply(usage)
                return
            session_id = await self._load_named_session(chat_key=chat_key, name=args[0])
            await message.reply(f"Loaded session: `{session_id}`")
            await self._send_projection_message(
                chat_key=chat_key, text=self._render_selection_surface(chat_key)
            )
            return
        if name in {"sessions", "list_sessions"}:
            assert self.conn is not None
            response = await self.conn.list_sessions(cwd=str(self.settings.workspace_root))
            lines = ["Sessions:"]
            for session in response.sessions:
                aliases = ", ".join(self.alias_store.aliases_for_session(session.session_id)) or "-"
                lines.append(
                    f"- {session.session_id} | title={session.title or '-'} | aliases={aliases}"
                )
            await message.reply("\n".join(lines))
            return
        session_id = self._active_session_id(chat_key)
        if session_id is None:
            await message.reply(_CREATE_SESSION_FIRST_TEXT)
            return
        if await self._try_run_dynamic_command(
            chat_key=chat_key,
            session_id=session_id,
            command_name=name,
            argument=" ".join(args).strip() or None,
            message=message,
        ):
            return
        if await self._try_set_mode(chat_key=chat_key, session_id=session_id, mode_id=name):
            await message.reply(f"Mode: `{name}`")
            return
        await message.reply("Unknown command.")

    async def _ensure_session(self, *, chat_key: str, alias: str | None, create_new: bool) -> str:
        binding = self.alias_store.load_binding(chat_key)
        assert self.conn is not None
        if alias and alias != "auto":
            mapped = binding.aliases.get(alias)
            if mapped is not None:
                response = await self.conn.load_session(
                    cwd=str(self.settings.workspace_root),
                    session_id=mapped,
                    mcp_servers=[],
                )
                if response is not None:
                    self._sync_binding_modes_from_response(binding, response)
                binding.active_session_id = mapped
                self.alias_store.save_binding(chat_key, binding)
                self.session_to_chat[mapped] = chat_key
                if self.workspace_manager is not None:
                    self.workspace_manager.bind_session(mapped, self.settings.workspace_root)
                return mapped
        if not create_new and binding.active_session_id is not None:
            if not binding.available_mode_ids:
                response = await self.conn.load_session(
                    cwd=str(self.settings.workspace_root),
                    session_id=binding.active_session_id,
                    mcp_servers=[],
                )
                if response is not None:
                    self._sync_binding_modes_from_response(binding, response)
                    self._sync_selection_state_from_response(chat_key, response)
                self.alias_store.save_binding(chat_key, binding)
                if self.workspace_manager is not None:
                    self.workspace_manager.bind_session(
                        binding.active_session_id,
                        self.settings.workspace_root,
                    )
                await self._refresh_bot_commands()
            return binding.active_session_id
        response = await self.conn.new_session(
            cwd=str(self.settings.workspace_root), mcp_servers=[]
        )
        session_id = response.session_id
        binding.active_session_id = session_id
        self._sync_binding_modes_from_response(binding, response)
        self._sync_selection_state_from_response(chat_key, response)
        if alias and alias != "auto":
            binding.aliases[alias] = session_id
        self.alias_store.save_binding(chat_key, binding)
        self.session_to_chat[session_id] = chat_key
        if self.workspace_manager is not None:
            self.workspace_manager.bind_session(session_id, self.settings.workspace_root)
        await self._refresh_bot_commands()
        return session_id

    async def _load_named_session(self, *, chat_key: str, name: str) -> str:
        binding = self.alias_store.load_binding(chat_key)
        session_id = binding.aliases.get(name, name)
        assert self.conn is not None
        response = await self.conn.load_session(
            cwd=str(self.settings.workspace_root),
            session_id=session_id,
            mcp_servers=[],
        )
        binding.active_session_id = session_id
        if response is not None:
            self._sync_binding_modes_from_response(binding, response)
            self._sync_selection_state_from_response(chat_key, response)
        self.alias_store.save_binding(chat_key, binding)
        self.session_to_chat[session_id] = chat_key
        if self.workspace_manager is not None:
            self.workspace_manager.bind_session(session_id, self.settings.workspace_root)
        await self._refresh_bot_commands()
        return session_id

    async def _create_topic_session(self, *, chat_id: int, title: str) -> str:
        create_forum_topic = cast(
            _CreateForumTopic | None,
            getattr(self.app, "create_forum_topic", None),
        )
        if not callable(create_forum_topic):
            raise RequestError(
                400,
                "Forum topics are unavailable.",
                {"reason": "This Telegram client does not expose create_forum_topic()."},
            )
        topic = await create_forum_topic(chat_id=chat_id, title=title)
        topic_id = getattr(topic, "id", None)
        if not isinstance(topic_id, int):
            raise RequestError(
                500,
                "Forum topic creation failed.",
                {"reason": "Telegram did not return a topic id."},
            )
        chat_key = self._chat_key(chat_id, topic_id)
        session_id = await self._ensure_session(chat_key=chat_key, alias=None, create_new=True)
        await self._send_projection_message(
            chat_key=chat_key,
            text=self._render_session_summary(chat_key),
        )
        await self._send_projection_message(
            chat_key=chat_key,
            text=self._render_selection_surface(chat_key),
        )
        return session_id

    @staticmethod
    def _forum_command_guard(chat: object | None) -> str | None:
        if chat is None:
            return "This command requires a forum-enabled supergroup."
        raw_chat_type = getattr(chat, "type", None)
        normalized_chat_type = TelegramGateway._normalize_chat_type(raw_chat_type)
        if normalized_chat_type is not None and normalized_chat_type != "supergroup":
            return "This command requires a forum-enabled supergroup."
        raw_is_forum = getattr(chat, "is_forum", None)
        if raw_is_forum is False:
            return "This supergroup does not have forum topics enabled."
        return None

    @staticmethod
    def _normalize_chat_type(raw_chat_type: object) -> str | None:
        for candidate in (
            getattr(raw_chat_type, "value", None),
            getattr(raw_chat_type, "name", None),
            raw_chat_type,
        ):
            if not isinstance(candidate, str):
                continue
            normalized = candidate.strip().lower()
            if normalized == "":
                continue
            if normalized.endswith(".supergroup"):
                return "supergroup"
            if normalized.endswith(".private"):
                return "private"
            if normalized.endswith(".group"):
                return "group"
            return normalized
        return None

    async def _repair_chat_binding(self, *, chat_key: str, name: str | None) -> str:
        binding = self.alias_store.load_binding(chat_key)
        candidate = name or binding.active_session_id
        if candidate is None:
            return "No active session is bound here."
        assert self.conn is not None
        target_session_id = binding.aliases.get(candidate, candidate)
        try:
            response = await self.conn.load_session(
                cwd=str(self.settings.workspace_root),
                session_id=target_session_id,
                mcp_servers=[],
            )
        except RequestError as exc:
            if "resource not found" not in str(exc).lower():
                raise
            cleared = self._unbind_chat_key(chat_key)
            if cleared is None:
                return "No active session is bound here."
            return f"Cleared stale binding for session: `{cleared}`"
        binding.active_session_id = target_session_id
        if response is not None:
            self._sync_binding_modes_from_response(binding, response)
            self._sync_selection_state_from_response(chat_key, response)
        self.alias_store.save_binding(chat_key, binding)
        self.session_to_chat[target_session_id] = chat_key
        await self._refresh_bot_commands()
        return f"Repaired binding for session: `{target_session_id}`"

    async def _finalize_prompt(self, chat_key: str, response: PromptResponse) -> None:
        if response.stop_reason == "cancelled":
            await self._upsert_status_message(chat_key=chat_key, text="Run cancelled.")
            return
        await self._upsert_status_message(chat_key=chat_key, text="Completed.")

    async def _try_set_mode(self, *, chat_key: str, session_id: str, mode_id: str) -> bool:
        assert self.conn is not None
        binding = self.alias_store.load_binding(chat_key)
        if binding.available_mode_ids and mode_id not in binding.available_mode_ids:
            return False
        try:
            response = await self.conn.set_session_mode(mode_id, session_id)
        except RequestError:
            return False
        binding.current_mode_id = mode_id
        self.alias_store.save_binding(chat_key, binding)
        return response is not None

    @staticmethod
    def _sync_binding_modes_from_response(
        binding: ChatBinding,
        response: NewSessionResponse | LoadSessionResponse,
    ) -> None:
        modes = response.modes
        if modes is None:
            return
        binding.available_mode_ids = [mode.id for mode in modes.available_modes]
        binding.current_mode_id = modes.current_mode_id

    def _sync_selection_state_from_response(
        self,
        chat_key: str,
        response: NewSessionResponse | LoadSessionResponse,
    ) -> None:
        state = self._state(chat_key)
        if response.models is not None:
            state.current_model_id = response.models.current_model_id
            state.available_model_ids = [
                model.model_id for model in response.models.available_models
            ]
        if response.config_options is not None:
            state.selection_options = list(response.config_options)
        self._rebuild_dynamic_commands(chat_key)

    def _reset_prompt_projection(self, chat_key: str) -> None:
        state = self._state(chat_key)
        state.response_message_id = None
        state.response_text = ""
        state.agent_text = ""
        state.last_response_stream_edit_monotonic = None

    async def _upsert_status_message(
        self,
        *,
        chat_key: str,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        chat_id = self._chat_id_for_key(chat_key)
        message_thread_id = self._thread_id_for_key(chat_key)
        state = self._state(chat_key)
        async with state.lock:
            if (
                state.status_message_id is not None
                and state.status_text == text
                and reply_markup is None
            ):
                return
            if state.status_message_id is None:
                message = await self.app.send_message(
                    chat_id,
                    text,
                    reply_markup=reply_markup,
                    message_thread_id=message_thread_id,
                )
                state.status_message_id = message.id
                state.status_text = text
                return
            if reply_markup is None:
                await self.app.edit_message_text(
                    chat_id,
                    state.status_message_id,
                    text,
                )
            else:
                await self.app.edit_message_text(
                    chat_id,
                    state.status_message_id,
                    text,
                    reply_markup=reply_markup,
                )
            state.status_text = text

    async def _upsert_response_message(self, *, chat_key: str, text: str) -> None:
        chat_id = self._chat_id_for_key(chat_key)
        message_thread_id = self._thread_id_for_key(chat_key)
        state = self._state(chat_key)
        async with state.lock:
            if state.response_message_id is not None and state.response_text == text:
                return
            if state.response_message_id is None:
                message = await self.app.send_message(
                    chat_id,
                    text,
                    message_thread_id=message_thread_id,
                )
                state.response_message_id = message.id
                state.response_text = text
                return
            await self.app.edit_message_text(chat_id, state.response_message_id, text)
            state.response_text = text

    async def _upsert_tool_message(
        self,
        *,
        chat_key: str,
        tool_call_id: str,
        text: str,
    ) -> None:
        chat_id = self._chat_id_for_key(chat_key)
        message_thread_id = self._thread_id_for_key(chat_key)
        state = self._state(chat_key)
        async with state.lock:
            existing_message_id = state.tool_message_ids.get(tool_call_id)
            existing_text = state.tool_texts.get(tool_call_id)
            if existing_message_id is not None and existing_text == text:
                return
            if existing_message_id is None:
                message = await self.app.send_message(
                    chat_id,
                    text,
                    message_thread_id=message_thread_id,
                    parse_mode=ParseMode.HTML,
                    link_preview_options=_LINK_PREVIEW_DISABLED,
                )
                state.tool_message_ids[tool_call_id] = message.id
                state.tool_texts[tool_call_id] = text
                return
            await self.app.edit_message_text(
                chat_id,
                existing_message_id,
                text,
                parse_mode=ParseMode.HTML,
                link_preview_options=_LINK_PREVIEW_DISABLED,
            )
            state.tool_texts[tool_call_id] = text

    async def _send_projection_message(self, *, chat_key: str, text: str) -> None:
        chat_id = self._chat_id_for_key(chat_key)
        message_thread_id = self._thread_id_for_key(chat_key)
        await self.app.send_message(
            chat_id,
            text,
            message_thread_id=message_thread_id,
            parse_mode=ParseMode.HTML,
            link_preview_options=_LINK_PREVIEW_DISABLED,
        )

    async def _upsert_plan_message(self, *, chat_key: str, update: AgentPlanUpdate) -> None:
        send_checklist = cast(_SendChecklist | None, getattr(self.app, "send_checklist", None))
        edit_message_checklist = cast(
            _EditMessageChecklist | None,
            getattr(self.app, "edit_message_checklist", None),
        )
        add_checklist_tasks = cast(
            _AddChecklistTasks | None,
            getattr(self.app, "add_checklist_tasks", None),
        )
        mark_checklist_tasks_as_done = cast(
            _MarkChecklistTasksAsDone | None,
            getattr(self.app, "mark_checklist_tasks_as_done", None),
        )
        if not (
            self.settings.telegram_business_connection_id is not None
            and callable(send_checklist)
            and callable(edit_message_checklist)
            and callable(add_checklist_tasks)
            and callable(mark_checklist_tasks_as_done)
        ):
            await self._send_projection_message(chat_key=chat_key, text=render_plan_update(update))
            return

        projection = build_plan_checklist(update)
        chat_id = self._chat_id_for_key(chat_key)
        message_thread_id = self._thread_id_for_key(chat_key)
        state = self._state(chat_key)
        async with state.lock:
            if not projection.tasks:
                if state.plan_message_id is None:
                    await self._send_projection_message(chat_key=chat_key, text="Plan is empty.")
                    return
                plan_message_id = state.plan_message_id
                await self.app.edit_message_text(chat_id, plan_message_id, "Plan is empty.")
                state.plan_message_id = None
                state.plan_task_ids.clear()
                state.plan_task_order.clear()
                state.plan_task_texts.clear()
                return

            task_ids = self._plan_task_ids(state, projection.tasks)
            task_keys = [task.key for task in projection.tasks]
            if state.plan_message_id is None:
                checklist = InputChecklist(
                    title=projection.title,
                    tasks=[
                        InputChecklistTask(id=task_ids[task.key], text=task.text)
                        for task in projection.tasks
                    ],
                )
                message = await send_checklist(
                    chat_id=chat_id,
                    checklist=checklist,
                    business_connection_id=self.settings.telegram_business_connection_id,
                    message_thread_id=message_thread_id,
                )
                state.plan_message_id = message.id
            elif self._can_append_plan_tasks(state, projection.tasks):
                plan_message_id = cast(int, state.plan_message_id)
                new_tasks = projection.tasks[len(state.plan_task_order) :]
                if new_tasks:
                    await add_checklist_tasks(
                        chat_id,
                        plan_message_id,
                        [
                            InputChecklistTask(id=task_ids[task.key], text=task.text)
                            for task in new_tasks
                        ],
                    )
            else:
                plan_message_id = cast(int, state.plan_message_id)
                checklist = InputChecklist(
                    title=projection.title,
                    tasks=[
                        InputChecklistTask(id=task_ids[task.key], text=task.text)
                        for task in projection.tasks
                    ],
                )
                await edit_message_checklist(
                    chat_id=chat_id,
                    message_id=plan_message_id,
                    checklist=checklist,
                    business_connection_id=self.settings.telegram_business_connection_id,
                )
            state.plan_task_ids = task_ids
            state.plan_task_order = task_keys
            state.plan_task_texts = {task.key: task.text for task in projection.tasks}
            done_ids = [task_ids[task.key] for task in projection.tasks if task.is_done]
            not_done_ids = [task_ids[task.key] for task in projection.tasks if not task.is_done]
            if done_ids or not_done_ids:
                plan_message_id = cast(int, state.plan_message_id)
                await mark_checklist_tasks_as_done(
                    chat_id=chat_id,
                    message_id=plan_message_id,
                    marked_as_done_task_ids=done_ids or None,
                    marked_as_not_done_task_ids=not_done_ids or None,
                )

    @staticmethod
    def _plan_task_ids(state: ChatState, tasks: list[PlanChecklistTask]) -> dict[str, int]:
        task_ids: dict[str, int] = {}
        next_task_id = state.next_plan_task_id
        for task in tasks:
            key = task.key
            existing_task_id = state.plan_task_ids.get(key)
            if existing_task_id is not None:
                task_ids[key] = existing_task_id
                continue
            task_ids[key] = next_task_id
            next_task_id += 1
        state.next_plan_task_id = next_task_id
        return task_ids

    @staticmethod
    def _can_append_plan_tasks(state: ChatState, tasks: list[PlanChecklistTask]) -> bool:
        existing_order = state.plan_task_order
        if len(tasks) < len(existing_order):
            return False
        next_keys = [task.key for task in tasks]
        if next_keys[: len(existing_order)] != existing_order:
            return False
        for task in tasks[: len(existing_order)]:
            if state.plan_task_texts.get(task.key) != task.text:
                return False
        return True

    async def _send_snapshot(self, *, chat_key: str, chat_id: int) -> None:
        binding = self.alias_store.load_binding(chat_key)
        state = self._state(chat_key)
        pending = [
            {
                "approval_id": approval.approval_id,
                "session_id": approval.session_id,
                "tool_call_id": approval.tool_call_id,
                "tool_title": approval.tool_title,
                "options": [option.option_id for option in approval.options],
            }
            for approval in self.pending_approvals.values()
            if self.session_to_chat.get(approval.session_id) == chat_key
        ]
        payload = {
            "chat_key": chat_key,
            "binding": {
                "active_session_id": binding.active_session_id,
                "aliases": binding.aliases,
                "available_mode_ids": binding.available_mode_ids,
                "current_mode_id": binding.current_mode_id,
                "streaming_enabled": binding.streaming_enabled,
            },
            "state": {
                "status_message_id": state.status_message_id,
                "response_message_id": state.response_message_id,
                "plan_message_id": state.plan_message_id,
                "plan_task_ids": state.plan_task_ids,
                "plan_task_order": state.plan_task_order,
                "plan_task_texts": state.plan_task_texts,
                "tool_message_ids": state.tool_message_ids,
                "agent_text": state.agent_text,
                "prompt_in_flight": state.prompt_in_flight,
                "current_approval_id": state.current_approval_id,
                "resolved_approval": (
                    {
                        "tool_call_id": state.resolved_approval.tool_call_id,
                        "tool_title": state.resolved_approval.tool_title,
                        "label": state.resolved_approval.label,
                        "message_id": state.resolved_approval.message_id,
                    }
                    if state.resolved_approval is not None
                    else None
                ),
                "current_model_id": state.current_model_id,
                "available_model_ids": state.available_model_ids,
                "selection_options": [
                    option.model_dump(mode="python", by_alias=True)
                    for option in state.selection_options
                ],
                "dynamic_commands": {
                    name: {
                        "acp_name": command.acp_name,
                        "description": command.description,
                        "hint": command.hint,
                        "source": command.source,
                    }
                    for name, command in state.dynamic_commands.items()
                },
            },
            "pending_approvals": pending,
        }
        snapshot = json.dumps(payload, indent=2, sort_keys=True)
        document = io.BytesIO(snapshot.encode("utf-8"))
        document.name = "session-snapshot.json"
        message_thread_id = self._thread_id_for_key(chat_key)
        if message_thread_id is None:
            await self.app.send_document(
                chat_id=chat_id,
                document=document,
                caption="Current session snapshot",
            )
            return
        await self.app.send_document(
            chat_id=chat_id,
            document=document,
            caption="Current session snapshot",
            message_thread_id=message_thread_id,
        )

    def _state(self, chat_key: str) -> ChatState:
        state = self.chat_states.get(chat_key)
        if state is None:
            state = ChatState()
            self.chat_states[chat_key] = state
        return state

    def _active_session_id(self, chat_key: str) -> str | None:
        binding = self.alias_store.load_binding(chat_key)
        return binding.active_session_id

    def _unbind_chat_key(self, chat_key: str) -> str | None:
        binding = self.alias_store.load_binding(chat_key)
        active_session_id = binding.active_session_id
        if active_session_id is None:
            return None
        binding.active_session_id = None
        self.alias_store.save_binding(chat_key, binding)
        if self.session_to_chat.get(active_session_id) == chat_key:
            self.session_to_chat.pop(active_session_id, None)
        return active_session_id

    def _has_pending_approval(self, chat_key: str) -> bool:
        state = self._state(chat_key)
        approval_id = state.current_approval_id
        return approval_id is not None and approval_id in self.pending_approvals

    def _chat_for_session(self, session_id: str) -> str:
        chat_key = self.session_to_chat.get(session_id)
        if chat_key is None:
            raise RuntimeError(f"No Telegram chat binding for session {session_id}")
        return chat_key

    @staticmethod
    def _chat_key(chat_id: int, message_thread_id: int | None = None) -> str:
        if message_thread_id is None:
            return str(chat_id)
        return f"{chat_id}:{message_thread_id}"

    @staticmethod
    def _chat_id_for_key(chat_key: str) -> int:
        raw_chat_id, *_rest = chat_key.split(":", 1)
        return int(raw_chat_id)

    @staticmethod
    def _thread_id_for_key(chat_key: str) -> int | None:
        _raw_chat_id, separator, raw_thread_id = chat_key.partition(":")
        if separator == "":
            return None
        return int(raw_thread_id)

    @staticmethod
    def _message_thread_id(message: Message) -> int | None:
        raw_value = getattr(message, "message_thread_id", None)
        return raw_value if isinstance(raw_value, int) else None

    @staticmethod
    def _parse_command(text: str | None) -> list[str] | None:
        if text is None:
            return None
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        first_line = stripped.splitlines()[0]
        parts = first_line.split()
        command_name = parts[0][1:].split("@", 1)[0]
        if command_name == "":
            return None
        return [command_name, *parts[1:]]

    async def _try_run_dynamic_command(
        self,
        *,
        chat_key: str,
        session_id: str,
        command_name: str,
        argument: str | None,
        message: Message,
    ) -> bool:
        dynamic_commands = {
            command.telegram_name: command for command in self._dynamic_commands_for_chat(chat_key)
        }
        command = dynamic_commands.get(command_name)
        if command is None:
            return False
        assert self.conn is not None
        target_name = command.acp_name
        if command.source == "model":
            if argument is None:
                state = self._state(chat_key)
                current = state.current_model_id or "-"
                available = ", ".join(state.available_model_ids) or "-"
                await message.reply(f"Model: `{current}`\nAvailable: `{available}`")
                return True
            await self.conn.set_session_model(argument, session_id)
            self._state(chat_key).current_model_id = argument
            await message.reply(f"Model: `{argument}`")
            return True
        if command.source == "config":
            if argument is None:
                option = self._selection_option(chat_key, target_name)
                if option is None:
                    await message.reply("Selection is unavailable.")
                    return True
                await message.reply(f"{option.name}: `{getattr(option, 'current_value', '-')}`")
                return True
            parsed_value = self._parse_config_value(
                chat_key=chat_key,
                config_id=target_name,
                raw=argument,
            )
            if parsed_value is None:
                await message.reply(f"Invalid value for `{command_name}`.")
                return True
            response = await self.conn.set_config_option(target_name, session_id, parsed_value)
            if response is not None:
                self._state(chat_key).selection_options = list(response.config_options)
                self._rebuild_dynamic_commands(chat_key)
                await self._refresh_bot_commands()
            await message.reply(f"{command_name}: `{argument}`")
            return True
        if command.source == "mode":
            if await self._try_set_mode(
                chat_key=chat_key,
                session_id=session_id,
                mode_id=target_name,
            ):
                await message.reply(f"Mode: `{target_name}`")
                return True
            await message.reply(f"Mode `{target_name}` is unavailable.")
            return True
        prompt_text = f"/{target_name}"
        if argument is not None:
            prompt_text = f"{prompt_text} {argument}"
        assert message.chat is not None and message.chat.id is not None
        await self._run_prompt_text(
            chat_key=chat_key,
            chat_id=message.chat.id,
            session_id=session_id,
            prompt_text=prompt_text,
        )
        return True

    async def _run_prompt_text(
        self,
        *,
        chat_key: str,
        chat_id: int,
        session_id: str,
        prompt_text: str,
    ) -> None:
        await self._run_prompt(
            chat_key=chat_key,
            chat_id=chat_id,
            session_id=session_id,
            prompt_blocks=[text_block(prompt_text)],
        )

    async def _run_prompt(
        self,
        *,
        chat_key: str,
        chat_id: int,
        session_id: str,
        prompt_blocks: list[PromptBlock],
    ) -> None:
        state = self._state(chat_key)
        state.prompt_in_flight = True
        try:
            self._reset_prompt_projection(chat_key)
            await self.app.send_chat_action(chat_id, ChatAction.TYPING)
            await self._upsert_status_message(chat_key=chat_key, text="Running...")
            assert self.conn is not None
            response = await self.conn.prompt(prompt=prompt_blocks, session_id=session_id)
            if state.agent_text.strip() != "":
                await self._upsert_response_message(chat_key=chat_key, text=state.agent_text)
            await self._finalize_prompt(chat_key, response)
        finally:
            state.prompt_in_flight = False

    def _selection_option(self, chat_key: str, config_id: str) -> SelectionOption | None:
        state = self._state(chat_key)
        for option in state.selection_options:
            if option.id == config_id:
                return option
        return None

    def _parse_config_value(self, *, chat_key: str, config_id: str, raw: str) -> str | bool | None:
        option = self._selection_option(chat_key, config_id)
        if option is None:
            return None
        if getattr(option, "type", None) == "boolean":
            normalized = raw.strip().lower()
            if normalized in {"true", "on", "yes", "1"}:
                return True
            if normalized in {"false", "off", "no", "0"}:
                return False
            return None
        return raw.strip()

    def _dynamic_commands_for_chat(self, chat_key: str) -> list[SelectionCommand]:
        state = self._state(chat_key)
        return sorted(state.dynamic_commands.values(), key=lambda command: command.telegram_name)

    def _global_dynamic_commands(self) -> list[SelectionCommand]:
        merged: dict[str, SelectionCommand] = {}
        for state in self.chat_states.values():
            merged.update(state.dynamic_commands)
        return sorted(merged.values(), key=lambda command: command.telegram_name)

    def _rebuild_dynamic_commands(self, chat_key: str) -> None:
        state = self._state(chat_key)
        binding = self.alias_store.load_binding(chat_key)
        commands: dict[str, SelectionCommand] = dict(state.dynamic_commands)
        for mode_id in binding.available_mode_ids:
            telegram_name = self._telegram_command_alias(mode_id)
            commands[telegram_name] = SelectionCommand(
                acp_name=mode_id,
                telegram_name=telegram_name,
                description=f"Switch the active session into `{mode_id}` mode.",
                source="mode",
            )
        if state.available_model_ids or state.current_model_id is not None:
            commands["model"] = SelectionCommand(
                acp_name="model",
                telegram_name="model",
                description="Show or set the current session model.",
                hint="provider:model",
                source="model",
            )
        for option in state.selection_options:
            telegram_name = self._telegram_command_alias(option.id)
            hint = self._selection_hint(option)
            commands[telegram_name] = SelectionCommand(
                acp_name=option.id,
                telegram_name=telegram_name,
                description=option.description or f"Show or set `{option.name}`.",
                hint=hint,
                source="config",
            )
        state.dynamic_commands = commands

    def _sync_available_commands(
        self,
        chat_key: str,
        available_commands: list[AvailableCommand],
    ) -> None:
        state = self._state(chat_key)
        commands: dict[str, SelectionCommand] = {}
        for command in available_commands:
            telegram_name = self._telegram_command_alias(command.name)
            hint = None
            if command.input is not None:
                hint = command.input.root.hint
            commands[telegram_name] = SelectionCommand(
                acp_name=command.name,
                telegram_name=telegram_name,
                description=command.description,
                hint=hint,
                source="command",
            )
        state.dynamic_commands = commands
        self._rebuild_dynamic_commands(chat_key)

    @staticmethod
    def _selection_hint(option: object) -> str | None:
        option_type = getattr(option, "type", None)
        if option_type == "boolean":
            return "true|false"
        if option_type == "select":
            values = [item.value for item in TelegramGateway._flatten_select_options(option)[:8]]
            return "|".join(values[:8]) or None
        return None

    def _streaming_enabled(self, chat_key: str) -> bool:
        binding = self.alias_store.load_binding(chat_key)
        if binding.streaming_enabled is not None:
            return binding.streaming_enabled
        return self.settings.streaming_default

    @staticmethod
    def _parse_toggle_argument(raw: str) -> bool | None:
        normalized = raw.strip().lower()
        if normalized in {"true", "on", "yes", "1"}:
            return True
        if normalized in {"false", "off", "no", "0"}:
            return False
        return None

    def _should_stream_response_edit(self, chat_key: str) -> bool:
        state = self._state(chat_key)
        if state.response_message_id is None:
            state.last_response_stream_edit_monotonic = time.monotonic()
            return True
        interval = self.settings.streaming_edit_interval_seconds
        if interval <= 0:
            state.last_response_stream_edit_monotonic = time.monotonic()
            return True
        now = time.monotonic()
        previous = state.last_response_stream_edit_monotonic
        if previous is None or now - previous >= interval:
            state.last_response_stream_edit_monotonic = now
            return True
        return False

    @staticmethod
    def _telegram_command_alias(name: str) -> str:
        lowered = name.strip().lower()
        pieces: list[str] = []
        previous_was_separator = False
        for character in lowered:
            if character.isalnum():
                pieces.append(character)
                previous_was_separator = False
                continue
            if not previous_was_separator:
                pieces.append("_")
                previous_was_separator = True
        alias = "".join(pieces).strip("_")
        if alias == "":
            return "cmd"
        if not alias[0].isalpha():
            alias = f"cmd_{alias}"
        return alias[:32]

    @staticmethod
    def _is_command_name(name: str) -> bool:
        return name.replace("_", "").isalnum() and 0 < len(name) <= 32 and name[0].isalpha()

    @staticmethod
    def _is_valid_bot_command_name(name: str) -> bool:
        return TelegramGateway._is_command_name(name) and name.islower()

    async def _refresh_bot_commands(self) -> None:
        with suppress(Exception):
            await self.app.set_bot_commands(self._bot_commands())

    def _render_selection_surface(self, chat_key: str) -> str:
        binding = self.alias_store.load_binding(chat_key)
        state = self._state(chat_key)
        commands = [
            AvailableCommand(
                name=command.telegram_name,
                description=command.description,
                input=(
                    AvailableCommandInput(root=UnstructuredCommandInput(hint=command.hint))
                    if command.hint is not None
                    else None
                ),
            )
            for command in self._dynamic_commands_for_chat(chat_key)
        ]
        return render_selection_surface(
            current_mode_id=binding.current_mode_id,
            current_model_id=state.current_model_id,
            selection_options=state.selection_options,
            commands=commands,
        )

    def _render_session_summary(self, chat_key: str) -> str:
        binding = self.alias_store.load_binding(chat_key)
        state = self._state(chat_key)
        metadata: list[tuple[str, str]] = [("surface", self._surface_label(chat_key))]
        metadata.append(("topic key", chat_key))
        metadata.append(("streaming", str(self._streaming_enabled(chat_key)).lower()))
        metadata.append(
            (
                "plan projection",
                "checklist"
                if self.settings.telegram_business_connection_id is not None
                else "html card",
            )
        )
        session_id = binding.active_session_id or "-"
        metadata.append(("session", session_id))
        if binding.current_mode_id is not None:
            metadata.append(("mode", binding.current_mode_id))
        if state.current_model_id is not None:
            metadata.append(("model", state.current_model_id))
        if binding.aliases:
            metadata.append(("aliases", ", ".join(sorted(binding.aliases))))
        return "\n".join(f"{label.title()}: `{value}`" for label, value in metadata)

    def _render_topic_session_summary(self, chat_key: str) -> str:
        binding = self.alias_store.load_binding(chat_key)
        lines = [
            f"Surface: `{self._surface_label(chat_key)}`",
            f"Topic key: `{chat_key}`",
        ]
        if binding.active_session_id is None:
            lines.append("Active session: `-`")
        else:
            lines.append(f"Active session: `{binding.active_session_id}`")
        if binding.aliases:
            aliases = ", ".join(
                f"`{alias}` -> `{session_id}`"
                for alias, session_id in sorted(binding.aliases.items())
            )
            lines.append(f"Aliases: {aliases}")
        return "\n".join(lines)

    def _render_topics_summary(self, chat_id: int) -> str:
        bindings = self.alias_store.bindings_for_chat(chat_id)
        if not bindings:
            return "No saved surface bindings for this chat."
        lines = ["Surface bindings:"]
        for chat_key, binding in bindings:
            active = binding.active_session_id or "-"
            alias_count = len(binding.aliases)
            lines.append(
                f"- {self._surface_label(chat_key)} | session={active} | aliases={alias_count}"
            )
        return "\n".join(lines)

    def _help_text(self, chat_key: str) -> str:
        lines = [
            "Commands:",
            "/help - show this help",
            "/bind <id-or-alias> - bind this surface to an existing session",
            "/mode <mode-id> - switch the active session mode",
            "/new - create and bind a fresh session",
            "/new <name> - create and bind a named session alias",
            "/new_topic <title> - create a forum topic with a fresh bound session",
            "/repair [id-or-alias] - repair or clear a stale session binding",
            "/session - show the current surface summary",
            "/switch <id-or-alias> - switch to an existing session",
            "/sessions - list ACP sessions and local aliases",
            "/snapshot - send the current chat/session snapshot as JSON",
            "/stop - cancel the current run",
            "/streaming <true|false> - toggle incremental Telegram reply edits",
            "/topic_session - show the current surface binding",
            "/topics - list saved surface bindings in this chat",
            "/unbind - clear the active session from this surface",
            "You can also call a mode directly, for example: /ask, /plan, /agent",
        ]
        dynamic_commands = self._dynamic_commands_for_chat(chat_key)
        if dynamic_commands:
            lines.append("")
            lines.append("Dynamic commands:")
            for command in dynamic_commands[:_DYNAMIC_COMMAND_MAX]:
                suffix = f" {command.hint}" if command.hint is not None else ""
                lines.append(f"/{command.telegram_name}{suffix} - {command.description}")
        return "\n".join(lines)

    def _bot_commands(self) -> list[BotCommand]:
        commands = [
            BotCommand("help", "Show command help"),
            BotCommand("bind", "Bind this surface to an existing session"),
            BotCommand("new", "Create and bind a session"),
            BotCommand("new_topic", "Create a forum topic with a new bound session"),
            BotCommand("repair", "Repair or clear a stale surface binding"),
            BotCommand("session", "Show the current surface summary"),
            BotCommand("switch", "Switch to an existing session"),
            BotCommand("sessions", "List sessions and aliases"),
            BotCommand("snapshot", "Export the current session snapshot"),
            BotCommand("mode", "Switch the active session mode"),
            BotCommand("stop", "Cancel the current run"),
            BotCommand("streaming", "Toggle incremental reply streaming"),
            BotCommand("topic_session", "Show the current surface binding"),
            BotCommand("topics", "List saved surface bindings in this chat"),
            BotCommand("unbind", "Clear the active session from this surface"),
        ]
        seen = {command.command for command in commands}
        for dynamic in self._global_dynamic_commands():
            if dynamic.telegram_name in seen or not self._is_valid_bot_command_name(
                dynamic.telegram_name
            ):
                continue
            commands.append(BotCommand(dynamic.telegram_name, dynamic.description[:256]))
            seen.add(dynamic.telegram_name)
        return commands

    async def _report_runtime_error(self, *, chat_key: str, error: Exception) -> None:
        message = str(error).strip() or error.__class__.__name__
        request_data: object | None = None
        text: str | None = None
        if isinstance(error, RequestError):
            request_data = error.data
        if "resource not found" in message.lower():
            binding = self.alias_store.load_binding(chat_key)
            binding.active_session_id = None
            self.alias_store.save_binding(chat_key, binding)
            text = _CREATE_SESSION_FIRST_TEXT
        elif "chunk is longer than limit" in message.lower():
            text = (
                "ACP connection failed: the agent process wrote an oversized line to stdout.\n"
                "This usually means protocol output is mixed with logs or a very large ACP frame.\n"
                "Keep agent logs on stderr and retry."
            )
        elif isinstance(request_data, dict) and request_data:
            details = ", ".join(f"{key}={value}" for key, value in request_data.items())
            text = f"Runtime error: {message}\n{details}"
        elif text is None:
            text = f"Runtime error: {message}"
        with suppress(Exception):
            await self._upsert_status_message(chat_key=chat_key, text=text)

    @staticmethod
    def _approval_keyboard_rows(
        approval_id: str,
        options: list[PermissionOption],
    ) -> list[list[InlineKeyboardButton]]:
        ordered = TelegramGateway._ordered_approval_options(options)
        rows: list[list[InlineKeyboardButton]] = []
        index_map = {id(option): index for index, option in enumerate(options)}
        for start in range(0, len(ordered), 2):
            row_options = ordered[start : start + 2]
            rows.append(
                [
                    InlineKeyboardButton(
                        TelegramGateway._approval_button_label(option.name),
                        callback_data=f"appr:{approval_id}:{index_map[id(option)]}",
                    )
                    for option in row_options
                ]
            )
        return rows

    @staticmethod
    def _approval_button_label(name: str) -> str:
        lowered = name.strip().lower()
        if "allow" in lowered or "approve" in lowered:
            return f"{name} ✅"
        if "deny" in lowered or "reject" in lowered:
            return f"{name} ❌"
        return name

    @staticmethod
    def _ordered_approval_options(options: list[PermissionOption]) -> list[PermissionOption]:
        canonical_order = {
            "allow once": 0,
            "allow always": 1,
            "deny once": 2,
            "deny always": 3,
        }
        lowered = {option.name.strip().lower() for option in options}
        if len(options) == 4 and lowered == set(canonical_order):
            return sorted(
                options,
                key=lambda option: canonical_order[option.name.strip().lower()],
            )
        return list(options)

    def _consume_resolved_approval(
        self,
        chat_key: str,
        *,
        tool_call_id: str,
    ) -> ResolvedApproval | None:
        state = self._state(chat_key)
        resolved = state.resolved_approval
        if resolved is None or resolved.tool_call_id != tool_call_id:
            return None
        state.resolved_approval = None
        return resolved

    async def _finalize_resolved_approval(
        self,
        *,
        chat_key: str,
        approval: ResolvedApproval | None,
        state: str,
    ) -> None:
        if approval is None or approval.message_id <= 0:
            return
        with suppress(Exception):
            await self.app.edit_message_text(
                self._chat_id_for_key(chat_key),
                approval.message_id,
                render_approval_resolution(
                    tool_title=approval.tool_title,
                    selected_option=approval.label,
                    state=state,
                ),
                parse_mode=ParseMode.HTML,
                link_preview_options=_LINK_PREVIEW_DISABLED,
            )

    def _require_workspace_manager(self) -> WorkspaceManager:
        if self.workspace_manager is None:
            raise RequestError(
                400,
                "Client-owned host tools are disabled.",
                {
                    "reason": _DISABLED_HOST_TOOLS_TEXT,
                },
            )
        return self.workspace_manager

    def _require_terminal_manager(self) -> TerminalManager:
        if self.terminal_manager is None:
            raise RequestError(
                400,
                "Client-owned host tools are disabled.",
                {
                    "reason": _DISABLED_HOST_TOOLS_TEXT,
                },
            )
        return self.terminal_manager

    async def _prompt_approval(
        self,
        *,
        chat_key: str,
        session_id: str,
        tool_call_id: str,
        tool_title: str,
        preview_text: str,
        options: list[PermissionOption],
    ) -> RequestPermissionResponse:
        if self._has_pending_approval(chat_key):
            raise RequestError(
                400,
                "Approval pending.",
                {
                    "reason": "Another approval is already waiting in this Telegram chat.",
                },
            )
        state = self._state(chat_key)
        future: asyncio.Future[RequestPermissionResponse] = (
            asyncio.get_running_loop().create_future()
        )
        approval_id = uuid.uuid4().hex[:12]
        self.pending_approvals[approval_id] = PendingApproval(
            approval_id=approval_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_title=tool_title,
            preview_text=preview_text,
            options=options,
            future=future,
        )
        state.current_approval_id = approval_id
        buttons = self._approval_keyboard_rows(approval_id, options)
        buttons.append([InlineKeyboardButton("Cancel", callback_data=f"appr:{approval_id}:cancel")])
        await self.app.send_message(
            self._chat_id_for_key(chat_key),
            preview_text,
            parse_mode=ParseMode.HTML,
            link_preview_options=_LINK_PREVIEW_DISABLED,
            reply_markup=InlineKeyboardMarkup(buttons),
            message_thread_id=self._thread_id_for_key(chat_key),
        )
        return await future

    @staticmethod
    def _surface_label(chat_key: str) -> str:
        thread_id = TelegramGateway._thread_id_for_key(chat_key)
        if thread_id is None:
            return f"chat:{TelegramGateway._chat_id_for_key(chat_key)}"
        return f"topic:{thread_id}"

    async def _approve_host_file_request(
        self,
        *,
        session_id: str,
        action: str,
        path: str,
        resolved_path: Path,
        preview_content: str | None,
    ) -> tuple[str, str]:
        chat_key = self._chat_for_session(session_id)
        reused_approval = self._reuse_host_tool_approval(chat_key=chat_key, action=action)
        if reused_approval is not None:
            return chat_key, reused_approval.tool_call_id
        tool_call_id = uuid.uuid4().hex[:12]
        response = await self._prompt_approval(
            chat_key=chat_key,
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_title=action,
            preview_text=self._render_host_file_approval(
                action=action,
                path=path,
                resolved_path=resolved_path,
                preview_content=preview_content,
            ),
            options=self._host_tool_permission_options(),
        )
        self._ensure_host_tool_allowed(
            response=response,
            action=action,
            details={
                "path": str(resolved_path),
            },
        )
        return chat_key, tool_call_id

    async def _approve_host_command_request(
        self,
        *,
        session_id: str,
        command: str,
        args: list[str] | None,
        cwd: Path,
    ) -> tuple[str, str]:
        chat_key = self._chat_for_session(session_id)
        reused_approval = self._reuse_host_tool_approval(
            chat_key=chat_key,
            action="Execute command",
        )
        if reused_approval is not None:
            return chat_key, reused_approval.tool_call_id
        tool_call_id = uuid.uuid4().hex[:12]
        command_line = " ".join([command, *(args or [])]).strip()
        response = await self._prompt_approval(
            chat_key=chat_key,
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_title="Execute command",
            preview_text=self._render_host_command_approval(
                command_line=command_line,
                cwd=cwd,
            ),
            options=self._host_tool_permission_options(),
        )
        self._ensure_host_tool_allowed(
            response=response,
            action="Execute command",
            details={
                "cwd": str(cwd),
                "command": command_line,
            },
        )
        return chat_key, tool_call_id

    async def _finalize_host_tool_approval(
        self,
        *,
        chat_key: str,
        tool_call_id: str,
        state: str,
    ) -> None:
        approval = self._consume_resolved_approval(
            chat_key,
            tool_call_id=tool_call_id,
        )
        await self._finalize_resolved_approval(
            chat_key=chat_key,
            approval=approval,
            state=state,
        )

    def _reuse_host_tool_approval(
        self,
        *,
        chat_key: str,
        action: str,
    ) -> ResolvedApproval | None:
        state = self._state(chat_key)
        resolved = state.resolved_approval
        if resolved is None:
            return None
        if resolved.label.strip().lower() != "allow once":
            return None
        if resolved.tool_title not in self._host_tool_titles_for_action(action):
            return None
        return resolved

    @staticmethod
    def _host_tool_titles_for_action(action: str) -> set[str]:
        if action == "Write file":
            return {"Write file", "mcp_host_write_workspace_file"}
        if action == "Read file":
            return {"Read file", "mcp_host_read_workspace_file"}
        if action == "Execute command":
            return {"Execute command", "mcp_host_run_command"}
        return {action}

    @staticmethod
    def _host_tool_permission_options() -> list[PermissionOption]:
        return [
            PermissionOption.model_validate(
                {
                    "kind": "allow_once",
                    "name": "Allow Once",
                    "optionId": "allow_once",
                }
            ),
            PermissionOption.model_validate(
                {
                    "kind": "reject_once",
                    "name": "Deny Once",
                    "optionId": "deny_once",
                }
            ),
        ]

    def _resolve_guarded_workspace_path(
        self,
        *,
        workspace_manager: WorkspaceManager,
        session_id: str,
        path: str,
    ) -> Path:
        try:
            resolved_path = workspace_manager.resolve_path(session_id, path)
        except PermissionError as exc:
            raise RequestError(
                400,
                "File access rejected.",
                {
                    "reason": str(exc),
                    "path": path,
                },
            ) from None
        session_cwd = workspace_manager.session_cwd(session_id).resolve()
        if not self._is_subpath(session_cwd, resolved_path):
            raise RequestError(
                400,
                "File access rejected.",
                {
                    "reason": "Path is outside the active session cwd.",
                    "path": str(resolved_path),
                    "session_cwd": str(session_cwd),
                },
            )
        return resolved_path

    def _resolve_guarded_command_cwd(
        self,
        *,
        workspace_manager: WorkspaceManager,
        session_id: str,
        cwd: str | None,
    ) -> Path:
        session_cwd = workspace_manager.session_cwd(session_id).resolve()
        candidate = cwd or str(session_cwd)
        resolved_cwd = Path(candidate).expanduser().resolve()
        workspace_root = workspace_manager.root.resolve()
        if not self._is_subpath(workspace_root, resolved_cwd):
            raise RequestError(
                400,
                "Command execution rejected.",
                {
                    "reason": "Command cwd escapes the workspace root.",
                    "cwd": str(resolved_cwd),
                    "workspace_root": str(workspace_root),
                },
            )
        if not self._is_subpath(session_cwd, resolved_cwd):
            raise RequestError(
                400,
                "Command execution rejected.",
                {
                    "reason": "Command cwd is outside the active session cwd.",
                    "cwd": str(resolved_cwd),
                    "session_cwd": str(session_cwd),
                },
            )
        return resolved_cwd

    @staticmethod
    def _is_subpath(root: Path, target: Path) -> bool:
        try:
            return os.path.commonpath([str(root), str(target)]) == str(root)
        except ValueError:
            return False

    @staticmethod
    def _ensure_host_tool_allowed(
        *,
        response: RequestPermissionResponse,
        action: str,
        details: dict[str, str],
    ) -> None:
        payload = response.model_dump(mode="python", by_alias=True)
        outcome = payload.get("outcome", {})
        if not isinstance(outcome, dict):
            raise RequestError(
                400,
                f"{action} rejected.",
                {"reason": "Approval response payload was invalid.", **details},
            )
        if outcome.get("outcome") == "cancelled":
            raise RequestError(
                400,
                f"{action} cancelled.",
                {"reason": "The Telegram approval request was cancelled.", **details},
            )
        if outcome.get("optionId") != "allow_once":
            raise RequestError(
                400,
                f"{action} rejected.",
                {"reason": "The Telegram approval request was denied.", **details},
            )

    @staticmethod
    def _render_host_file_approval(
        *,
        action: str,
        path: str,
        resolved_path: Path,
        preview_content: str | None,
    ) -> str:
        code_tag = TelegramGateway._code_block_open_tag(path)
        lines = [
            "<b>Approval required</b>",
            f"<b>tool:</b> {escape(action)}",
            "<b>status:</b> in progress",
            "",
            f"{code_tag}"
            f"# requested path\n{escape(path.strip())}\n\n"
            f"# resolved path\n{escape(str(resolved_path))}"
            + (
                f"\n\n# preview\n{escape(preview_content.strip())}"
                if isinstance(preview_content, str) and preview_content.strip() != ""
                else ""
            )
            + "</pre>",
        ]
        return "\n".join(lines)

    @staticmethod
    def _render_host_command_approval(
        *,
        command_line: str,
        cwd: Path,
    ) -> str:
        return "\n".join(
            [
                "<b>Approval required</b>",
                "<b>tool:</b> Execute command",
                "<b>status:</b> in progress",
                f"<b>cwd:</b> <code>{escape(str(cwd))}</code>",
                "",
                f'<pre language="bash">{escape(command_line)}</pre>',
            ]
        )

    @staticmethod
    def _code_block_open_tag(path: str) -> str:
        suffix = Path(path).suffix.lower()
        language = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".json": "json",
            ".md": "markdown",
            ".html": "html",
            ".css": "css",
            ".sh": "bash",
            ".yml": "yaml",
            ".yaml": "yaml",
        }.get(suffix)
        if language is None:
            return "<pre>"
        return f'<pre language="{escape(language)}">'

    def _suppress_request_error_logging(self, conn: AcpAgent) -> None:
        raw_connection = getattr(conn, "_conn", None)
        if raw_connection is None or getattr(
            raw_connection, "_acprouter_request_errors_filtered", False
        ):
            return
        supervisor = getattr(raw_connection, "_tasks", None)
        error_handlers = getattr(supervisor, "_error_handlers", None)
        if not isinstance(error_handlers, list):
            return
        patched_handlers: list[object] = []
        for handler in error_handlers:
            if callable(handler):

                def _filtered(
                    task: asyncio.Task[object],
                    exc: BaseException,
                    *,
                    original=handler,
                ) -> None:
                    if isinstance(exc, RequestError):
                        return
                    original(task, exc)

                patched_handlers.append(_filtered)
                continue
            patched_handlers.append(handler)
        error_handlers.clear()
        error_handlers.extend(patched_handlers)
        raw_connection._acprouter_request_errors_filtered = True

    def _bind_handlers(self) -> None:
        if self._handlers_bound:
            return
        self.app.on_message(
            (filters.private | filters.group) & (filters.text | filters.caption | filters.media)
        )(self.handle_message)
        self.app.on_callback_query()(self.handle_callback_query)
        self._handlers_bound = True

    async def _run_with_connection(self, conn: AcpAgent) -> None:
        self.conn = conn
        self._suppress_request_error_logging(conn)
        initialize_response = await conn.initialize(protocol_version=PROTOCOL_VERSION)
        self._sync_prompt_capabilities(initialize_response)
        await self.app.start()
        await self.app.set_bot_commands(self._bot_commands())
        try:
            await idle()
        finally:
            with suppress(Exception):
                await self.app.stop()

    async def _build_prompt_blocks(self, message: Message) -> list[PromptBlock]:
        blocks: list[PromptBlock] = []
        self._append_message_prompt_text(blocks, message)
        if await self._append_message_media_blocks(blocks, message):
            return blocks
        reply_to_message = cast(Message | None, getattr(message, "reply_to_message", None))
        if reply_to_message is not None and self._message_has_prompt_media(reply_to_message):
            self._append_message_prompt_text(blocks, reply_to_message)
            await self._append_message_media_blocks(blocks, reply_to_message)
        return blocks

    @staticmethod
    def _append_message_prompt_text(blocks: list[PromptBlock], message: Message) -> None:
        message_text = (message.text or "").strip()
        caption_text = (message.caption or "").strip()
        if message_text != "":
            blocks.append(text_block(message_text))
            return
        if caption_text != "":
            blocks.append(text_block(caption_text))

    @classmethod
    def _message_has_prompt_media(cls, message: Message) -> bool:
        return cls._prompt_media_name(message) is not None

    @classmethod
    def _prompt_media_name(cls, message: Message) -> str | None:
        media_name = cls._normalize_prompt_media_name(getattr(message, "media", None))
        if media_name is not None and getattr(message, media_name, None) is not None:
            return media_name
        for field_name in _PROMPT_MEDIA_FIELDS:
            if getattr(message, field_name, None) is not None:
                return field_name
        return None

    @staticmethod
    def _normalize_prompt_media_name(media: object) -> str | None:
        candidate_values: list[str] = []
        media_value = getattr(media, "value", None)
        media_name = getattr(media, "name", None)
        if isinstance(media_value, str):
            candidate_values.append(media_value)
        if isinstance(media_name, str):
            candidate_values.append(media_name)
        if isinstance(media, str):
            candidate_values.append(media)
        for candidate in candidate_values:
            normalized = candidate.strip().lower().replace("-", "_")
            if normalized.startswith("messagemediatype."):
                normalized = normalized.rsplit(".", 1)[-1]
            if normalized in _PROMPT_MEDIA_FIELDS:
                return normalized
        return None

    async def _append_message_media_blocks(
        self,
        blocks: list[PromptBlock],
        message: Message,
    ) -> bool:
        media_name = self._prompt_media_name(message)
        if media_name is None:
            return False
        media = getattr(message, media_name, None)
        if media is None:
            return False

        if media_name == "photo":
            if not self._prompt_supports_image():
                raise RequestError(
                    400,
                    "Image prompts are unsupported.",
                    {"reason": "This ACP agent did not advertise image prompt support."},
                )
            blocks.append(await self._download_image_block(message, media))
            return True

        if media_name in {"voice", "audio"}:
            if not self._prompt_supports_audio():
                raise RequestError(
                    400,
                    "Audio prompts are unsupported.",
                    {"reason": "This ACP agent did not advertise audio prompt support."},
                )
            blocks.append(await self._download_audio_block(message, media))
            return True

        if media_name == "document":
            payload = await self._download_media_payload(
                message, media, fallback="application/octet-stream"
            )
            if payload.mime_type.startswith("image/"):
                if not self._prompt_supports_image():
                    raise RequestError(
                        400,
                        "Image prompts are unsupported.",
                        {"reason": "This ACP agent did not advertise image prompt support."},
                    )
                blocks.append(self._image_block_from_payload(payload))
                return True
            if payload.mime_type.startswith("audio/"):
                if not self._prompt_supports_audio():
                    raise RequestError(
                        400,
                        "Audio prompts are unsupported.",
                        {"reason": "This ACP agent did not advertise audio prompt support."},
                    )
                blocks.append(self._audio_block_from_payload(payload))
                return True
            if not self._prompt_supports_embedded_context():
                raise RequestError(
                    400,
                    "Document prompts are unsupported.",
                    {"reason": "This ACP agent did not advertise embedded context support."},
                )
            blocks.append(self._embedded_resource_from_payload(message, payload))
            return True

        if media_name in {"video", "video_note"}:
            if not self._prompt_supports_embedded_context():
                raise RequestError(
                    400,
                    "Video prompts are unsupported.",
                    {"reason": "This ACP agent did not advertise embedded context support."},
                )
            blocks.append(
                await self._download_embedded_resource(message, media, fallback_mime="video/mp4")
            )
            return True

        if media_name == "animation":
            if not self._prompt_supports_embedded_context():
                raise RequestError(
                    400,
                    "Animation prompts are unsupported.",
                    {"reason": "This ACP agent did not advertise embedded context support."},
                )
            blocks.append(
                await self._download_embedded_resource(message, media, fallback_mime="video/mp4")
            )
            return True

        if media_name == "sticker":
            payload = await self._download_media_payload(message, media, fallback="image/webp")
            if payload.mime_type.startswith("image/"):
                if not self._prompt_supports_image():
                    raise RequestError(
                        400,
                        "Sticker prompts are unsupported.",
                        {"reason": "This ACP agent did not advertise image prompt support."},
                    )
                blocks.append(self._image_block_from_payload(payload))
                return True
            if not self._prompt_supports_embedded_context():
                raise RequestError(
                    400,
                    "Sticker prompts are unsupported.",
                    {"reason": "This ACP agent did not advertise embedded context support."},
                )
            blocks.append(self._embedded_resource_from_payload(message, payload))
            return True
        return False

    async def _download_image_block(self, message: Message, media: object) -> ImageContentBlock:
        payload = await self._download_media_payload(message, media, fallback="image/jpeg")
        return self._image_block_from_payload(payload)

    def _image_block_from_payload(self, payload: _DownloadedMediaPayload) -> ImageContentBlock:
        file_bytes = payload.data
        mime_type = payload.mime_type
        file_bytes, mime_type = self._normalize_image_bytes(file_bytes, mime_type)
        return ImageContentBlock(
            type="image",
            data=base64.b64encode(file_bytes).decode("ascii"),
            mime_type=mime_type,
        )

    async def _download_audio_block(self, message: Message, media: object) -> AudioContentBlock:
        payload = await self._download_media_payload(message, media, fallback="audio/ogg")
        return self._audio_block_from_payload(payload)

    @staticmethod
    def _audio_block_from_payload(payload: _DownloadedMediaPayload) -> AudioContentBlock:
        return AudioContentBlock(
            type="audio",
            data=base64.b64encode(payload.data).decode("ascii"),
            mime_type=payload.mime_type,
        )

    async def _download_embedded_resource(
        self,
        message: Message,
        media: object,
        *,
        fallback_mime: str,
    ) -> EmbeddedResourceContentBlock:
        payload = await self._download_media_payload(message, media, fallback=fallback_mime)
        return self._embedded_resource_from_payload(message, payload)

    def _embedded_resource_from_payload(
        self,
        message: Message,
        payload: _DownloadedMediaPayload,
    ) -> EmbeddedResourceContentBlock:
        file_bytes = payload.data
        file_name = payload.file_name
        mime_type = payload.mime_type
        chat = message.chat
        chat_id = chat.id if chat is not None and chat.id is not None else 0
        message_id = getattr(message, "id", 0)
        uri = f"telegram://chat/{chat_id}/message/{message_id}/{file_name}"
        if not self._is_textual_media_type(mime_type):
            return EmbeddedResourceContentBlock(
                type="resource",
                resource=BlobResourceContents(
                    uri=uri,
                    blob=base64.b64encode(file_bytes).decode("ascii"),
                    mime_type=mime_type,
                ),
            )
        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return EmbeddedResourceContentBlock(
                type="resource",
                resource=BlobResourceContents(
                    uri=uri,
                    blob=base64.b64encode(file_bytes).decode("ascii"),
                    mime_type=mime_type,
                ),
            )
        return EmbeddedResourceContentBlock(
            type="resource",
            resource=TextResourceContents(
                uri=uri,
                text=text,
                mime_type=mime_type,
            ),
        )

    async def _download_media_payload(
        self,
        message: Message,
        media: object,
        *,
        fallback: str,
    ) -> _DownloadedMediaPayload:
        file_bytes, file_name = await self._download_media_bytes(message, media)
        mime_type = self._resolve_downloaded_media_type(
            media,
            file_bytes,
            file_name,
            fallback=fallback,
        )
        return _DownloadedMediaPayload(
            data=file_bytes,
            file_name=file_name,
            mime_type=mime_type,
        )

    async def _download_media_bytes(self, message: Message, media: object) -> tuple[bytes, str]:
        download_media = cast(_DownloadMedia | None, getattr(self.app, "download_media", None))
        if not callable(download_media):
            raise RequestError(
                500,
                "Telegram media download is unavailable.",
                {"reason": "This Telegram client does not expose download_media()."},
            )
        downloaded = await download_media(message, in_memory=True)
        if isinstance(downloaded, list):
            downloaded = downloaded[0] if downloaded else None
        if downloaded is None:
            downloaded = await download_media(media, in_memory=True)
            if isinstance(downloaded, list):
                downloaded = downloaded[0] if downloaded else None
        if downloaded is None:
            raise RequestError(
                500,
                "Telegram media download failed.",
                {"reason": "download_media() returned no content."},
            )
        read = getattr(downloaded, "read", None)
        file_name = getattr(downloaded, "name", None)
        if not callable(read) or not isinstance(file_name, str) or file_name.strip() == "":
            raise RequestError(
                500,
                "Telegram media download failed.",
                {"reason": "download_media() did not return a valid in-memory file."},
            )
        file_bytes = read()
        seek = getattr(downloaded, "seek", None)
        if callable(seek):
            seek(0)
        if not isinstance(file_bytes, bytes):
            raise RequestError(
                500,
                "Telegram media download failed.",
                {"reason": "download_media() did not return bytes."},
            )
        return file_bytes, file_name

    @staticmethod
    def _guess_media_type(file_name: str, *, fallback: str) -> str:
        normalized_name = file_name.lower()
        known_suffixes = {
            ".csv": "text/csv",
            ".html": "text/html",
            ".js": "application/javascript",
            ".json": "application/json",
            ".md": "text/markdown",
            ".py": "text/x-python",
            ".sh": "text/x-shellscript",
            ".toml": "application/toml",
            ".ts": "text/plain",
            ".tsx": "text/plain",
            ".txt": "text/plain",
            ".xml": "application/xml",
            ".yaml": "application/yaml",
            ".yml": "application/yaml",
        }
        for suffix, mime_type in known_suffixes.items():
            if normalized_name.endswith(suffix):
                return mime_type
        guessed, _encoding = mimetypes.guess_type(file_name)
        if guessed is None:
            return fallback
        return guessed

    @staticmethod
    def _is_textual_media_type(mime_type: str) -> bool:
        if mime_type.startswith("text/"):
            return True
        return mime_type.startswith("application/") and (
            mime_type.endswith("+json")
            or mime_type.endswith("+xml")
            or mime_type
            in {
                "application/javascript",
                "application/json",
                "application/toml",
                "application/xml",
                "application/yaml",
            }
        )

    @staticmethod
    def _media_file_name(media: object) -> str:
        file_name = getattr(media, "file_name", None)
        if isinstance(file_name, str) and file_name.strip() != "":
            return file_name
        return "media"

    @classmethod
    def _resolve_media_type(cls, media: object, file_name: str, *, fallback: str) -> str:
        mime_type = getattr(media, "mime_type", None)
        if isinstance(mime_type, str) and mime_type.strip() != "":
            return mime_type
        return cls._guess_media_type(file_name, fallback=fallback)

    @classmethod
    def _resolve_downloaded_media_type(
        cls,
        media: object,
        file_bytes: bytes,
        file_name: str,
        *,
        fallback: str,
    ) -> str:
        sniffed_mime_type = cls._sniff_media_type(file_bytes)
        if sniffed_mime_type is not None:
            return sniffed_mime_type
        return cls._resolve_media_type(media, file_name, fallback=fallback)

    @staticmethod
    def _sniff_media_type(file_bytes: bytes) -> str | None:
        if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if file_bytes.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if file_bytes.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(file_bytes) >= 12 and file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
            return "image/webp"
        if file_bytes.startswith(b"BM"):
            return "image/bmp"
        if file_bytes.startswith((b"II*\x00", b"MM\x00*")):
            return "image/tiff"
        if file_bytes.startswith(b"%PDF-"):
            return "application/pdf"
        if file_bytes.startswith(b"OggS"):
            return "audio/ogg"
        if len(file_bytes) >= 12 and file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WAVE":
            return "audio/wav"
        if file_bytes.startswith(b"ID3"):
            return "audio/mpeg"
        if len(file_bytes) >= 8 and file_bytes[4:8] == b"ftyp":
            return "video/mp4"
        if file_bytes.startswith(b"\x1a\x45\xdf\xa3"):
            return "video/webm"
        return None

    @staticmethod
    def _normalize_image_bytes(file_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
        try:
            image_module = importlib.import_module("PIL.Image")
            image_ops_module = importlib.import_module("PIL.ImageOps")
            unidentified_error = cast(type[Exception], image_module.UnidentifiedImageError)
        except (ImportError, AttributeError):
            return TelegramGateway._normalize_image_bytes_with_sips(file_bytes, mime_type)
        try:
            with image_module.open(io.BytesIO(file_bytes)) as image:
                normalized = image_ops_module.exif_transpose(image)
                if normalized.mode not in ("RGB", "RGBA"):
                    normalized = normalized.convert(
                        "RGBA" if "A" in normalized.getbands() else "RGB"
                    )
                output = io.BytesIO()
                normalized.save(output, format="PNG")
                return output.getvalue(), "image/png"
        except (OSError, ValueError, unidentified_error):
            return TelegramGateway._normalize_image_bytes_with_sips(file_bytes, mime_type)

    @staticmethod
    def _normalize_image_bytes_with_sips(file_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
        if sys.platform != "darwin":
            return file_bytes, mime_type
        input_suffix = TelegramGateway._image_suffix_for_mime_type(mime_type)
        try:
            with tempfile.TemporaryDirectory(prefix="acprouter-image-") as temp_dir:
                input_path = Path(temp_dir) / f"input{input_suffix}"
                output_path = Path(temp_dir) / "output.png"
                input_path.write_bytes(file_bytes)
                completed = subprocess.run(
                    (
                        "/usr/bin/sips",
                        "-s",
                        "format",
                        "png",
                        str(input_path),
                        "--out",
                        str(output_path),
                    ),
                    capture_output=True,
                    check=False,
                    text=False,
                )
                if completed.returncode != 0 or not output_path.is_file():
                    return file_bytes, mime_type
                return output_path.read_bytes(), "image/png"
        except (OSError, ValueError):
            return file_bytes, mime_type

    @staticmethod
    def _image_suffix_for_mime_type(mime_type: str) -> str:
        suffixes = {
            "image/bmp": ".bmp",
            "image/gif": ".gif",
            "image/heic": ".heic",
            "image/heif": ".heif",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/tiff": ".tiff",
            "image/webp": ".webp",
        }
        return suffixes.get(mime_type.lower(), ".img")

    def _sync_prompt_capabilities(self, response: InitializeResponse | None) -> None:
        if response is None or response.agent_capabilities is None:
            return
        prompt_capabilities = response.agent_capabilities.prompt_capabilities
        if prompt_capabilities is None:
            return
        self.prompt_capabilities = prompt_capabilities

    def _prompt_supports_image(self) -> bool:
        return bool(self.prompt_capabilities.image)

    def _prompt_supports_audio(self) -> bool:
        return bool(self.prompt_capabilities.audio)

    def _prompt_supports_embedded_context(self) -> bool:
        return bool(self.prompt_capabilities.embedded_context)

    @staticmethod
    def _flatten_select_options(
        option: object,
    ) -> list[SessionConfigSelectOption]:
        raw_options = getattr(option, "options", [])
        flattened: list[SessionConfigSelectOption] = []
        for item in raw_options:
            if isinstance(item, SessionConfigSelectOption):
                flattened.append(item)
                continue
            if isinstance(item, SessionConfigSelectGroup):
                flattened.extend(item.options)
        return flattened


async def run_telegram_gateway(settings: AppSettings) -> None:
    gateway = TelegramGateway.from_settings(settings)
    await gateway.run()
