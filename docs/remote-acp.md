# Remote ACP Bridges

ACP Router can run next to Telegram while the agent runtime runs elsewhere.

There are two common remote shapes:

- `acpkit serve ...` adapts a Python target and exposes the resulting ACP server over WebSocket.
- `acpremote.serve_acp(...)` or `acpremote.serve_command(...)` exposes an already-ACP surface over
  WebSocket.

## ACP Kit Remote Host

```bash
acpkit serve examples.pydantic_acp_agent:agent --host 0.0.0.0 --port 8080
```

Then mirror it locally:

```bash
export ACPROUTER_COMMAND='acpkit run --addr ws://remote.example.com:8080/acp/ws'
uv run acprouter
```

## acpremote Bridge

Use `acpremote` when the runtime already speaks ACP:

```python
from acpremote import serve_acp

server = await serve_acp(agent=my_acp_agent, host="0.0.0.0", port=8080)
await server.serve_forever()
```

For an existing stdio ACP command:

```python
from acpremote import serve_command

server = await serve_command(["fast-agent-acp"], host="0.0.0.0", port=8080)
await server.serve_forever()
```

Use `examples/acpremote_bridge_server.py` for the bridge host and
`examples/acprouter_remote_acp.py` for the local Python API connector.
