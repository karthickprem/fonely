"""Inbound WhatsApp event worker entrypoint.

Fails closed if SARVAM_API_KEY is not configured.
Cooperative shutdown: SIGTERM sets stop event, worker finishes current
unit (no new claims), then exits. Force cancel after configured timeout.
"""

import asyncio
import contextlib
import logging
import signal
import sys

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fonely.core.config import settings
from fonely.core.logging_config import configure_logging
from fonely.services.model_gateway import SarvamModelGateway
from fonely.workers.inbound_worker import run_inbound_worker

logger = logging.getLogger("fonely.workers.inbound.main")


async def main() -> None:
    configure_logging(settings.log_format, settings.log_level)

    if not settings.sarvam_api_key:
        logger.error("sarvam_api_key_missing")
        sys.exit(1)

    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    http_client = httpx.AsyncClient()
    gateway = SarvamModelGateway(client=http_client)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    logger.info("inbound_worker_starting")
    task = loop.create_task(run_inbound_worker(factory, gateway, stop=stop))
    stop_task = loop.create_task(stop.wait())

    try:
        done, _ = await asyncio.wait(
            [task, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop.is_set():
            logger.info("inbound_worker_draining")
            if task not in done:
                try:
                    await asyncio.wait_for(task, timeout=settings.shutdown_timeout_seconds)
                except TimeoutError:
                    logger.warning("inbound_worker_drain_timeout")
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        elif task in done:
            exc = task.exception()
            if exc:
                logger.error("inbound_worker_crashed", exc_info=exc)
                raise exc
            logger.error("inbound_worker_unexpected_exit")
            sys.exit(1)
    finally:
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        with contextlib.suppress(Exception):
            await http_client.aclose()
        await engine.dispose()
        logger.info("inbound_worker_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        logger.error("inbound_worker_fatal", exc_info=True)
        sys.exit(1)
