FROM ghcr.io/astral-sh/uv:0.12.10 AS uv
FROM python:3.14-slim

WORKDIR /app

# Install system deps for asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Dependencies come from uv.lock (the source of truth — see scripts/lock.sh).
# The export never touches the lock (--frozen); uv requires matching hashes
# for every dependency. Mount uv only during builds so it stays out of the image.
# This layer is cached until pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv export --frozen --no-emit-project -o /tmp/requirements.txt \
    && uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY . .
# The project itself: deps are already satisfied above, so --no-deps keeps
# this layer from re-resolving anything outside the lock.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv pip install --system --no-cache --no-deps -e .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
