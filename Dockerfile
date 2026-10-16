# Playwright image ships Chromium + all its OS libraries + the matching playwright package.
# Match this tag to your playwright version (run: python -m playwright --version).
FROM mcr.microsoft.com/playwright/python:v1.64.0-noble

WORKDIR /app

# App dependencies. Do NOT reinstall playwright here — the base image already has it with the browsers.
RUN pip install --no-cache-dir "httpx[http2]>=0.26" "fastapi>=0.110" "uvicorn>=0.29" "cryptography>=42"

COPY . .

ENV FB_SCRAPER_DATA=/data \
    PLAYWRIGHT_NO_SANDBOX=1
VOLUME ["/data"]
EXPOSE 8765
CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]
