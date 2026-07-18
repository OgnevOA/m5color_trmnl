# --- Stage 1: build the React control panel (web/dist) --------------------- #
FROM node:20-slim AS webbuild
WORKDIR /web
# Install deps first for better layer caching.
COPY web/package.json web/package-lock.json* ./
RUN npm install
# Build the SPA.
COPY web/ ./
RUN npm run build

# --- Stage 2: the app image ------------------------------------------------ #
# Use the official Playwright Python image: Chromium and all required system
# libraries for headless rendering are already installed.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Ensure the Chromium build matching the pinned Playwright version is present.
RUN python -m playwright install chromium

# Copy the application (web/dist is .dockerignored; the built bundle is copied
# from the Node stage below).
COPY . .

# Built web control panel, served by FastAPI at the site root.
COPY --from=webbuild /web/dist ./web/dist

# Persistent data (SQLite DB, rendered images, uploads) lives here.
ENV APP_ENV=production \
    DATA_DIR=/data \
    DATABASE_PATH=/data/trmnl.db \
    RENDERED_IMAGES_DIR=/data/rendered \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000
VOLUME ["/data"]

# Runs the combined process: FastAPI API + pre-render worker + Telegram bot.
CMD ["python", "server.py"]
