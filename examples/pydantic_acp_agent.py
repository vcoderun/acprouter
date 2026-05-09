# pyright: reportMissingImports=false
from __future__ import annotations as _annotations

from pydantic_acp import run_acp
from pydantic_ai import Agent

agent = Agent(
    "openai:gpt-5",
    name="acprouter-demo-pydantic",
    instructions="Answer directly and keep responses short.",
)


@agent.tool_plain
def describe_router() -> str:
    """Return a short description visible through ACP."""
    return "ACP Router connects Telegram chats to an ACP agent session."


if __name__ == "__main__":
    run_acp(agent=agent)
