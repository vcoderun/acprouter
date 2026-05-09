# Agent Server Setup

ACP Router is the Telegram client surface. It does not adapt Pydantic AI, LangChain, or another
runtime by itself. The agent side must already be exposed as ACP.

The normal split is:

- ACP Kit adapts a Pydantic AI agent or LangChain graph into an ACP server.
- ACP Router starts the Telegram gateway and talks to that ACP server over stdio.
- acpremote is a transport bridge for moving an already-ACP surface over WebSocket.

## Environment Variables

Required for the CLI entrypoint:

| Variable | Purpose |
| --- | --- |
| `ACPROUTER_COMMAND` | Command ACP Router launches as the stdio ACP server. |
| `TELEGRAM_API_ID` | Telegram API id used by the Telegram client session. |
| `TELEGRAM_API_HASH` | Telegram API hash used by the Telegram client session. |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token. |

Optional runtime settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ACPROUTER_WORKSPACE_ROOT` | current working directory | Workspace boundary for ACP client-owned file and terminal methods. |
| `ACPROUTER_STATE_DIR` | `<workspace>/.acprouter-state` | Local state for chat/session aliases and bindings. |
| `ACPROUTER_AGENT_CWD` | workspace root | Working directory for `ACPROUTER_COMMAND`. |
| `ACPROUTER_TELEGRAM_SESSION` | `acprouter-bot` | Local Telegram session name. |
| `ACPROUTER_TELEGRAM_BUSINESS_CONNECTION_ID` | unset | Optional Telegram business connection id. |
| `ACPROUTER_STDIO_BUFFER_LIMIT_BYTES` | `52428800` | Stdio buffer limit for the ACP subprocess. |
| `ACPROUTER_ENABLE_HOST_TOOLS` | `true` | Whether ACP Router satisfies ACP client-owned file and terminal requests. |
| `ACPROUTER_STREAMING_DEFAULT` | `false` | Whether new chats use incremental Telegram reply edits by default. |
| `ACPROUTER_STREAMING_EDIT_INTERVAL_SECONDS` | `1.0` | Edit throttle for incremental Telegram streaming. |
| `ACPROUTER_LOG_LEVEL` | `INFO` | Python logging level for ACP Router. |

`ACPROUTER_AGENT_COMMAND` is accepted as a legacy fallback for `ACPROUTER_COMMAND`. Prefer
`ACPROUTER_COMMAND` in new deployments.

Do not configure a surface selector. ACP Router is Telegram-only; non-Telegram values in
`ACPROUTER_SURFACE` are rejected.

## Local Pydantic AI Agent

The Pydantic example in `examples/pydantic_acp_agent.py` is a normal `pydantic_ai.Agent`:

```python
from pydantic_ai import Agent

agent = Agent(
    "openai:gpt-5",
    name="acprouter-demo-pydantic",
    instructions="Answer directly and keep responses short.",
)
```

ACP Kit can resolve that `agent` symbol and expose it through the Pydantic ACP adapter:

```bash
export ACPROUTER_COMMAND='acpkit run examples.pydantic_acp_agent:agent'
uv run acprouter
```

That starts ACP Router, which starts the `acpkit run ...` subprocess and binds Telegram chats to the
ACP sessions created by the adapted Pydantic AI agent.

The same file can also be run directly as a stdio ACP server because it calls `run_acp(...)` when
executed as a script:

```bash
export ACPROUTER_COMMAND='python examples/pydantic_acp_agent.py'
uv run acprouter
```

Use the `acpkit run module:attribute` shape when you want ACP Kit to perform target resolution. Use
the direct Python command when your module owns its own `run_acp(...)` startup.

## Local LangChain Graph

The LangChain example in `examples/langchain_acp_graph.py` defines a compiled graph-shaped target:

```python
from langchain.agents import create_agent

graph = create_agent(
    model="openai:gpt-5",
    tools=[],
    system_prompt="Answer directly and keep responses short.",
)
```

ACP Kit can resolve the graph and dispatch it to `langchain-acp`:

```bash
export ACPROUTER_COMMAND='acpkit run examples.langchain_acp_graph:graph'
uv run acprouter
```

If the graph module owns startup itself, it can call `langchain_acp.run_acp(graph=graph)` and ACP
Router can launch it directly:

```bash
export ACPROUTER_COMMAND='python examples/langchain_acp_graph.py'
uv run acprouter
```

This is the LangChain equivalent of the Pydantic flow: LangChain owns agent behavior, ACP Kit owns
the ACP adapter boundary, and ACP Router owns Telegram routing.

## In-Process Python API

Use `TelegramGateway.from_acp_agent(...)` when your Python process already has an ACP-compatible
agent object and should start Telegram in the same process.

`examples/acprouter_with_acpkit_instance.py` creates a Pydantic AI agent, adapts it with
`pydantic_acp.create_acp_agent(...)`, and passes the ACP agent object to ACP Router:

```python
from acprouter import TelegramGateway
from pydantic_acp import create_acp_agent

