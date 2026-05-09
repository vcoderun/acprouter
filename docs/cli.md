# CLI

The `acprouter` command loads `.env` from the current working directory, builds `AppSettings`, and
starts `TelegramGateway.from_settings(...)`.

```bash
uv run acprouter
```

## Agent Command

`ACPROUTER_COMMAND` must be a command that starts an ACP server over stdio. Typical values:

```bash
export ACPROUTER_COMMAND='acpkit run examples.pydantic_acp_agent:agent'
export ACPROUTER_COMMAND='acpkit run examples.langchain_acp_graph:graph'
export ACPROUTER_COMMAND='python examples/pydantic_acp_agent.py'
export ACPROUTER_COMMAND='acpkit run --addr ws://remote.example.com:8080/acp/ws'
```

Use `acpkit run module:attribute` when ACP Kit should resolve a local Python target and choose the
matching adapter. Use `acpkit run --addr ...` when a remote ACP WebSocket endpoint should be mirrored
back into the local stdio boundary ACP Router expects.

## Quality Commands

```bash
make format
make tests
make check
make check-coverage
make serve
```

`make serve` starts MkDocs at `http://127.0.0.1:8000`.
