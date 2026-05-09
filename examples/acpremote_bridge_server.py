# pyright: reportMissingImports=false
from __future__ import annotations as _annotations

import asyncio
import os

from acpremote import serve_acp
from pydantic_acp import create_acp_agent
from pydantic_ai import Agent

pydantic_agent = Agent(
    "openai:gpt-5",
    name="remote-acp-demo",
    instructions="Answer directly and keep responses short.",
)


@pydantic_agent.tool_plain
def remote_runtime() -> str:
    """Describe the remote ACP bridge."""
    return "This agent is exposed over WebSocket by acpremote."


async def main() -> None:
    acp_agent = create_acp_agent(agent=pydantic_agent)
    server = await serve_acp(
        agent=acp_agent,
        host=os.environ.get("ACPREMOTE_HOST", "0.0.0.0"),
        port=int(os.environ.get("ACPREMOTE_PORT", "8080")),
        bearer_token=os.environ.get("ACPREMOTE_BEARER_TOKEN") or None,
    )
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
