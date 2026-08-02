FROM python:3.12-slim AS base

RUN groupadd --gid 1000 fonely && \
    useradd --uid 1000 --gid fonely --create-home fonely

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

COPY src/ src/
COPY migrations/ migrations/
COPY alembic.ini ./
COPY run.py ./

RUN uv pip install --no-deps -e .

USER fonely
EXPOSE 8000

CMD [".venv/bin/python", "run.py"]
