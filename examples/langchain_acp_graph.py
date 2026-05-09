# pyright: reportMissingImports=false
from __future__ import annotations as _annotations

from langchain.agents import create_agent
from langchain_acp import run_acp

graph = create_agent(
    model="openai:gpt-5",
    tools=[],
    system_prompt="Answer directly and keep responses short.",
)


if __name__ == "__main__":
    run_acp(graph=graph)
