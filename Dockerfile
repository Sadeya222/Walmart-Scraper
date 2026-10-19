# ---------------------------------------------------------------------------
# Walmart catalog scraper - production image.
#
# Base image ships the Playwright browsers and their OS dependencies, matching
# the pinned playwright version in requirements.txt.  Bump both together.
# ---------------------------------------------------------------------------
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code changes do not invalidate the layer cache.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY scrapy.cfg pyproject.toml ./
COPY walmart_scraper ./walmart_scraper
COPY run_scraper.py ./
COPY config ./config
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Artifacts live on a volume so a container restart cannot lose a published run.
RUN mkdir -p /app/output /app/state /app/quarantine \
    && useradd --create-home --uid 10001 scraper \
    && chown -R scraper:scraper /app
USER scraper

# Fail fast if the browser is missing, before the crawl burns time.
HEALTHCHECK NONE

ENTRYPOINT ["./entrypoint.sh"]
CMD ["--dry-run"]