acp_agent = create_acp_agent(agent=pydantic_agent)
gateway = TelegramGateway.from_acp_agent(acp_agent, telegram_settings())
await gateway.run()
```

Run it with Telegram credentials in the environment:

```bash
export TELEGRAM_API_ID='12345'
export TELEGRAM_API_HASH='...'
export TELEGRAM_BOT_TOKEN='...'
uv run python examples/acprouter_with_acpkit_instance.py
```

This path does not use `ACPROUTER_COMMAND` for transport because no subprocess is spawned. The
example builds `AppSettings` manually and uses a placeholder `acp_command` value that is never read
by `from_acp_agent(...)`.

## Remote ACP Kit Server

Use `acpkit serve ...` when the Python runtime should live on a remote host and ACP Kit should adapt
that runtime before exposing it over WebSocket:

```bash
# Remote host
acpkit serve examples.pydantic_acp_agent:agent --host 0.0.0.0 --port 8080
```

The WebSocket endpoint is:

```text
ws://<host>:8080/acp/ws
```

On the machine running ACP Router, mirror that remote endpoint back into a local stdio ACP server:

```bash
export ACPROUTER_COMMAND='acpkit run --addr ws://remote.example.com:8080/acp/ws'
uv run acprouter
```

This is useful when the agent runtime needs remote resources, but Telegram credentials and routing
should stay on the local ACP Router machine.

## Remote Bridge With acpremote

Use acpremote when the target is already ACP and only needs a WebSocket bridge. It does not adapt
Pydantic AI, LangChain, or any other framework by itself.

`examples/acpremote_bridge_server.py` shows the combined adapter plus bridge pattern:

```python
from acpremote import serve_acp
from pydantic_acp import create_acp_agent

acp_agent = create_acp_agent(agent=pydantic_agent)
server = await serve_acp(
    agent=acp_agent,
    host="0.0.0.0",
    port=8080,
    bearer_token=None,
)
await server.serve_forever()
```

Start the remote bridge:

```bash
uv run python examples/acpremote_bridge_server.py
```

Then connect ACP Router to that remote bridge through the ACP Kit local mirror:

```bash
export ACPROUTER_COMMAND='acpkit run --addr ws://remote.example.com:8080/acp/ws'
uv run acprouter
```

If the remote bridge uses bearer-token auth, set `ACPREMOTE_BEARER_TOKEN` on the remote bridge and
use the Python API local connector instead of the short unauthenticated `acpkit run --addr ...`
command.

## Remote Bridge Through Python API

`examples/acprouter_remote_acp.py` connects to a remote ACP WebSocket endpoint with
`acpremote.connect_acp(...)` and mounts that remote ACP agent in Telegram with
`TelegramGateway.from_acp_agent(...)`:

```python
from acpremote import connect_acp
from acprouter import TelegramGateway

acp_agent = connect_acp(
    "ws://remote.example.com:8080/acp/ws",
    bearer_token="secret-token",
)
gateway = TelegramGateway.from_acp_agent(acp_agent, telegram_settings())
await gateway.run()
```

Run it locally:

```bash
export TELEGRAM_API_ID='12345'
export TELEGRAM_API_HASH='...'
export TELEGRAM_BOT_TOKEN='...'
export ACPREMOTE_URL='ws://remote.example.com:8080/acp/ws'
export ACPREMOTE_BEARER_TOKEN='secret-token'
uv run python examples/acprouter_remote_acp.py
```

Use this when you need auth headers or custom transport options that are easier to express in
Python than in a single shell command.

## Existing Stdio ACP Command

If the remote runtime is already a stdio ACP command, acpremote can expose that command over
WebSocket without adapting it:

```python
from acpremote import serve_command

server = await serve_command(
    ["fast-agent-acp"],
    host="0.0.0.0",
    port=8080,
    cwd="/srv/agent",
    env={"EXAMPLE_SETTING": "value"},
)
await server.serve_forever()
```

Then use the same local ACP Router command:

```bash
export ACPROUTER_COMMAND='acpkit run --addr ws://remote.example.com:8080/acp/ws'
uv run acprouter
```

## Choosing A Path

| Starting point | Adapter or bridge | ACP Router startup |
| --- | --- | --- |
| Local Pydantic AI agent | `acpkit run examples.pydantic_acp_agent:agent` | CLI with `ACPROUTER_COMMAND` |
| Local LangChain graph | `acpkit run examples.langchain_acp_graph:graph` | CLI with `ACPROUTER_COMMAND` |
| In-process Pydantic AI agent | `pydantic_acp.create_acp_agent(...)` | Python API with `TelegramGateway.from_acp_agent(...)` |
| Remote Python target needing adaptation | remote `acpkit serve ...` | local `ACPROUTER_COMMAND='acpkit run --addr ws://host:8080/acp/ws'` |
| Existing ACP agent object | remote `acpremote.serve_acp(...)` | local `acpkit run --addr ...` or `connect_acp(...)` plus `from_acp_agent(...)` |
| Existing stdio ACP command | remote `acpremote.serve_command(...)` | local `ACPROUTER_COMMAND='acpkit run --addr ws://host:8080/acp/ws'` |

## References

- [ACP Kit](https://vcoderun.github.io/acpkit/)
- [ACP Kit CLI](https://vcoderun.github.io/acpkit/cli/)
- [Pydantic Quickstart](https://vcoderun.github.io/acpkit/getting-started/pydantic-quickstart/)
- [LangChain Quickstart](https://vcoderun.github.io/acpkit/getting-started/langchain-quickstart/)
- [ACP Remote overview](https://vcoderun.github.io/acpkit/acpremote/)
- [ACP Remote API](https://vcoderun.github.io/acpkit/api/acpremote/)
