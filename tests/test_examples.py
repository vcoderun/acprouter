from __future__ import annotations as _annotations

import runpy
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest

import acprouter


class _FakeSettings:
    @staticmethod
    def from_env() -> object:
        return object()


class _FakeTelegramGateway:
    run_calls = 0

    @classmethod
    def from_settings(cls, settings: object) -> _FakeTelegramGateway:
        del settings
        return cls()

    async def run(self) -> None:
        type(self).run_calls += 1


class _FakeAgent:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs
        self.tools: list[object] = []

    def tool_plain(self, func: object) -> object:
        self.tools.append(func)
        return func


class _FakeGateway:
    instances: list[_FakeGateway] = []

    def __init__(self, acp_agent: object, settings: object) -> None:
        self.acp_agent = acp_agent
        self.settings = settings
        self.run_calls = 0
        type(self).instances.append(self)

    @classmethod
    def from_acp_agent(cls, acp_agent: object, settings: object) -> _FakeGateway:
        return cls(acp_agent, settings)

    async def run(self) -> None:
        self.run_calls += 1


class _FakeRemoteServer:
    def __init__(self) -> None:
        self.serve_forever_calls = 0

    async def serve_forever(self) -> None:
        self.serve_forever_calls += 1


def _install_agent_example_modules(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    pydantic_ai = ModuleType("pydantic_ai")
    pydantic_ai.Agent = _FakeAgent
    pydantic_acp = ModuleType("pydantic_acp")
    pydantic_acp.run_acp = Mock()
    pydantic_acp.create_acp_agent = Mock(side_effect=lambda *, agent: {"agent": agent})
    langchain = ModuleType("langchain")
    langchain_agents = ModuleType("langchain.agents")
    langchain_agents.create_agent = Mock(return_value={"graph": "created"})
    langchain_acp = ModuleType("langchain_acp")
    langchain_acp.run_acp = Mock()
    acpremote = ModuleType("acpremote")
    remote_server = _FakeRemoteServer()
    acpremote.connect_acp = Mock(return_value={"remote": "agent"})

    async def _serve_acp(**kwargs: object) -> _FakeRemoteServer:
        acpremote.serve_acp_kwargs = kwargs
        return remote_server

    acpremote.serve_acp = _serve_acp
    monkeypatch.setitem(sys.modules, "pydantic_ai", pydantic_ai)
    monkeypatch.setitem(sys.modules, "pydantic_acp", pydantic_acp)
    monkeypatch.setitem(sys.modules, "langchain", langchain)
    monkeypatch.setitem(sys.modules, "langchain.agents", langchain_agents)
    monkeypatch.setitem(sys.modules, "langchain_acp", langchain_acp)
    monkeypatch.setitem(sys.modules, "acpremote", acpremote)
    return {
        "pydantic_acp": pydantic_acp,
        "langchain_acp": langchain_acp,
        "langchain_agents": langchain_agents,
        "acpremote": acpremote,
        "remote_server": remote_server,
    }


def _set_telegram_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ACPROUTER_WORKSPACE_ROOT", str(tmp_path))


def test_examples_main_lists_entrypoints(capsys: pytest.CaptureFixture[str]) -> None:
    runpy.run_module("examples.__main__", run_name="__main__")
    output = capsys.readouterr().out
    assert "examples/telegram_gateway.py" in output
    assert "examples/pydantic_acp_agent.py" in output
    assert "examples/langchain_acp_graph.py" in output
    assert "examples/acprouter_with_acpkit_instance.py" in output
    assert "examples/acpremote_bridge_server.py" in output
    assert "examples/acprouter_remote_acp.py" in output


def test_telegram_gateway_example_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acprouter, "AppSettings", _FakeSettings)
    monkeypatch.setattr(acprouter, "TelegramGateway", _FakeTelegramGateway)
    _FakeTelegramGateway.run_calls = 0

    runpy.run_module("examples.telegram_gateway", run_name="__main__")

    assert _FakeTelegramGateway.run_calls == 1


def test_pydantic_agent_example_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    modules = _install_agent_example_modules(monkeypatch)

    globals_ = runpy.run_module("examples.pydantic_acp_agent", run_name="__main__")

    modules["pydantic_acp"].run_acp.assert_called_once()
    assert (
        globals_["describe_router"]()
        == "ACP Router connects Telegram chats to an ACP agent session."
    )


def test_langchain_agent_example_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    modules = _install_agent_example_modules(monkeypatch)

    globals_ = runpy.run_module("examples.langchain_acp_graph", run_name="__main__")

    modules["langchain_agents"].create_agent.assert_called_once()
    modules["langchain_acp"].run_acp.assert_called_once_with(graph=globals_["graph"])


def test_in_process_acpkit_example_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    modules = _install_agent_example_modules(monkeypatch)
    _set_telegram_env(monkeypatch, tmp_path)
    monkeypatch.setattr(acprouter, "TelegramGateway", _FakeGateway)
    _FakeGateway.instances = []

    globals_ = runpy.run_module("examples.acprouter_with_acpkit_instance", run_name="__main__")

    modules["pydantic_acp"].create_acp_agent.assert_called_once()
    assert _FakeGateway.instances[0].run_calls == 1
    assert (
        globals_["router_runtime"]()
        == "This Pydantic AI agent was adapted to ACP and mounted in Telegram in-process."
    )


def test_acpremote_bridge_example_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    modules = _install_agent_example_modules(monkeypatch)

    globals_ = runpy.run_module("examples.acpremote_bridge_server", run_name="__main__")

    modules["pydantic_acp"].create_acp_agent.assert_called_once()
    assert modules["remote_server"].serve_forever_calls == 1
    assert globals_["remote_runtime"]() == "This agent is exposed over WebSocket by acpremote."


def test_acprouter_remote_acp_example_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    modules = _install_agent_example_modules(monkeypatch)
    _set_telegram_env(monkeypatch, tmp_path)
    monkeypatch.setattr(acprouter, "TelegramGateway", _FakeGateway)
    _FakeGateway.instances = []

    runpy.run_module("examples.acprouter_remote_acp", run_name="__main__")

    modules["acpremote"].connect_acp.assert_called_once()
    assert _FakeGateway.instances[0].run_calls == 1
