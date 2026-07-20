FROM node:22-slim AS assets

WORKDIR /assets
RUN npm install --ignore-scripts --no-audit --no-fund alpinejs@3.15.12

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py tax_engine.py index.html ./
COPY static/styles.css ./static/styles.css
COPY --from=assets /assets/node_modules/alpinejs/dist/cdn.min.js ./static/alpine.min.js

EXPOSE 8088

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8088"]
