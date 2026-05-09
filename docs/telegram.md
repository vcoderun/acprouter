# Telegram Surface

ACP Router owns Telegram routing and rendering. The ACP server owns agent truth.

The router binds Telegram chats to ACP sessions, forwards user prompts, renders ACP updates, and
answers ACP permission requests with Telegram inline buttons.

## Commands

- `/new`
- `/new <name>`
- `/session`
- `/mode <mode-id>`
- `/<mode-id>`
- `/switch <session-id-or-alias>`
- `/sessions`
- `/stop`
- `/streaming <true|false>`

ACP-exposed modes, models, config options, and available commands can also become dynamic Telegram
commands when the names are Telegram-safe.

## Streaming

`ACPROUTER_STREAMING_DEFAULT=false` buffers text chunks and sends the final answer when a run
completes. Set it to `true` to edit the active Telegram reply incrementally.

`ACPROUTER_STREAMING_EDIT_INTERVAL_SECONDS` throttles those edits to reduce Telegram flood waits.

This is only a Telegram render preference. It is not ACP session state.

## Approvals

Read, write, and execute requests from ACP client-owned host methods require Telegram approval when
host tools are enabled. Rejected requests return a structured reason to the ACP server.
