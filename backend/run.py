"""Production entrypoint for the Fonely backend.

Runs uvicorn with configuration from environment variables.
Single-worker mode is correct for initial staging with 1-5 clinics.
"""

import uvicorn

from fonely.core.config import settings


def main() -> None:
    uvicorn.run(
        "fonely.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=True,
        workers=1,
    )


if __name__ == "__main__":
    main()
