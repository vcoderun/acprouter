# pyright: reportMissingImports=false
from __future__ import annotations as _annotations

import asyncio
import os
from pathlib import Path

from acpremote import connect_acp

from acprouter import AppSettings, TelegramGateway


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
        acp_command=("remote-acp-agent",),
        workspace_root=workspace_root,
        state_dir=state_dir.resolve(),
        acp_cwd=workspace_root,
        acp_stdio_buffer_limit_bytes=50 * 1024 * 1024,
        enable_host_tools=True,
        streaming_default=False,
        streaming_edit_interval_seconds=1.0,
    )


async def main() -> None:
    acp_agent = connect_acp(
        os.environ.get("ACPREMOTE_URL", "ws://127.0.0.1:8080/acp/ws"),
        bearer_token=os.environ.get("ACPREMOTE_BEARER_TOKEN") or None,
    )
    gateway = TelegramGateway.from_acp_agent(acp_agent, telegram_settings())
    await gateway.run()


if __name__ == "__main__":
    asyncio.run(main())
