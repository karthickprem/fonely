"""Standalone retention cleanup worker entrypoint.

Cooperative shutdown: SIGTERM sets stop event. Current cleanup iteration
completes (bounded by DataRetentionService batch size). Failure uses
bounded exponential backoff, not the normal 6-hour interval.
"""

import asyncio
import contextlib
import logging
import signal
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fonely.core.config import settings
from fonely.core.logging_config import configure_logging
from fonely.services.data_retention import DataRetentionService

logger = logging.getLogger("fonely.workers.retention")

INTERVAL_HOURS = 6
MAX_FAILURE_BACKOFF = 300


async def run() -> None:
    configure_logging(settings.log_format, settings.log_level)
    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    logger.info("retention_worker_started", extra={"interval_hours": INTERVAL_HOURS})
    consecutive_failures = 0

    try:
        while not stop.is_set():
            try:
                async with factory() as session:
                    service = DataRetentionService(session)
                    result = await service.run_cleanup()
                    await session.commit()
                    logger.info("retention_cleanup_complete", extra=result.to_dict())
                consecutive_failures = 0
                wait = INTERVAL_HOURS * 3600
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                wait = min(MAX_FAILURE_BACKOFF, 2**consecutive_failures)
                logger.warning(
                    "retention_cleanup_error",
                    exc_info=True,
                    extra={"consecutive_failures": consecutive_failures, "retry_in": wait},
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=wait)
    finally:
        await engine.dispose()
        logger.info("retention_worker_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except Exception:
        sys.exit(1)
