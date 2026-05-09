# Gateway Construction

`acprouter` ships one gateway surface:

- `TelegramGateway`

It supports configured construction and prebuilt ACP-agent construction.

## Construct From Settings

Use `from_settings(...)` when `acprouter` should spawn the ACP process defined by `ACPROUTER_COMMAND`.

```python
from __future__ import annotations as _annotations

import asyncio

from acprouter import AppSettings, TelegramGateway

settings = AppSettings.from_env()
gateway = TelegramGateway.from_settings(settings)

asyncio.run(gateway.run())
```

This is the same path used by the CLI entrypoint.

By default this path is server-owned for ACP truth:

- the ACP server owns tool execution
- the ACP server owns guardrails and approval policy
- the ACP server owns projection truth

At the same time, `ACPROUTER_ENABLE_HOST_TOOLS=true` by default, so ACP Router can still satisfy ACP client-owned file and terminal requests when the connected ACP server expects them.

`ACPROUTER_COMMAND` can point at a local ACP Kit adapter command such as:

```bash
acpkit run examples.pydantic_acp_agent:agent
acpkit run examples.langchain_acp_graph:graph
```

It can also point at a local mirror for a remote ACP WebSocket endpoint:

```bash
acpkit run --addr ws://remote.example.com:8080/acp/ws
```

Use `acpkit serve ...` on the remote host when ACP Kit should adapt a Python runtime first. Use
`acpremote.serve_acp(...)` or `acpremote.serve_command(...)` when the remote host already has an ACP
agent or stdio ACP command and only needs WebSocket transport.

The same ACP boundary can also be supplied directly to the Python API. For example,
`examples/acprouter_with_acpkit_instance.py` uses `pydantic_acp.create_acp_agent(...)` and then calls
`TelegramGateway.from_acp_agent(...)`; `examples/acprouter_remote_acp.py` uses
`acpremote.connect_acp(...)` and then calls the same gateway constructor.

## Construct From An Existing ACP Agent

Use `from_acp_agent(...)` when some other part of the application already owns the ACP agent object.

```python
from __future__ import annotations as _annotations

import asyncio

from acprouter import AppSettings, TelegramGateway

settings = AppSettings.from_env()
gateway = TelegramGateway.from_acp_agent(acp_agent, settings)

asyncio.run(gateway.run())
```

In this mode `acprouter` does not spawn a subprocess. It binds the supplied ACP agent to the
gateway and then starts the Telegram client.

## Runtime Behavior

`gateway.run()` is responsible for:

- binding Telegram message and callback handlers
- initializing the ACP connection
- starting the Telegram client
- registering Telegram bot commands
- entering the idle loop
- stopping Telegram on shutdown

If the gateway was created with `from_settings(...)`, `run()` also spawns the configured ACP subprocess and tears it down when the bot exits.

## Ownership Boundary

Prefer server-owned ACP truth.

That means:

- tool execution should usually happen on the ACP server
- guardrail and safety policy should usually live on the ACP server
- rich diff, terminal, and tool projection payloads should come from ACP updates

ACP Router should mainly:

- forward user prompts and approval selections
- bind Telegram chats to ACP sessions
- render ACP updates into Telegram-friendly UI

ACP Router does not invent file or terminal tool updates for these requests. It only executes the ACP client-owned methods when the ACP server calls them.

When client-owned host tools are active:

- file access must stay inside the configured workspace root
- file access must stay inside the active session cwd
- command cwd must stay inside the active session cwd
- read, write, and execute requests require Telegram approval
- denied requests return explicit ACP errors instead of silent no-op results

If `ACPROUTER_ENABLE_HOST_TOOLS=false`, ACP Router rejects ACP client-owned file and terminal requests with a structured reason.

## Telegram Reply Streaming

Telegram reply streaming is a client-local render preference.

Current behavior:

- streaming is disabled by default
- when disabled, ACP Router buffers `AgentMessageChunk` text and sends the final reply once the run completes
- when enabled, ACP Router edits the active Telegram reply message incrementally
- incremental edits are throttled by `ACPROUTER_STREAMING_EDIT_INTERVAL_SECONDS` to avoid Telegram flood waits

This does not change ACP truth. It only changes how ACP Router renders agent chunks into Telegram.

## Selection Surfaces

When the ACP server exposes session selection state, ACP Router turns that truth into Telegram UI and commands.

Current behavior:

- `CurrentModeUpdate` updates the visible current mode
- session model state from `new_session` or `load_session` responses updates the visible current model
- `ConfigOptionUpdate` updates visible config selections
- `AvailableCommandsUpdate` updates the set of Telegram-safe ACP commands

ACP Router then:

- renders a session selections card into the chat
- refreshes Telegram bot commands
- forwards dynamic command invocations back to ACP

Examples:

- `/agent`
  direct mode shorthand when the ACP session exposes an `agent` mode
- `/full_access`
  Telegram alias for an ACP mode or command such as `full-access`
- `/model openai:gpt-5`
  calls ACP session-model selection
- `/thinking high`
  calls ACP config selection when the server exposes a `thinking` select option
- `/tools`
  forwards the slash command back to ACP when the server exposes `tools` as an available command

This remains server-owned. ACP Router only renders and forwards what the ACP server exposes.
If an ACP name contains Telegram-hostile characters such as `-`, ACP Router creates a Telegram-safe alias and maps it back to the original ACP name during invocation.
