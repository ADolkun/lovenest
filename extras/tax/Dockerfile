FROM ghcr.io/astral-sh/uv:0.12.10 AS uv

FROM node:24-slim AS assets

WORKDIR /assets
RUN npm install --ignore-scripts --no-audit --no-fund alpinejs@3.15.12

FROM python:3.14-slim

WORKDIR /app

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml uv.lock .python-version ./
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

COPY app.py tax_engine.py index.html ./
COPY static/styles.css ./static/styles.css
COPY --from=assets /assets/node_modules/alpinejs/dist/cdn.min.js ./static/alpine.min.js

EXPOSE 8088

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8088"]
