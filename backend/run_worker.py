"""Notification worker entrypoint with trusted WhatsApp channel routing."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fonely.core.config import settings
from fonely.core.logging_config import configure_logging
from fonely.workers.notification_worker import (
    NotificationSender,
    run_notification_worker,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("fonely.workers.main")


def _create_sender(session_factory: async_sessionmaker[AsyncSession]) -> NotificationSender:
    """Build the outbound sender.

    Channel identity is no longer a process setting: which provider number
    belongs to which tenant lives in business_whatsapp_channels (migration
    0016). The resolver re-reads ownership at delivery time, so there is
    nothing left to validate at startup beyond the shared access token.
    """
    if not settings.whatsapp_access_token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN is required")

    from fonely.services.whatsapp_notification_sender import (
        DatabaseWhatsAppSenderResolver,
        WhatsAppNotificationSender,
    )

    resolver = DatabaseWhatsAppSenderResolver(
        access_token=settings.whatsapp_access_token,
        session_factory=session_factory,
    )
    logger.info("notification_sender_configured", extra={"type": "whatsapp_db_resolver"})
    return WhatsAppNotificationSender(resolver=resolver)


async def main() -> None:
    configure_logging(settings.log_format, settings.log_level)
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sender = _create_sender(factory)
    try:
        await run_notification_worker(factory, sender)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
