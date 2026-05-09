from __future__ import annotations as _annotations

import asyncio
import os
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from acp.schema import (
    CreateTerminalResponse,
    EnvVariable,
    KillTerminalResponse,
    ReleaseTerminalResponse,
    TerminalExitStatus,
    TerminalOutputResponse,
    WaitForTerminalExitResponse,
)

__all__ = ("TerminalManager",)

_BUFFER_LIMIT: Final[int] = 200_000


@dataclass(slots=True, kw_only=True)
class _TerminalHandle:
    process: asyncio.subprocess.Process
    command_line: str
    cwd: Path
    output: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    reader_task: asyncio.Task[None] | None = None
    completion_reported: bool = False


@dataclass(slots=True, frozen=True, kw_only=True)
class TerminalSnapshot:
    terminal_id: str
    command_line: str
    cwd: Path
    output: str
    truncated: bool
    exit_code: int | None
    signal: str | None


@dataclass(slots=True, kw_only=True)
class TerminalManager:
    default_cwd: Path
    _handles: dict[str, _TerminalHandle] = field(default_factory=dict)

    async def create_terminal(
        self,
        command: str,
        *,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[EnvVariable] | None = None,
    ) -> CreateTerminalResponse:
        terminal_id = uuid.uuid4().hex
        resolved_cwd = Path(cwd).expanduser().resolve() if cwd is not None else self.default_cwd
        command_line = " ".join([command, *(args or [])]).strip()
        process = await asyncio.create_subprocess_exec(
            command,
            *(args or []),
            cwd=str(resolved_cwd),
            env=self._build_env(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        handle = _TerminalHandle(process=process, command_line=command_line, cwd=resolved_cwd)
        if process.stdout is not None:
            handle.reader_task = asyncio.create_task(self._capture_output(handle, process.stdout))
        self._handles[terminal_id] = handle
        return CreateTerminalResponse(terminal_id=terminal_id)

    async def terminal_output(self, terminal_id: str) -> TerminalOutputResponse:
        handle = self._handles[terminal_id]
        exit_status = None
        if handle.process.returncode is not None:
            if handle.process.returncode < 0:
                exit_status = TerminalExitStatus(
                    signal=signal.Signals(-handle.process.returncode).name
                )
            else:
                exit_status = TerminalExitStatus(exit_code=handle.process.returncode)
        return TerminalOutputResponse(
            output=handle.output.decode("utf-8", errors="replace"),
            truncated=handle.truncated,
            exit_status=exit_status,
        )

    async def wait_for_terminal_exit(self, terminal_id: str) -> WaitForTerminalExitResponse:
        handle = self._handles[terminal_id]
        code = await handle.process.wait()
        if code < 0:
            return WaitForTerminalExitResponse(signal=signal.Signals(-code).name)
        return WaitForTerminalExitResponse(exit_code=code)

    async def kill_terminal(self, terminal_id: str) -> KillTerminalResponse:
        handle = self._handles[terminal_id]
        if handle.process.returncode is None:
            handle.process.terminate()
            await handle.process.wait()
        return KillTerminalResponse()

    async def release_terminal(self, terminal_id: str) -> ReleaseTerminalResponse:
        handle = self._handles.pop(terminal_id)
        if handle.reader_task is not None:
            await handle.reader_task
        return ReleaseTerminalResponse()

    def terminal_snapshot(self, terminal_id: str) -> TerminalSnapshot:
        handle = self._handles[terminal_id]
        exit_code: int | None = None
        signal_name: str | None = None
        if handle.process.returncode is not None:
            if handle.process.returncode < 0:
                signal_name = signal.Signals(-handle.process.returncode).name
            else:
                exit_code = handle.process.returncode
        return TerminalSnapshot(
            terminal_id=terminal_id,
            command_line=handle.command_line,
            cwd=handle.cwd,
            output=handle.output.decode("utf-8", errors="replace"),
            truncated=handle.truncated,
            exit_code=exit_code,
            signal=signal_name,
        )

    def mark_completion_reported(self, terminal_id: str) -> bool:
        handle = self._handles[terminal_id]
        if handle.completion_reported:
            return False
        handle.completion_reported = True
        return True

    async def _capture_output(
        self,
        handle: _TerminalHandle,
        stream: asyncio.StreamReader,
    ) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            handle.output.extend(chunk)
            if len(handle.output) > _BUFFER_LIMIT:
                del handle.output[:-_BUFFER_LIMIT]
                handle.truncated = True

    def _build_env(self, env: list[EnvVariable] | None) -> dict[str, str]:
        base = dict(os.environ)
        for variable in env or []:
            base[variable.name] = variable.value
        return base
