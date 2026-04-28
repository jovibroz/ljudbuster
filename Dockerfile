# ===================================================
# LJUDBUSTER - Dockerfile
# Version: 1.2.1 av Johan Hörnqvist
# Uppdaterad: 2026-02-23
# Changelog:
# - Playwright: Låser browser-path till /ms-playwright (stabilt i Docker + funkar med UID 1026).
# - Installerar Chromium via playwright install under build så browser-binary alltid finns.
# - Behåller ffmpeg + nodejs (yt-dlp/EJS) + curl (healthcheck).
# ===================================================

FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Systempaket: ffmpeg + nodejs + curl + Playwright/Chromium-deps
RUN apt-get update && apt-get install -y \
    ffmpeg \
    nodejs \
    curl \
    ca-certificates \
    libnss3 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libgtk-3-0 \
    libx11-6 libx11-xcb1 libxcb1 libxext6 libxrender1 \
  && rm -rf /var/lib/apt/lists/*

# Debian-fix: säkerställ att binären heter 'node'
RUN ln -sf /usr/bin/nodejs /usr/local/bin/node || true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installera Chromium till deterministisk path (inte ~/.cache)
RUN mkdir -p /ms-playwright \
  && python -m playwright install chromium

COPY . .

RUN mkdir -p /output /app/state
RUN chmod +x entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
