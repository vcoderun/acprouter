from __future__ import annotations as _annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from acp.schema import ReadTextFileResponse, WriteTextFileResponse

from .types import SessionWorkspace

__all__ = (
    "FileReadResult",
    "FileWriteResult",
    "WorkspaceManager",
)


@dataclass(slots=True, frozen=True, kw_only=True)
class FileReadResult:
    path: Path
    content: str


@dataclass(slots=True, frozen=True, kw_only=True)
class FileWriteResult:
    path: Path
    old_text: str | None
    new_text: str


@dataclass(slots=True, kw_only=True)
class WorkspaceManager:
    root: Path
    _sessions: dict[str, SessionWorkspace] = field(default_factory=dict)

    def bind_session(self, session_id: str, cwd: Path) -> None:
        self._sessions[session_id] = SessionWorkspace(cwd=cwd)

    def session_cwd(self, session_id: str) -> Path:
        workspace = self._sessions.get(session_id)
        if workspace is None:
            return self.root
        return workspace.cwd

    def resolve_path(self, session_id: str, path: str) -> Path:
        return self._resolve(session_id, path)

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        *,
        limit: int | None = None,
        line: int | None = None,
    ) -> ReadTextFileResponse:
        resolved = self._resolve(session_id, path)
        text = resolved.read_text(encoding="utf-8")
        if line is not None or limit is not None:
            lines = text.splitlines()
            start = max((line or 1) - 1, 0)
            end = start + limit if limit is not None else None
            text = "\n".join(lines[start:end])
        return ReadTextFileResponse(content=text)

    async def read_file_result(
        self,
        session_id: str,
        path: str,
        *,
        limit: int | None = None,
        line: int | None = None,
    ) -> FileReadResult:
        resolved = self._resolve(session_id, path)
        response = await self.read_text_file(session_id, path, limit=limit, line=line)
        return FileReadResult(path=resolved, content=response.content)

    async def write_text_file(
        self, session_id: str, path: str, content: str
    ) -> WriteTextFileResponse:
        resolved = self._resolve(session_id, path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return WriteTextFileResponse()

    async def write_file_result(
        self,
        session_id: str,
        path: str,
        content: str,
    ) -> FileWriteResult:
        resolved = self._resolve(session_id, path)
        old_text = resolved.read_text(encoding="utf-8") if resolved.exists() else None
        await self.write_text_file(session_id, path, content)
        return FileWriteResult(path=resolved, old_text=old_text, new_text=content)

    def _resolve(self, session_id: str, path: str) -> Path:
        cwd = self.session_cwd(session_id)
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        resolved = candidate.resolve()
        root = self.root.resolve()
        common = os.path.commonpath([str(root), str(resolved)])
        if common != str(root):
            raise PermissionError(f"Path escapes workspace root: {path}")
        return resolved
