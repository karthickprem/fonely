"""Inbound WhatsApp event worker entrypoint.

Fails closed if SARVAM_API_KEY is not configured — the worker cannot
process messages without a model gateway.
"""

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fonely.core.config import settings
from fonely.core.logging_config import configure_logging
from fonely.workers.inbound_worker import run_inbound_worker

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("fonely.workers.inbound.main")


def _create_model_gateway() -> object:
    if not settings.sarvam_api_key:
        raise RuntimeError(
            "SARVAM_API_KEY is required for the inbound worker. "
            "The worker cannot process messages without a model gateway."
        )
    import httpx

    from fonely.services.model_gateway import SarvamModelGateway

    return SarvamModelGateway(client=httpx.AsyncClient())


async def main() -> None:
    configure_logging(settings.log_format, settings.log_level)
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    gateway = _create_model_gateway()
    logger.info("inbound_worker_starting")
    try:
        await run_inbound_worker(factory, gateway)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
