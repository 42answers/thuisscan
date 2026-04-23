# Thuisscan — single-container deployment.
# Frontend (apps/web/) en backend (apps/api/) draaien als één FastAPI-proces;
# dezelfde setup als lokaal op port 8765.
FROM python:3.12-slim

# Build-deps minimaal houden — geen native libs nodig (httpx is pure Python)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies eerst — krijgt eigen layer zodat code-changes geen pip-install triggeren
COPY apps/api/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Playwright Chromium — vereist voor /rapport.pdf endpoint.
# `--with-deps` installeert ook libnss3, libatk1.0-0, etc. (~150 MB).
# We zetten BROWSERS_PATH expliciet zodat het pad voorspelbaar is.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN python -m playwright install --with-deps chromium

# Applicatie-code
COPY apps/api  /app/apps/api
COPY apps/web  /app/apps/web

# Cache + data dirs (volume mount op Fly.io)
RUN mkdir -p /app/apps/api/cache

WORKDIR /app/apps/api

# Fly.io zet PORT in de env; we falle back op 8000.
EXPOSE 8000
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
