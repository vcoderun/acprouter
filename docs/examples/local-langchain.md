# Local LangChain Example

`examples/langchain_acp_graph.py` defines a LangChain graph target.

Run it through ACP Kit target resolution:

```bash
export ACPROUTER_COMMAND='acpkit run examples.langchain_acp_graph:graph'
uv run acprouter
```

Or run the file as its own stdio ACP server:

```bash
export ACPROUTER_COMMAND='python examples/langchain_acp_graph.py'
uv run acprouter
```
