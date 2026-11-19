# Convenience targets.  `make test` is the check that must pass before a deploy.
VENV ?= .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.PHONY: help install browsers dev test lint coverage plan crawl evidence docker-build docker-crawl clean

help:
	@echo "install       create .venv and install pinned dependencies"
	@echo "browsers      download the Playwright Chromium binary"
	@echo "test          run the full suite (unit + end-to-end crawl)"
	@echo "lint          flake8 over the package, runner and tests"
	@echo "plan          print the crawl plan without crawling (--dry-run)"
	@echo "crawl         run the crawl"
	@echo "evidence      run the crawl with raw-evidence capture and a timestamped log"
	@echo "docker-build  build the production image"
	@echo "clean         remove run artifacts and caches"

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt

browsers:
	$(PY) -m playwright install --with-deps chromium

dev: install

test:
	$(PY) -m pytest tests -q

lint:
	$(PY) -m flake8 --max-line-length=110 --extend-ignore=E203,W503 walmart_scraper run_scraper.py tests

coverage:
	$(PY) -m pytest tests -q --cov=walmart_scraper --cov-report=term-missing

plan:
	$(PY) run_scraper.py --dry-run

crawl:
	$(PY) run_scraper.py

# The run to perform for a contest/acceptance review: raw evidence for up to 2
# products per store + a timestamped log under output/.
evidence:
	$(PY) run_scraper.py --evidence --log-file "output/run-$$(date +%Y%m%dT%H%M%SZ).log"

docker-build:
	docker build -t walmart-scraper:local .

docker-crawl:
	docker compose run --rm scraper --crawl

clean:
	rm -rf .pytest_cache htmlcov .coverage output state quarantine
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
