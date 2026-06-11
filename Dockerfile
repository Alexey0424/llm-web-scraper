# Real-Estate Agent Scraper — container image.
# All project software runs inside this container.
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
# This ensures the browser binaries are installed in a predictable place 
# and that Scrapling/Playwright can find them.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV SCRAPLING_BROWSER_PATH=/scrapling-browsers


# System deps: lxml build needs libxml2/libxslt; browsers need a few libs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libxml2-dev libxslt1-dev ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Install the browsers Scrapling's fetch engines need: Playwright Chromium for
# DynamicFetcher, plus the browser set `scrapling install` provides for
# StealthyFetcher (the default SCRAPER_MODE).
RUN playwright install --with-deps chromium && \
    scrapling install

COPY . .

# data/ and runs/ are mounted as volumes at runtime (see docker-compose.yml).
ENTRYPOINT ["python", "-m", "src.main"]
CMD ["--help"]
