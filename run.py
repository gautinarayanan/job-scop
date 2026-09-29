"""Job Scout entry point.

    python run.py              # scrape if scrape_every_days have passed, upload, email
    python run.py --force      # scrape now, ignoring the interval
    python run.py --dry-run    # scrape and write ./out/<date>_jobslist.csv; no Google, no email
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from scout.config import ROOT, load_config
from scout.scrape import COLUMNS, scrape_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
for noisy in ("JobSpy", "urllib3", "googleapiclient.discovery_cache"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("scout")

OUT = ROOT / "out"


def step_summary(markdown: str) -> None:
    """Show a summary on the GitHub Actions run page, when running there."""
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")


def write_local(jobs, today: str, fmt: str):
    OUT.mkdir(exist_ok=True)
    path = OUT / f"{today}_jobslist.{fmt}"
    if fmt == "json":
        path.write_text(json.dumps([{c: j[c] for c in COLUMNS} for j in jobs], indent=2))
    else:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(jobs)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape jobs to Google Drive and track applications.")
    ap.add_argument("--force", action="store_true", help="scrape now, ignoring scrape_every_days")
    ap.add_argument("--dry-run", action="store_true", help="no Google, no email; write ./out/ locally")
    args = ap.parse_args()

    cfg = load_config()
    today_d = datetime.now(ZoneInfo(cfg.timezone)).date()
    today = today_d.isoformat()

    store = None
    if args.dry_run:
        seen_file = OUT / "seen.txt"
        seen = set(seen_file.read_text().split("\n")) - {""} if seen_file.exists() else set()
    else:
        from scout.google_drive import GoogleStore
        store = GoogleStore(cfg.drive_folder_name)
        last = store.last_scrape_date(cfg.timezone)
        if last and not args.force:
            days = (today_d - last).days
            if days < cfg.scrape_every_days:
                log.info(f"Last scrape was {days} day(s) ago ({last}); scraping every "
                         f"{cfg.scrape_every_days} days, so nothing to do today.")
                step_summary(f"### Job Scout {today}\nSkipped: last scrape {last}, "
                             f"interval {cfg.scrape_every_days} days.\n")
                return 0
        seen = store.load_seen()
    log.info(f"{len(seen)} previously seen job keys")

    results, meter = scrape_all(cfg, seen, today_d)
    jobs = [j for r in results for j in r.jobs]
    keys = [k for j in jobs for k in j.pop("_keys")]
    mb = meter.used / 1e6

    for r in results:
        note = " (stopped: byte budget)" if r.stopped_by_budget else ""
        log.info(f"{r.site}: {len(r.jobs)} new jobs from {r.searches} searches, {r.errors} errors{note}")
    runs_per_month = 30 / cfg.scrape_every_days
    log.info(f"Bandwidth: ~{mb:.2f} MB over {meter.requests} requests "
             f"(~{mb * runs_per_month:.1f} MB/month at one scrape every {cfg.scrape_every_days} day(s))")

    searches = sum(r.searches for r in results)
    all_failed = searches > 0 and sum(r.errors for r in results) == searches
    if all_failed:
        # Upload nothing: a failed run must not count as a scrape, or the
        # interval check would skip tomorrow's retry.
        log.error("Every search failed. Check the proxies, or whether a site is blocking.")
        step_summary(f"### Job Scout {today}\n**Failed**: every search errored.\n")
        return 1

    file_name = file_url = None
    if args.dry_run:
        path = write_local(jobs, today, cfg.output_format)
        with open(OUT / "seen.txt", "a", encoding="utf-8") as f:
            f.writelines(k + "\n" for k in keys)
        log.info(f"Dry run: wrote {len(jobs)} jobs to {path}")
    else:
        # Always upload, even when empty: the file's date is what the interval
        # check reads, and an empty file is an honest record that a run happened.
        file_name, file_url = store.upload_scrape(jobs, COLUMNS, today, cfg.output_format)
        store.add_jobs(jobs, file_name, file_url)
        store.add_seen(keys, today)
        log.info(f"Uploaded {file_name} and added {len(jobs)} jobs to the master sheet")
        if jobs or cfg.email_when_no_new_jobs:
            from scout.notify import send_summary
            send_summary(jobs, file_name, file_url, store.master_url, mb, today)
        log.info(f"Google API calls this run: {store.calls}")

    step_summary(
        f"### Job Scout {today}\n"
        f"- New jobs: **{len(jobs)}** (" + ", ".join(f"{r.site} {len(r.jobs)}" for r in results) + ")\n"
        f"- Bandwidth: ~{mb:.2f} MB\n"
        + (f"- File: [{file_name}]({file_url})\n" if file_url else "")
        + (f"- [Master sheet]({store.master_url})\n" if store else ""))

    return 0


if __name__ == "__main__":
    sys.exit(main())
