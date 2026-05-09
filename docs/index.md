---
title: ACP Router
---

# ACP Router {.hide}

--8<-- "docs/.partials/index-header.html"

`acprouter` is an ACP client surface for driving ACP agents from Telegram.

Current MVP coverage:

- connect to an ACP agent process over stdio
- create, load, list, and bind ACP sessions per Telegram chat
- project ACP approvals, plan updates, tool updates, and agent replies into Telegram
- render ACP-exposed selection state such as mode, model, config options, and available commands
- answer ACP approval requests with inline buttons
- stop active runs with `/stop`
- default to server-owned ACP truth
- optionally satisfy ACP client-owned file and terminal requests against the local workspace when the ACP server expects them

## Construction

`acprouter` can either:

- spawn an ACP subprocess from settings
- attach to an already constructed ACP agent object
- start a Telegram gateway

Programmatic construction:

```python
from __future__ import annotations as _annotations

import asyncio

from acprouter import AppSettings, TelegramGateway

settings = AppSettings.from_env()
gateway = TelegramGateway.from_settings(settings)

asyncio.run(gateway.run())
```

See [Gateway Construction](gateway.md) for the supported construction paths.
See [Agent Server Setup](getting-started/agent-server.md) for environment variables and ACP server
command examples.

## Run

Required settings:

| Variable | Purpose |
| --- | --- |
| `ACPROUTER_COMMAND` | Stdio ACP server command launched by ACP Router. |
| `TELEGRAM_API_ID` | Telegram API id. |
| `TELEGRAM_API_HASH` | Telegram API hash. |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token. |

Optional host-tool setting:

- `ACPROUTER_ENABLE_HOST_TOOLS`
- `ACPROUTER_STREAMING_DEFAULT`
- `ACPROUTER_STREAMING_EDIT_INTERVAL_SECONDS`

`ACPROUTER_ENABLE_HOST_TOOLS` defaults to `true`.

When enabled, ACP Router can satisfy ACP client-owned file and terminal requests, but it still:

- keeps projection truth server-owned
- enforces workspace-root and session-cwd boundaries
- requires Telegram approval before read, write, or execute actions
- returns explicit rejection reasons to the ACP server when access is denied

Then run:

```bash
uv run acprouter
```

Example `ACPROUTER_COMMAND` values:

```bash
export ACPROUTER_COMMAND='acpkit run examples.pydantic_acp_agent:agent'
export ACPROUTER_COMMAND='acpkit run examples.langchain_acp_graph:graph'
export ACPROUTER_COMMAND='acpkit run --addr ws://remote.example.com:8080/acp/ws'
```

The first two examples let ACP Kit adapt local Pydantic AI and LangChain targets. The remote example
mirrors a WebSocket ACP server, such as one started by `acpkit serve ...` or
`acpremote.serve_acp(...)`, back into the local stdio boundary that ACP Router expects.

## Telegram Notes

Telegram exposes dynamic slash commands, inline approval buttons, and optional incremental reply
edits.

## Current Telegram Commands

- `/new`
- `/new <name>`
- `/session`
- `/mode <mode-id>`
- `/<mode-id>`
- dynamic ACP selection commands such as `/model <provider:model>` or `/thinking <value>` when the ACP server exposes Telegram-safe command names
- `/switch <session-id-or-alias>`
- `/sessions`
- `/stop`
- `/streaming <true|false>`
