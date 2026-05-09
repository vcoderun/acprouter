# ACP Router

`acprouter` is an ACP client surface for driving ACP agents from Telegram.

Current MVP scope:

- connect to an ACP agent process over stdio
- bind one active ACP session per Telegram chat
- create, load, and list sessions
- stop active runs with `/stop`
- project ACP approvals, plan updates, tool updates, and agent replies into Telegram
- project ACP-exposed selection state such as current mode, current model, config options, and available slash commands
- answer ACP approval requests with inline buttons
- default to server-owned ACP truth: the ACP server owns tool execution, guardrails, approvals, and projection payloads
- optionally serve ACP client-owned file and terminal requests against the local workspace when the connected ACP server expects them

## Run

> You can create a Telegram app from [here](https://my.telegram.org/auth) to obtain an API id and hash. Finally, you will need to create a bot from [BotFather](https://t.me/BotFather) and keep the bot token.

Environment variables:

| Variable | Required | Purpose |
| --- | --- | --- |
| `ACPROUTER_COMMAND` | yes | Stdio ACP server command that ACP Router launches and talks to. |
| `TELEGRAM_API_ID` | yes | Telegram API id for the bot client session. |
| `TELEGRAM_API_HASH` | yes | Telegram API hash for the bot client session. |
| `TELEGRAM_BOT_TOKEN` | yes | Telegram bot token from BotFather. |
| `ACPROUTER_WORKSPACE_ROOT` | no | Workspace root for client-owned file and terminal requests. Defaults to the current working directory. |
| `ACPROUTER_STATE_DIR` | no | Local state directory for chat/session bindings. Defaults to `.acprouter-state` under the workspace root. |
| `ACPROUTER_AGENT_CWD` | no | Working directory for the ACP subprocess. Defaults to the workspace root. |
| `ACPROUTER_TELEGRAM_SESSION` | no | Telegram client session name. Defaults to `acprouter-bot`. |
| `ACPROUTER_TELEGRAM_BUSINESS_CONNECTION_ID` | no | Optional Telegram business connection id. |
| `ACPROUTER_STDIO_BUFFER_LIMIT_BYTES` | no | Stdio buffer limit for the ACP subprocess. Defaults to 50 MiB. |
| `ACPROUTER_ENABLE_HOST_TOOLS` | no | Enables ACP client-owned file and terminal methods. Defaults to `true`. |
| `ACPROUTER_STREAMING_DEFAULT` | no | Enables incremental Telegram reply edits by default. Defaults to `false`. |
| `ACPROUTER_STREAMING_EDIT_INTERVAL_SECONDS` | no | Minimum interval between Telegram streaming edits. Defaults to `1.0`. |
| `ACPROUTER_LOG_LEVEL` | no | Python logging level. Defaults to `INFO`. |

`ACPROUTER_AGENT_COMMAND` is still accepted as a legacy name for `ACPROUTER_COMMAND`, but new
setups should use `ACPROUTER_COMMAND`. No surface selector is required; ACP Router is Telegram-only.

`ACPROUTER_ENABLE_HOST_TOOLS` defaults to `true`. This only affects ACP client-owned file and terminal requests. It does not make ACP Router invent projection truth or override server-owned tool semantics.

When client-owned host tools are enabled:

- file paths must stay inside the configured workspace root
- file paths must stay inside the active session cwd
- command cwd must stay inside the active session cwd
- read, write, and execute requests require Telegram approval
- rejected requests return a reason back to the ACP server

If you explicitly set `ACPROUTER_ENABLE_HOST_TOOLS=false`, ACP Router returns a rejection reason for ACP client-owned host requests instead of executing them.

`ACPROUTER_STREAMING_DEFAULT` defaults to `false`. This is a Telegram render preference, not ACP state. Leave it off to buffer agent chunks and send the final reply once. Turn it on only when you want incremental Telegram edits.

Run:

```bash
uv run acprouter
```

## ACP Agent Command

`ACPROUTER_COMMAND` must start an ACP server over stdio. It can point at any command that speaks ACP
JSON on `stdout` and writes logs to `stderr`.

For a local Pydantic AI agent adapted by ACP Kit, point the command at the agent symbol:

```bash
export ACPROUTER_COMMAND='acpkit run examples.pydantic_acp_agent:agent'
uv run acprouter
```

For a local LangChain graph adapted by ACP Kit, point the command at the graph symbol:

```bash
export ACPROUTER_COMMAND='acpkit run examples.langchain_acp_graph:graph'
uv run acprouter
```

For an in-process Python integration, create an ACP agent object with ACP Kit and pass it directly to
the Telegram gateway:

```python
from acprouter import TelegramGateway
from pydantic_acp import create_acp_agent

acp_agent = create_acp_agent(agent=pydantic_agent)
gateway = TelegramGateway.from_acp_agent(acp_agent, telegram_settings())
await gateway.run()
```

See `examples/acprouter_with_acpkit_instance.py` for the full version.

For a remote ACP server exposed over WebSocket, run the server with ACP Kit or acpremote, then mirror
that endpoint back into a local stdio ACP boundary:

```bash
# Remote host
acpkit serve examples.pydantic_acp_agent:agent --host 0.0.0.0 --port 8080

# Machine running acprouter
export ACPROUTER_COMMAND='acpkit run --addr ws://remote.example.com:8080/acp/ws'
uv run acprouter
```

Use `acpkit serve ...` when the target is a Python runtime that ACP Kit should resolve into an
adapter. Use `acpremote.serve_acp(...)` or `acpremote.serve_command(...)` when the target already is
an ACP agent or a stdio ACP command and only needs WebSocket transport.

Concrete examples:

- `examples/pydantic_acp_agent.py`
- `examples/langchain_acp_graph.py`
- `examples/acprouter_with_acpkit_instance.py`
- `examples/acpremote_bridge_server.py`
- `examples/acprouter_remote_acp.py`

More detail is in
[docs/getting-started/agent-server.md](https://github.com/vcoderun/acprouter/blob/main/docs/getting-started/agent-server.md).

## Construct Programmatically

Use `TelegramGateway.from_settings(...)` when `acprouter` should spawn the ACP subprocess from
`ACPROUTER_COMMAND`:

```python
from __future__ import annotations as _annotations

import asyncio

from acprouter import AppSettings, TelegramGateway

settings = AppSettings.from_env()
gateway = TelegramGateway.from_settings(settings)

asyncio.run(gateway.run())
```

Use `TelegramGateway.from_acp_agent(...)` when another part of the process already owns the ACP
agent:

```python
from __future__ import annotations as _annotations

import asyncio

from acprouter import AppSettings, TelegramGateway

settings = AppSettings.from_env()
gateway = TelegramGateway.from_acp_agent(acp_agent, settings)

asyncio.run(gateway.run())
```

Notes:

- `ACPROUTER_COMMAND` must launch an ACP agent that writes ACP JSON only to `stdout`.
- Agent logs should go to `stderr`, otherwise the ACP stdio connection will break.

Contributor setup and local quality commands are documented in
[CONTRIBUTING.md](https://github.com/vcoderun/acprouter/blob/main/CONTRIBUTING.md).
Gateway construction details are documented in
[docs/gateway.md](https://github.com/vcoderun/acprouter/blob/main/docs/gateway.md).

## Current Commands

- `/new`
  create and bind a fresh ACP session
- `/new <name>`
  create and bind a fresh ACP session, then store the alias for the current chat
- `/session`
  show the active session id for the current chat
- `/mode <mode-id>`
  switch the active session mode
- `/<mode-id>`
  shorthand for switching directly into a mode such as `/ask`, `/plan`, or `/agent`
- dynamic ACP commands
  ACP Router also registers ACP-exposed selection and command surfaces as Telegram commands when the names are Telegram-safe. For example, ACP mode, model, and select-style config surfaces can appear as commands such as `/agent`, `/model <provider:model>`, or `/thinking <medium|high>`.
  If an ACP name is not Telegram-safe, ACP Router publishes a Telegram alias such as `/read_only` for ACP `/read-only` and routes it back to the original ACP name.
- `/switch <session-id-or-alias>`
  load and bind an existing session
- `/sessions`
  list available sessions
- `/stop`
  cancel the current run
- `/streaming <true|false>`
  toggle incremental Telegram reply edits for the current chat

## Selection Rendering

When the ACP server exposes mode, model, config, or command-selection state, ACP Router renders a Telegram projection card for that session and turns Telegram-safe selections into commands.

Typical examples:

- ACP current mode becomes visible in the session selections card
- ACP current model becomes visible in the same card
- ACP config options such as `thinking=medium` render as selection rows
- ACP commands with unstructured input hints can appear as commands such as `/thinking medium|high`

ACP Router does not invent these surfaces. If the ACP server does not expose them, there is nothing to render or register.
