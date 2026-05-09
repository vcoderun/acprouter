from __future__ import annotations as _annotations

import asyncio

from acprouter import AppSettings, TelegramGateway


async def main() -> None:
    settings = AppSettings.from_env()
    gateway = TelegramGateway.from_settings(settings)
    await gateway.run()


if __name__ == "__main__":
    asyncio.run(main())
