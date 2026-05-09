# In-Process Example

`examples/acprouter_with_acpkit_instance.py` keeps the ACP adapter and Telegram gateway in the same
Python process:

```python
from acprouter import TelegramGateway
from pydantic_acp import create_acp_agent

acp_agent = create_acp_agent(agent=pydantic_agent)
gateway = TelegramGateway.from_acp_agent(acp_agent, telegram_settings())
await gateway.run()
```

Use this path when your application already owns the agent object and should not spawn an ACP
subprocess.
