FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NEXT_TELEMETRY_DISABLED=1 \
    NODE_OPTIONS=--max-old-space-size=768

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY web/package*.json ./web/
WORKDIR /app/web
RUN npm ci

WORKDIR /app
COPY . .

WORKDIR /app/web
RUN npm run build

WORKDIR /app
COPY start.sh ./start.sh

EXPOSE 8000 3000

CMD ["sh", "/app/start.sh"]
