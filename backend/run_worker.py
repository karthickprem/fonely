"""Notification worker entrypoint."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fonely.core.config import settings
from fonely.workers.notification_worker import LoggingNotificationSender, run_notification_worker

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


async def main() -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sender = LoggingNotificationSender()
    try:
        await run_notification_worker(factory, sender)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
