"""Azure Functions (Python v2 programming model) timer trigger.

Runs the scraper on a schedule inside the same Docker image, so the code path is
identical to a local run.  Two triggers are provided:

* ``walmart_daily_crawl`` - cron schedule, starts a crawl.
* ``walmart_health``      - HTTP endpoint returning the last run manifest, so an
  alert can fire when the previous run withheld its dataset.

Build/run with the Dockerfile in this folder (or reuse the repo Dockerfile).
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import azure.functions as func

app = func.FunctionApp()

APP_DIR = os.environ.get("WMT_APP_DIR", "/app")
OUTPUT_DIR = os.environ.get("WMT_OUTPUT_DIR", "/app/output")


@app.schedule(
    schedule="0 30 6 * * *",  # 06:30 daily - override with WMT_CRON
    arg_name="timer",
    function_name="walmart_daily_crawl",
)
def walmart_daily_crawl(timer: func.TimerRequest) -> None:
    """Start a crawl and fail the function when the dataset was not published."""
    if timer.past_due:
        logging.warning("timer was past due at %s", datetime.datetime.now(datetime.timezone.utc))

    args = [sys.executable, "run_scraper.py"]
    if os.environ.get("WMT_RESUME", "true").lower() in {"1", "true", "yes"}:
        args.append("--resume")

    logging.info("starting scraper: %s", " ".join(args))
    completed = subprocess.run(args, cwd=APP_DIR, capture_output=True, text=True)
    logging.info("scraper stdout:\n%s", completed.stdout[-8000:])
    if completed.stderr:
        logging.warning("scraper stderr:\n%s", completed.stderr[-4000:])

    # Exit codes: 0 published, 2 config, 3 blocked, 4 withheld, 5 empty.
    if completed.returncode != 0:
        raise RuntimeError(
            f"scraper exited {completed.returncode}: {completed.stdout[-2000:]}"
        )


@app.route(route="walmart/health", auth_level=func.AuthLevel.FUNCTION, function_name="walmart_health")
def walmart_health(req: func.HttpRequest) -> func.HttpResponse:
    """Return the last run manifest (200 when published, 503 when withheld)."""
    manifest_path = Path(OUTPUT_DIR) / "run_manifest.json"
    if not manifest_path.exists():
        return func.HttpResponse(
            json.dumps({"status": "no_run_yet"}), status_code=404, mimetype="application/json"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return func.HttpResponse(
            json.dumps({"status": "corrupt_manifest", "error": str(exc)}),
            status_code=500, mimetype="application/json",
        )
    published = bool(manifest.get("dataset", {}).get("published"))
    return func.HttpResponse(
        json.dumps(manifest),
        status_code=200 if published else 503,
        mimetype="application/json",
    )
