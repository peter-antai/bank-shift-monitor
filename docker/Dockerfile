FROM python:3.12-slim

# ------------------------------------------------------------
# Playwright drives a real Chromium, which needs a long list
# of system libraries. Rather than list them by hand we let
# Playwright install its own dependencies.
# ------------------------------------------------------------

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------
# Application code.
#
# monitor/ is on the path so the tools can import it.
# ------------------------------------------------------------

COPY monitor/ /app/monitor/
COPY tools/ /app/tools/

ENV PYTHONPATH=/app/monitor

# ------------------------------------------------------------
# Writable state lives in a mounted volume, never in the image.
# ------------------------------------------------------------

ENV ALLOCATE_HOME=/data

VOLUME ["/data"]

CMD ["python", "/app/monitor/allocate_monitor_live.py"]
