# Installation

ACP Router is a Telegram client surface for ACP agents. Install it in the same Python environment
that will run the Telegram gateway:

```bash
uv sync
```

For local development, include the development and documentation extras:

```bash
uv sync --extra dev --extra docs
```

ACP Router expects the agent side to be ACP already. For local Pydantic AI or LangChain runtimes,
install ACP Kit with the adapter extra that matches the runtime:

```bash
uv add "acpkit[pydantic]"
uv add "acpkit[langchain]"
```

For remote ACP bridging, install the ACP remote transport package through ACP Kit:

```bash
uv add acpkit
```

## Required Telegram Settings

Set these before starting the router:

```bash
export TELEGRAM_API_ID='12345'
export TELEGRAM_API_HASH='...'
export TELEGRAM_BOT_TOKEN='...'
```

For CLI startup, also set `ACPROUTER_COMMAND` to a stdio ACP server command:

```bash
export ACPROUTER_COMMAND='acpkit run examples.pydantic_acp_agent:agent'
```

The Python API path can skip `ACPROUTER_COMMAND` when it supplies an ACP agent object directly with
`TelegramGateway.from_acp_agent(...)`.
