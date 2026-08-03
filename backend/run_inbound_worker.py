"""Inbound WhatsApp event worker entrypoint."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fonely.core.config import settings
from fonely.core.logging_config import configure_logging
from fonely.services.whatsapp_sender import WhatsAppSender
from fonely.workers.inbound_worker import run_inbound_worker

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("fonely.workers.inbound.main")


def _create_sender() -> WhatsAppSender | None:
    if settings.whatsapp_access_token and settings.whatsapp_phone_number_id:
        return WhatsAppSender(
            access_token=settings.whatsapp_access_token,
            phone_number_id=settings.whatsapp_phone_number_id,
        )
    return None


def _create_model_gateway() -> object | None:
    if settings.sarvam_api_key:
        import httpx

        from fonely.services.model_gateway import SarvamModelGateway

        return SarvamModelGateway(client=httpx.AsyncClient())
    return None


async def main() -> None:
    configure_logging(settings.log_format, settings.log_level)
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sender = _create_sender()
    gateway = _create_model_gateway()
    logger.info("inbound_worker_starting")
    try:
        await run_inbound_worker(factory, gateway, sender)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
