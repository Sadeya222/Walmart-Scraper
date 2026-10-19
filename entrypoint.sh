#!/usr/bin/env bash
# Container entry point.
#
# Verifies the environment, then hands every argument straight to the Python
# runner so the same flags work locally, in Docker and on Azure:
#
#   docker run image                            -> plan preview (safe default)
#   docker run image --crawl                    -> real crawl
#   docker run image --crawl --store 2457       -> one store
set -euo pipefail

if [[ "${1:-}" == "--crawl" ]]; then
    shift
    ARGS=("$@")
else
    ARGS=("$@")
    if [[ ${#ARGS[@]} -eq 0 ]]; then
        ARGS=("--dry-run")
    fi
fi

echo "[entrypoint] python:  $(python --version 2>&1)"
echo "[entrypoint] scrapy:  $(python -c 'import scrapy; print(scrapy.__version__)')"
echo "[entrypoint] args:    ${ARGS[*]:-<none>}"

# Surface configuration problems before the browser starts.
python -c "from walmart_scraper.config_loader import ScraperConfig; ScraperConfig.from_env()" \
    || { echo "[entrypoint] configuration is invalid - refusing to start"; exit 2; }

exec python run_scraper.py "${ARGS[@]}"
