"""Notification worker entrypoint with trusted WhatsApp channel routing.

Cooperative shutdown: SIGTERM sets stop event, worker finishes current
unit (no new claims), then exits. Force cancel after configured timeout.
"""

import asyncio
import contextlib
import json
import logging
import signal
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fonely.core.config import settings
from fonely.core.logging_config import configure_logging
from fonely.workers.notification_worker import (
    NotificationSender,
    run_notification_worker,
)

logger = logging.getLogger("fonely.workers.main")


def _create_sender() -> NotificationSender:
    if not settings.whatsapp_access_token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN is required")
    if not settings.whatsapp_business_mappings:
        raise RuntimeError("WHATSAPP_BUSINESS_MAPPINGS is required")
    try:
        mappings_raw = json.loads(settings.whatsapp_business_mappings)
        mappings = {str(key): int(value) for key, value in mappings_raw.items()}
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("WHATSAPP_BUSINESS_MAPPINGS is invalid") from exc
    if not mappings:
        raise RuntimeError("WHATSAPP_BUSINESS_MAPPINGS must not be empty")

    from fonely.services.whatsapp_notification_sender import (
        ConfiguredWhatsAppSenderResolver,
        WhatsAppNotificationSender,
    )

    resolver = ConfiguredWhatsAppSenderResolver(
        access_token=settings.whatsapp_access_token,
        business_mappings=mappings,
    )
    logger.info("notification_sender_configured", extra={"type": "whatsapp_resolver"})
    return WhatsAppNotificationSender(resolver=resolver)


async def main() -> None:
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
    sender = _create_sender()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    logger.info("notification_worker_starting")
    task = loop.create_task(run_notification_worker(factory, sender, stop=stop))
    stop_task = loop.create_task(stop.wait())

    try:
        done, _ = await asyncio.wait(
            [task, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop.is_set():
            logger.info("notification_worker_draining")
            if task not in done:
                try:
                    await asyncio.wait_for(task, timeout=settings.shutdown_timeout_seconds)
                except TimeoutError:
                    logger.warning("notification_worker_drain_timeout")
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        elif task in done:
            exc = task.exception()
            if exc:
                logger.error("notification_worker_crashed", exc_info=exc)
                raise exc
            logger.error("notification_worker_unexpected_exit")
            sys.exit(1)
    finally:
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        await engine.dispose()
        logger.info("notification_worker_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        logger.error("notification_worker_fatal", exc_info=True)
        sys.exit(1)
