# Azure Functions custom container for the scraper.
# Reuses the browser image so the scheduled job runs exactly like a local crawl.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

ENV AzureWebJobsScriptRoot=/home/site/wwwroot \
    FUNCTIONS_EXTENSION_VERSION=~4 \
    FUNCTIONS_WORKER_RUNTIME=python \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY ../../requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY ../../walmart_scraper /app/walmart_scraper
COPY ../../run_scraper.py /app/
COPY ../../scrapy.cfg /app/
COPY ../../config /app/config
COPY . /home/site/wwwroot

EXPOSE 80
CMD ["azure-functions-host"]
