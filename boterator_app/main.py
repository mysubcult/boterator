from __future__ import annotations

import asyncio
import logging
import signal

from .ai import AiAnalyzer
from .config import Settings
from .db import create_pool, ensure_schema
from .master import MasterBot
from .slave import SlaveManager


async def amain() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    pool = await create_pool(settings.database_url)
    await ensure_schema(pool)

    analyzer = AiAnalyzer(settings)
    if settings.ai_provider != "none" and not analyzer.enabled:
        logging.warning("AI_PROVIDER=%s configured, but the API key is missing", settings.ai_provider)

    slave_manager = SlaveManager(
        pool,
        analyzer,
        settings.publish_mode,
        settings.data_retention_days,
        settings.cleanup_interval_hours,
    )
    await slave_manager.start_existing()

    master = MasterBot(pool, settings, slave_manager)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    master_task = asyncio.create_task(master.run(), name="master-polling")
    stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-waiter")

    try:
        done, pending = await asyncio.wait({master_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            exc = task.exception()
            if exc:
                raise exc
    finally:
        await master.stop()
        await slave_manager.stop_all()
        await pool.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
