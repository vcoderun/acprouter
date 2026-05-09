# pyright: reportMissingImports=false
from __future__ import annotations as _annotations

import asyncio
import os
from pathlib import Path

from pydantic_acp import create_acp_agent
from pydantic_ai import Agent

from acprouter import AppSettings, TelegramGateway

pydantic_agent = Agent(
    "openai:gpt-5",
    name="acprouter-in-process-demo",
    instructions="Answer directly and keep responses short.",
)


@pydantic_agent.tool_plain
def router_runtime() -> str:
    """Describe the current integration path."""
    return "This Pydantic AI agent was adapted to ACP and mounted in Telegram in-process."


def telegram_settings() -> AppSettings:
    workspace_root = (
        Path(os.environ.get("ACPROUTER_WORKSPACE_ROOT", os.getcwd())).expanduser().resolve()
    )
    state_dir = Path(
        os.environ.get("ACPROUTER_STATE_DIR", workspace_root / ".acprouter-state")
    ).expanduser()
    return AppSettings(
        telegram_api_id=int(os.environ["TELEGRAM_API_ID"]),
        telegram_api_hash=os.environ["TELEGRAM_API_HASH"],
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
        telegram_session_name=os.environ.get("ACPROUTER_TELEGRAM_SESSION", "acprouter-bot"),
        telegram_business_connection_id=(
            os.environ.get("ACPROUTER_TELEGRAM_BUSINESS_CONNECTION_ID", "").strip() or None
        ),
        acp_command=("in-process-acp-agent",),
        workspace_root=workspace_root,
        state_dir=state_dir.resolve(),
        acp_cwd=workspace_root,
        acp_stdio_buffer_limit_bytes=50 * 1024 * 1024,
        enable_host_tools=True,
        streaming_default=False,
        streaming_edit_interval_seconds=1.0,
    )


async def main() -> None:
    acp_agent = create_acp_agent(agent=pydantic_agent)
    gateway = TelegramGateway.from_acp_agent(acp_agent, telegram_settings())
    await gateway.run()


if __name__ == "__main__":
    asyncio.run(main())
