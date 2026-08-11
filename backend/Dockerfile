FROM python:3.12-slim AS base

RUN groupadd --gid 1000 fonely && \
    useradd --uid 1000 --gid fonely --create-home fonely

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
# Install locked third-party dependencies before source copy for layer caching.
# The project itself is installed non-editably after its package is present.
RUN uv sync --locked --no-dev --no-install-project

COPY src/ src/
COPY migrations/ migrations/
COPY alembic.ini ./
COPY run.py run_worker.py run_inbound_worker.py run_retention_worker.py ./

RUN uv sync --locked --no-dev --no-editable

USER fonely
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready')" || exit 1

CMD [".venv/bin/python", "run.py"]
