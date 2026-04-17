# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

# Install ffmpeg (required by the align module to decode arbitrary audio formats)
# and curl for the uv installer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv (copied from the official image for reproducibility)
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Install dependencies first (better layer caching)
COPY pyproject.toml ./
RUN uv sync --no-install-project --no-dev

# Copy application code
COPY app ./app

# Install the project itself (so `from app...` works out of the installed env)
RUN uv sync --no-dev

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
