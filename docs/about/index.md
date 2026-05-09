# About ACP Router

`acprouter` is a chat-native ACP client surface. It binds Telegram conversations to ACP sessions
and projects ACP state back into Telegram without inventing a separate agent protocol.

## Scope

Current scope:

- ACP stdio client connection to an existing agent process
- Telegram chat surface
- session binding and recovery
- approvals, tool progress, and plan projection
- optional ACP client-owned file and terminal methods behind workspace and cwd guards

## Ownership Model

`acprouter` is not the source of truth for agent behavior.

The intended boundary is:

- the ACP server owns tool execution, approval policy, and projection truth
- `acprouter` owns chat routing, session binding, and chat-native rendering

## Repository Links

- Repository: https://github.com/vcoderun/acprouter
- Issues: https://github.com/vcoderun/acprouter/issues
- Contributing: https://github.com/vcoderun/acprouter/blob/main/CONTRIBUTING.md
