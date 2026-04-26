from __future__ import annotations

import asyncio

from app.config import get_settings
from app.services.background_runner import background_sampler_loop


async def main() -> None:
    settings = get_settings()
    await background_sampler_loop(settings)


if __name__ == "__main__":
    asyncio.run(main())
