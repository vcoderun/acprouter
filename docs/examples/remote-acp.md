# Remote ACP Example

`examples/acpremote_bridge_server.py` exposes an ACP agent over WebSocket with acpremote.

```bash
uv run python examples/acpremote_bridge_server.py
```

The local router can connect through the CLI mirror:

```bash
export ACPROUTER_COMMAND='acpkit run --addr ws://remote.example.com:8080/acp/ws'
uv run acprouter
```

Or through the Python API connector in `examples/acprouter_remote_acp.py`:

```bash
export ACPREMOTE_URL='ws://remote.example.com:8080/acp/ws'
uv run python examples/acprouter_remote_acp.py
```
