from __future__ import annotations as _annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

__all__ = ("AppSettings",)

_DEFAULT_STDIO_BUFFER_LIMIT_BYTES = 50 * 1024 * 1024
_DEFAULT_STREAMING_EDIT_INTERVAL_SECONDS = 1.0


def _required_command_env() -> str:
    preferred = os.environ.get("ACPROUTER_COMMAND")
    if preferred is not None and preferred.strip() != "":
        return preferred
    legacy = os.environ.get("ACPROUTER_AGENT_COMMAND")
    if legacy is not None and legacy.strip() != "":
        return legacy
    raise RuntimeError(
        "Missing required environment variable: ACPROUTER_COMMAND (legacy: ACPROUTER_AGENT_COMMAND)"
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "on", "yes"}:
        return True
    if normalized in {"0", "false", "off", "no"}:
        return False
    raise RuntimeError(f"{name} must be one of: true, false, on, off, 1, 0, yes, no")


def _validate_surface_env() -> None:
    raw = os.environ.get("ACPROUTER_SURFACE")
    if raw is None or raw.strip() == "" or raw.strip().lower() == "telegram":
        return
    raise RuntimeError("ACPROUTER_SURFACE is no longer supported; acprouter only supports Telegram")


@dataclass(slots=True, frozen=True, kw_only=True)
class AppSettings:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_bot_token: str
    telegram_session_name: str
    telegram_business_connection_id: str | None
    acp_command: tuple[str, ...]
    workspace_root: Path
    state_dir: Path
    acp_cwd: Path
    acp_stdio_buffer_limit_bytes: int
    enable_host_tools: bool
    streaming_default: bool
    streaming_edit_interval_seconds: float

    @classmethod
    def from_env(cls) -> AppSettings:
        workspace_root = (
            Path(os.environ.get("ACPROUTER_WORKSPACE_ROOT", os.getcwd())).expanduser().resolve()
        )
        state_dir = Path(
            os.environ.get("ACPROUTER_STATE_DIR", workspace_root / ".acprouter-state")
        ).expanduser()
        acp_cwd = Path(os.environ.get("ACPROUTER_AGENT_CWD", workspace_root)).expanduser().resolve()
        _validate_surface_env()
        command = tuple(shlex.split(_required_command_env()))
        if not command:
            raise RuntimeError("ACPROUTER_COMMAND must not be empty")
        return cls(
            telegram_api_id=int(_required_env("TELEGRAM_API_ID")),
            telegram_api_hash=_required_env("TELEGRAM_API_HASH"),
            telegram_bot_token=_required_env("TELEGRAM_BOT_TOKEN"),
            telegram_session_name=os.environ.get("ACPROUTER_TELEGRAM_SESSION", "acprouter-bot"),
            telegram_business_connection_id=(
                os.environ.get("ACPROUTER_TELEGRAM_BUSINESS_CONNECTION_ID", "").strip() or None
            ),
            acp_command=command,
            workspace_root=workspace_root,
            state_dir=state_dir.resolve(),
            acp_cwd=acp_cwd,
            acp_stdio_buffer_limit_bytes=int(
                os.environ.get(
                    "ACPROUTER_STDIO_BUFFER_LIMIT_BYTES",
                    str(_DEFAULT_STDIO_BUFFER_LIMIT_BYTES),
                )
            ),
            enable_host_tools=_env_bool("ACPROUTER_ENABLE_HOST_TOOLS", default=True),
            streaming_default=_env_bool("ACPROUTER_STREAMING_DEFAULT", default=False),
            streaming_edit_interval_seconds=float(
                os.environ.get(
                    "ACPROUTER_STREAMING_EDIT_INTERVAL_SECONDS",
                    str(_DEFAULT_STREAMING_EDIT_INTERVAL_SECONDS),
                )
            ),
        )
