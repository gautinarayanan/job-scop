"""Load config.json, keywords.json, and proxies.

Settings are normally produced by the settings page (settings/index.html),
which validates them; this module re-validates so a hand-edited file fails
loudly instead of silently scraping the wrong thing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

ROOT = Path(__file__).resolve().parent.parent

JOB_TYPES = {"", "fulltime", "parttime", "contract", "internship"}
OUTPUT_FORMATS = {"csv", "json"}


@dataclass
class Config:
    keywords: List[str]

    # What to search
    sites: List[str] = field(default_factory=lambda: ["linkedin", "indeed"])
    locations: List[str] = field(default_factory=lambda: ["United States"])
    distance_miles: int = 50
    posted_within_hours: Union[int, str] = "auto"   # "auto" = scrape_every_days * 24, 0 = any
    job_type: str = ""                              # "", fulltime, parttime, contract, internship
    remote_only: bool = False
    easy_apply: bool = False
    country_indeed: str = "USA"
    annual_salary: bool = False                     # convert hourly/monthly pay to yearly

    # How much and how often
    scrape_every_days: int = 2
    jobs_per_site: int = 20
    max_per_search: int = 10
    max_searches_per_site: int = 8
    max_mb_per_run: float = 25

    # Output
    output_format: str = "csv"
    timezone: str = "UTC"                           # IANA name; decides the date in file names
    drive_folder_name: str = "Job Scout"
    email_when_no_new_jobs: bool = False

    proxies: List[str] = field(default_factory=list)

    @property
    def hours_old(self) -> Optional[int]:
        if self.posted_within_hours == "auto":
            return self.scrape_every_days * 24
        return int(self.posted_within_hours) or None


def parse_proxies(raw: Optional[str]) -> List[str]:
    """One proxy per line (or comma-separated), in any Webshare download format.

    host:port            IP-authorization lists: your IP must be allowlisted
    host:port:user:pass  username/password lists: work from anywhere
    user:pass@host:port  already in jobspy's format
    """
    if not raw:
        return []
    out = []
    for line in raw.replace(",", "\n").splitlines():
        p = line.strip()
        if not p or p.startswith("#"):
            continue
        p = p.removeprefix("http://").removeprefix("https://")
        if "@" not in p and p.count(":") == 3:
            host, port, user, pwd = p.split(":")
            p = f"{user}:{pwd}@{host}:{port}"
        out.append(p)
    return out


def _load_proxies() -> List[str]:
    """WEBSHARE_PROXIES env var (used in GitHub Actions), else ./proxies.txt."""
    raw = os.getenv("WEBSHARE_PROXIES")
    if raw is None:
        local = ROOT / "proxies.txt"
        raw = local.read_text() if local.exists() else ""
    return parse_proxies(raw)


def _validate(cfg: Config) -> None:
    problems = []
    cfg.sites = [s.lower() for s in cfg.sites]
    if not cfg.sites or set(cfg.sites) - {"linkedin", "indeed"}:
        problems.append("sites must be a non-empty list of 'linkedin' and/or 'indeed'")
    if not cfg.locations or not all(isinstance(x, str) and x.strip() for x in cfg.locations):
        problems.append("locations must be a non-empty list of strings")
    if cfg.job_type not in JOB_TYPES:
        problems.append(f"job_type must be one of {sorted(JOB_TYPES)}")
    if cfg.output_format not in OUTPUT_FORMATS:
        problems.append("output_format must be 'csv' or 'json'")
    if cfg.posted_within_hours != "auto" and not (isinstance(cfg.posted_within_hours, int) and cfg.posted_within_hours >= 0):
        problems.append("posted_within_hours must be 'auto' or a whole number >= 0")
    for name in ("scrape_every_days", "jobs_per_site", "max_per_search", "max_searches_per_site"):
        v = getattr(cfg, name)
        if not isinstance(v, int) or v < 1:
            problems.append(f"{name} must be a whole number >= 1")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(cfg.timezone)
    except Exception:
        problems.append(f"timezone '{cfg.timezone}' is not a valid IANA name, e.g. America/Los_Angeles")
    if problems:
        raise ValueError("config.json problems:\n  - " + "\n  - ".join(problems))


def load_config() -> Config:
    raw = json.loads((ROOT / "config.json").read_text())
    keywords = json.loads((ROOT / "keywords.json").read_text())
    if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
        raise ValueError('keywords.json must be a JSON list of strings, e.g. ["Data Analyst", "UX Researcher"]')
    keywords = list(dict.fromkeys(k.strip() for k in keywords if k.strip()))
    if not keywords:
        raise ValueError("keywords.json is empty")

    known = {f for f in Config.__dataclass_fields__ if f not in ("keywords", "proxies")}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Unknown keys in config.json: {sorted(unknown)}")

    cfg = Config(keywords=keywords, **raw)
    _validate(cfg)
    cfg.proxies = _load_proxies()
    return cfg
