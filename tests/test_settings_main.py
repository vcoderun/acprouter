from __future__ import annotations as _annotations

import asyncio
import importlib
import runpy
import sys
from pathlib import Path

import pytest

from acprouter.settings import AppSettings

main_module = importlib.import_module("acprouter.__main__")
settings_module = importlib.import_module("acprouter.settings")


def test_app_settings_from_env_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ACPROUTER_SURFACE", raising=False)
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ACPROUTER_COMMAND", "python agent.py")
    monkeypatch.chdir(tmp_path)

    settings = AppSettings.from_env()

    assert settings.telegram_api_id == 1
    assert settings.acp_command == ("python", "agent.py")
    assert settings.workspace_root == tmp_path.resolve()
    assert settings.acp_cwd == tmp_path.resolve()
    assert settings.telegram_business_connection_id is None
    assert settings.enable_host_tools is True
    assert settings.streaming_default is False
    assert settings.streaming_edit_interval_seconds == 1.0


def test_app_settings_from_env_parses_optional_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ACPROUTER_COMMAND", "python agent.py --flag")
    monkeypatch.setenv("ACPROUTER_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("ACPROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ACPROUTER_AGENT_CWD", str(tmp_path / "agent"))
    monkeypatch.setenv("ACPROUTER_TELEGRAM_SESSION", "telegram-test")
    monkeypatch.setenv("ACPROUTER_TELEGRAM_BUSINESS_CONNECTION_ID", "biz-123")
    monkeypatch.setenv("ACPROUTER_STDIO_BUFFER_LIMIT_BYTES", "2048")
    monkeypatch.setenv("ACPROUTER_ENABLE_HOST_TOOLS", "true")
    monkeypatch.setenv("ACPROUTER_STREAMING_DEFAULT", "true")
    monkeypatch.setenv("ACPROUTER_STREAMING_EDIT_INTERVAL_SECONDS", "0.5")

    settings = AppSettings.from_env()

    assert settings.telegram_session_name == "telegram-test"
    assert settings.telegram_business_connection_id == "biz-123"
    assert settings.acp_command == ("python", "agent.py", "--flag")
    assert settings.workspace_root == (tmp_path / "workspace").resolve()
    assert settings.state_dir == (tmp_path / "state").resolve()
    assert settings.acp_cwd == (tmp_path / "agent").resolve()
    assert settings.acp_stdio_buffer_limit_bytes == 2048
    assert settings.enable_host_tools is True
    assert settings.streaming_default is True
    assert settings.streaming_edit_interval_seconds == 0.5


def test_app_settings_rejects_non_telegram_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ACPROUTER_SURFACE", "legacy-chat")
    monkeypatch.setenv("ACPROUTER_COMMAND", "python agent.py")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match="only supports Telegram"):
        AppSettings.from_env()


def test_app_settings_rejects_invalid_boolean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ACPROUTER_COMMAND", "python agent.py")
    monkeypatch.setenv("ACPROUTER_ENABLE_HOST_TOOLS", "maybe")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match="ACPROUTER_ENABLE_HOST_TOOLS"):
        AppSettings.from_env()


def test_app_settings_parses_false_boolean_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ACPROUTER_COMMAND", "python agent.py")
    monkeypatch.setenv("ACPROUTER_ENABLE_HOST_TOOLS", "off")
    monkeypatch.chdir(tmp_path)

    settings = AppSettings.from_env()

    assert settings.enable_host_tools is False


def test_app_settings_rejects_missing_required_telegram_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ACPROUTER_COMMAND", "python agent.py")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match="TELEGRAM_API_HASH"):
        AppSettings.from_env()


def test_app_settings_rejects_empty_split_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings_module, "_required_command_env", lambda: "")

    with pytest.raises(RuntimeError, match="must not be empty"):
        AppSettings.from_env()


def test_app_settings_requires_non_empty_required_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ACPROUTER_COMMAND", "   ")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(
        RuntimeError, match="Missing required environment variable: ACPROUTER_COMMAND"
    ):
        AppSettings.from_env()


def test_app_settings_rejects_unknown_surface(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ACPROUTER_SURFACE", "discord")
    monkeypatch.setenv("ACPROUTER_COMMAND", "python agent.py")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match="ACPROUTER_SURFACE"):
        AppSettings.from_env()


def test_app_settings_supports_legacy_agent_command_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ACPROUTER_AGENT_COMMAND", "python legacy_agent.py")
    monkeypatch.chdir(tmp_path)

    settings = AppSettings.from_env()

    assert settings.acp_command == ("python", "legacy_agent.py")


def test_main_loads_cwd_dotenv_and_runs_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded: dict[str, object] = {}
    real_asyncio_run = asyncio.run
    monkeypatch.chdir(tmp_path)
    settings = AppSettings(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        telegram_session_name="telegram-test",
        telegram_business_connection_id=None,
        acp_command=("python", "agent.py"),
        workspace_root=tmp_path,
        state_dir=tmp_path / ".state",
        acp_cwd=tmp_path,
        acp_stdio_buffer_limit_bytes=2048,
        enable_host_tools=True,
        streaming_default=False,
        streaming_edit_interval_seconds=1.0,
    )

    def _fake_load_dotenv(*, dotenv_path: Path, override: bool) -> None:
        recorded["dotenv_path"] = dotenv_path
        recorded["override"] = override

    async def _fake_run_gateway(passed_settings: object) -> None:
        recorded["settings"] = passed_settings

    monkeypatch.setattr(main_module.dotenv, "load_dotenv", _fake_load_dotenv)
    monkeypatch.setattr(main_module.AppSettings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(main_module, "run_telegram_gateway", _fake_run_gateway)
    monkeypatch.setattr(main_module.asyncio, "run", lambda coro: real_asyncio_run(coro))

    main_module.main()

    assert recorded["dotenv_path"] == tmp_path / ".env"
    assert recorded["override"] is False
    assert recorded["settings"] is settings


def test_main_module_entrypoint_runs_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorded: dict[str, object] = {}
    settings = AppSettings(
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_bot_token="token",
        telegram_session_name="telegram-test",
        telegram_business_connection_id=None,
        acp_command=("python", "agent.py"),
        workspace_root=tmp_path,
        state_dir=tmp_path / ".state",
        acp_cwd=tmp_path,
        acp_stdio_buffer_limit_bytes=2048,
        enable_host_tools=True,
        streaming_default=False,
        streaming_edit_interval_seconds=1.0,
    )

    async def _fake_run_gateway(passed_settings: object) -> None:
        recorded["settings"] = passed_settings

    monkeypatch.setattr(settings_module.AppSettings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr("acprouter.telegram_gateway.run_telegram_gateway", _fake_run_gateway)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delitem(sys.modules, "acprouter.__main__", raising=False)

    runpy.run_module("acprouter.__main__", run_name="__main__")

    assert recorded["settings"] is settings


def test_conftest_inserts_repo_paths_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    src = root / "src"
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p not in {str(root), str(src)}])

    runpy.run_path(str(root / "tests" / "conftest.py"))

    assert str(root) in sys.path
    assert str(src) in sys.path
