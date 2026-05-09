from __future__ import annotations as _annotations

import asyncio
import signal
import sys
from pathlib import Path

import pytest
from acp.schema import EnvVariable

from acprouter.terminals import _BUFFER_LIMIT, TerminalManager, _TerminalHandle
from acprouter.workspace import WorkspaceManager


@pytest.mark.asyncio
async def test_workspace_manager_reads_and_writes_relative_to_session_cwd(tmp_path: Path) -> None:
    workspace = WorkspaceManager(root=tmp_path)
    session_cwd = tmp_path / "project"
    workspace.bind_session("session-1", session_cwd)

    await workspace.write_text_file("session-1", "notes/hello.txt", "hello\nworld\n")
    response = await workspace.read_text_file("session-1", "notes/hello.txt")

    assert response.content == "hello\nworld\n"
    assert (session_cwd / "notes" / "hello.txt").read_text(encoding="utf-8") == "hello\nworld\n"


@pytest.mark.asyncio
async def test_workspace_manager_supports_line_windowing(tmp_path: Path) -> None:
    workspace = WorkspaceManager(root=tmp_path)
    workspace.bind_session("session-1", tmp_path)
    path = tmp_path / "README.md"
    path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    response = await workspace.read_text_file("session-1", "README.md", line=2, limit=2)

    assert response.content == "two\nthree"


@pytest.mark.asyncio
async def test_workspace_manager_result_helpers_preserve_paths_and_previous_content(
    tmp_path: Path,
) -> None:
    workspace = WorkspaceManager(root=tmp_path)
    workspace.bind_session("session-1", tmp_path)
    (tmp_path / "main.py").write_text("print('old')\n", encoding="utf-8")

    write_result = await workspace.write_file_result("session-1", "main.py", "print('new')\n")
    read_result = await workspace.read_file_result("session-1", "main.py")

    assert write_result.path == tmp_path / "main.py"
    assert write_result.old_text == "print('old')\n"
    assert write_result.new_text == "print('new')\n"
    assert read_result.path == tmp_path / "main.py"
    assert read_result.content == "print('new')\n"


def test_workspace_manager_defaults_session_cwd_to_root(tmp_path: Path) -> None:
    workspace = WorkspaceManager(root=tmp_path)

    assert workspace.session_cwd("missing-session") == tmp_path
    assert workspace.resolve_path("missing-session", "README.md") == tmp_path / "README.md"


@pytest.mark.asyncio
async def test_workspace_manager_rejects_workspace_escape(tmp_path: Path) -> None:
    workspace = WorkspaceManager(root=tmp_path)
    workspace.bind_session("session-1", tmp_path)

    with pytest.raises(PermissionError, match="Path escapes workspace root"):
        await workspace.read_text_file("session-1", "../outside.txt")


@pytest.mark.asyncio
async def test_terminal_manager_captures_output_and_exit_status(tmp_path: Path) -> None:
    manager = TerminalManager(default_cwd=tmp_path)
    created = await manager.create_terminal(
        sys.executable,
        args=["-c", "print('hello from terminal')"],
        cwd=str(tmp_path),
        env=[EnvVariable(name="ACPROUTER_TEST_ENV", value="1")],
    )

    waited = await manager.wait_for_terminal_exit(created.terminal_id)
    output = await manager.terminal_output(created.terminal_id)
    snapshot = manager.terminal_snapshot(created.terminal_id)
    first_completion = manager.mark_completion_reported(created.terminal_id)
    second_completion = manager.mark_completion_reported(created.terminal_id)
    await manager.release_terminal(created.terminal_id)

    assert waited.exit_code == 0
    assert "hello from terminal" in output.output
    assert output.exit_status is not None
    assert output.exit_status.exit_code == 0
    assert "hello from terminal" in snapshot.output
    assert first_completion is True
    assert second_completion is False


@pytest.mark.asyncio
async def test_terminal_manager_kills_long_running_process(tmp_path: Path) -> None:
    manager = TerminalManager(default_cwd=tmp_path)
    created = await manager.create_terminal(
        sys.executable,
        args=["-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
    )

    await manager.kill_terminal(created.terminal_id)
    waited = await manager.wait_for_terminal_exit(created.terminal_id)
    await manager.release_terminal(created.terminal_id)

    assert waited.exit_code is not None or waited.signal is not None


@pytest.mark.asyncio
async def test_terminal_manager_reports_signal_status_and_truncates_output(tmp_path: Path) -> None:
    manager = TerminalManager(default_cwd=tmp_path)

    class _FakeProcess:
        returncode = -signal.SIGTERM

    handle = _TerminalHandle(
        process=_FakeProcess(),
        command_line="fake",
        cwd=tmp_path,
        output=bytearray(b"hello"),
    )
    manager._handles["term"] = handle

    output = await manager.terminal_output("term")
    snapshot = manager.terminal_snapshot("term")

    assert output.exit_status is not None
    assert output.exit_status.signal == "SIGTERM"
    assert snapshot.signal == "SIGTERM"

    class _FakeStream:
        def __init__(self) -> None:
            self.chunks = [b"x" * (_BUFFER_LIMIT + 10), b""]

        async def read(self, size: int) -> bytes:
            del size
            await asyncio.sleep(0)
            return self.chunks.pop(0)

    await manager._capture_output(handle, _FakeStream())

    assert handle.truncated is True
    assert len(handle.output) == _BUFFER_LIMIT
