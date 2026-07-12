FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

COPY pyproject.toml uv.lock /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra dev

COPY src /app/src
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra dev

CMD ["uvicorn", "vsniper.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/app/src"]
