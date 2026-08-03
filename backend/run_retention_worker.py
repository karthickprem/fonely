"""Standalone retention cleanup worker entrypoint."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fonely.core.config import settings
from fonely.core.logging_config import configure_logging
from fonely.services.data_retention import DataRetentionService

logger = logging.getLogger("fonely.workers.retention")

INTERVAL_HOURS = 6


async def run() -> None:
    configure_logging(settings.log_format, settings.log_level)
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    logger.info("retention_worker_started", extra={"interval_hours": INTERVAL_HOURS})

    while True:
        try:
            async with factory() as session:
                service = DataRetentionService(session)
                result = await service.run_cleanup()
                await session.commit()
                logger.info("retention_cleanup_complete", extra=result.to_dict())
        except Exception:
            logger.warning("retention_cleanup_error")
        await asyncio.sleep(INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    asyncio.run(run())
