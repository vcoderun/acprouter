# Local Pydantic Example

`examples/pydantic_acp_agent.py` defines a normal `pydantic_ai.Agent`.

Run it through ACP Kit target resolution:

```bash
export ACPROUTER_COMMAND='acpkit run examples.pydantic_acp_agent:agent'
uv run acprouter
```

Or run the file as its own stdio ACP server:

```bash
export ACPROUTER_COMMAND='python examples/pydantic_acp_agent.py'
uv run acprouter
```
