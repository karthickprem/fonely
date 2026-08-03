"""Notification worker entrypoint."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fonely.core.config import settings
from fonely.core.logging_config import configure_logging
from fonely.workers.notification_worker import (
    LoggingNotificationSender,
    NotificationSender,
    run_notification_worker,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("fonely.workers.main")


def _create_sender() -> NotificationSender:
    if settings.whatsapp_access_token and settings.whatsapp_phone_number_id:
        from fonely.services.whatsapp_notification_sender import WhatsAppNotificationSender
        from fonely.services.whatsapp_sender import WhatsAppSender

        whatsapp = WhatsAppSender(
            access_token=settings.whatsapp_access_token,
            phone_number_id=settings.whatsapp_phone_number_id,
        )
        logger.info("notification_sender_configured", extra={"type": "whatsapp"})
        return WhatsAppNotificationSender(whatsapp)

    logger.info("notification_sender_configured", extra={"type": "logging"})
    return LoggingNotificationSender()


async def main() -> None:
    configure_logging(settings.log_format, settings.log_level)
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sender = _create_sender()
    try:
        await run_notification_worker(factory, sender)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
