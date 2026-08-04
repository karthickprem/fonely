"""Notification worker entrypoint with trusted WhatsApp channel routing."""

import asyncio
import json
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fonely.core.config import settings
from fonely.core.logging_config import configure_logging
from fonely.workers.notification_worker import (
    NotificationSender,
    run_notification_worker,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
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
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sender = _create_sender()
    try:
        await run_notification_worker(factory, sender)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
