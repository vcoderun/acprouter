# Quickstart

The shortest useful path is:

1. expose a Pydantic AI agent or LangChain graph as ACP with ACP Kit
2. point `ACPROUTER_COMMAND` at that ACP server
3. start ACP Router

## Pydantic AI

```bash
export TELEGRAM_API_ID='12345'
export TELEGRAM_API_HASH='...'
export TELEGRAM_BOT_TOKEN='...'
export ACPROUTER_COMMAND='acpkit run examples.pydantic_acp_agent:agent'
uv run acprouter
```

`acpkit run examples.pydantic_acp_agent:agent` imports the example module, resolves the
`pydantic_ai.Agent`, and exposes it through `pydantic-acp`.

## LangChain

```bash
export TELEGRAM_API_ID='12345'
export TELEGRAM_API_HASH='...'
export TELEGRAM_BOT_TOKEN='...'
export ACPROUTER_COMMAND='acpkit run examples.langchain_acp_graph:graph'
uv run acprouter
```

`acpkit run examples.langchain_acp_graph:graph` resolves the graph target and exposes it through
`langchain-acp`.

## Remote ACP

When the ACP server runs elsewhere:

```bash
# Remote host
acpkit serve examples.pydantic_acp_agent:agent --host 0.0.0.0 --port 8080
```

```bash
# ACP Router host
export TELEGRAM_API_ID='12345'
export TELEGRAM_API_HASH='...'
export TELEGRAM_BOT_TOKEN='...'
export ACPROUTER_COMMAND='acpkit run --addr ws://remote.example.com:8080/acp/ws'
uv run acprouter
```

For authenticated or custom remote transport, use the Python API path in
`examples/acprouter_remote_acp.py`.
