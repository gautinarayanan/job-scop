"""Scrape LinkedIn and Indeed through jobspy, keeping only the columns we need.

Bandwidth is the scarce resource (Webshare free tier: 1 GB/month), so this
module does three things the stock jobspy call does not:

1. LinkedIn: linkedin_fetch_description=False. Otherwise jobspy downloads the
   full HTML page of every job, one extra request per listing.
2. Indeed: jobspy's GraphQL query hard-codes `limit: 100` and asks for the
   employer "dossier" (CEO name, company blurb, logos, addresses). We rewrite
   the query at runtime to ask for only `max_per_search` jobs and no dossier.
   Indeed's description text cannot be dropped: jobspy's parser reads it
   unconditionally. At 10 jobs a page that is small, and we discard it.
3. Every HTTP response is metered, and the run hard-stops at max_mb_per_run.
"""

from __future__ import annotations

import logging
import math
import random
import re
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Set

import requests

log = logging.getLogger("scout.scrape")

COLUMNS = ["Title", "Company", "Location", "Salary", "Site", "Link", "Found"]


# ── Byte meter ───────────────────────────────────────────────────────────────
class BudgetExceeded(Exception):
    """Raised before a request is sent once the run's byte budget is spent."""


def wire_bytes(response) -> int:
    """Best estimate of bytes that actually crossed the network (and the proxy).

    1. urllib3's raw.tell(): exact compressed bytes read (works for LinkedIn).
    2. Content-Length header: exact, when the server sends it.
    3. Chunked + gzip (Indeed) exposes neither, so re-compress the body to
       estimate the wire size. Falling back to the decompressed length would
       overstate usage several times over.
    """
    content = response.content or b""
    try:
        told = response.raw.tell()
        if told:
            return int(told)
    except Exception:
        pass
    length = response.headers.get("Content-Length")
    if length and length.isdigit():
        return int(length)
    if response.headers.get("Content-Encoding", "").lower() in ("gzip", "br", "deflate"):
        return len(zlib.compress(content, 6))
    return len(content)


class ByteMeter:
    """Counts approximate bytes received, across every requests.Session.

    Both jobspy scrapers use requests.Session subclasses, so patching
    Session.send once catches every response from both sites. Raising before
    the request goes out means an abort costs nothing, even inside jobspy code
    that swallows exceptions.
    """

    HEADER_OVERHEAD = 1_000  # rough allowance for headers and the request itself

    def __init__(self, limit_mb: float):
        self.limit = int(limit_mb * 1_000_000) if limit_mb else 0
        self.used = 0
        self.requests = 0
        self._lock = threading.Lock()
        self._original = None

    @property
    def exhausted(self) -> bool:
        return bool(self.limit) and self.used >= self.limit

    def install(self) -> None:
        if self._original is not None:
            return
        self._original = original = requests.Session.send
        meter = self

        def send(session, request, **kwargs):
            if meter.exhausted:
                raise BudgetExceeded(f"{meter.used / 1e6:.1f} MB used")
            response = original(session, request, **kwargs)
            body = wire_bytes(response)
            with meter._lock:
                meter.used += body + ByteMeter.HEADER_OVERHEAD
                meter.requests += 1
            return response

        requests.Session.send = send

    def uninstall(self) -> None:
        if self._original is not None:
            requests.Session.send = self._original
            self._original = None


# ── Indeed query trim ────────────────────────────────────────────────────────
def _strip_block(query: str, opener: str) -> str:
    """Remove `opener {{ ... }}` from a str.format template, braces balanced."""
    start = query.find(opener)
    if start == -1:
        return query
    i = query.index("{{", start)
    depth = 0
    while i < len(query):
        if query.startswith("{{", i):
            depth += 1
            i += 2
        elif query.startswith("}}", i):
            depth -= 1
            i += 2
            if depth == 0:
                return query[:start] + query[i:]
        else:
            i += 1
    return query  # unbalanced: leave untouched


def slim_indeed_query(per_page: int) -> bool:
    """Shrink jobspy's Indeed GraphQL query in place. Returns True if applied.

    If a future jobspy release changes the query text, this logs a warning and
    leaves it alone: results stay correct, the run just costs more bytes.
    """
    try:
        import jobspy.indeed as indeed_mod
    except ImportError:
        return False

    original = getattr(indeed_mod, "_scout_original_query", None) or indeed_mod.job_search_query
    indeed_mod._scout_original_query = original

    slim, n = re.subn(r"limit:\s*100\b", f"limit: {int(per_page)}", original)
    if n != 1:
        log.warning("Indeed query layout changed; page-size trim not applied")
        return False
    slim = _strip_block(slim, "dossier {{")
    indeed_mod.job_search_query = slim
    return True


# ── Search plan and normalisation ────────────────────────────────────────────
def search_plan(keywords: List[str], locations: List[str], today: date) -> List[tuple]:
    """Every (keyword, location) pair, rotated so each run starts somewhere new.

    Without rotation the first keyword would fill the quota every run and the
    rest would never be searched. The offset advances once every two days.
    """
    pairs = [(k, loc) for k in keywords for loc in locations]
    offset = (today.toordinal() // 2) % len(pairs)
    return pairs[offset:] + pairs[:offset]


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = " ".join(str(value).split())
    # Scraped text lands in a Google Sheet via CSV import, where a leading
    # = + @ would be evaluated as a formula. A leading space defuses that.
    return f" {text}" if text[:1] in ("=", "+", "@") else text


_INTERVALS = {"yearly": "yr", "monthly": "mo", "weekly": "wk", "daily": "day", "hourly": "hr"}


def format_salary(row: Dict) -> str:
    lo, hi = row.get("min_amount"), row.get("max_amount")
    lo = None if lo is None or (isinstance(lo, float) and math.isnan(lo)) else float(lo)
    hi = None if hi is None or (isinstance(hi, float) and math.isnan(hi)) else float(hi)
    if lo is None and hi is None:
        return ""
    currency = _clean(row.get("currency")) or "USD"
    sym = "$" if currency == "USD" else f"{currency} "
    per = _INTERVALS.get(_clean(row.get("interval")).lower(), "")

    def fmt(x: float) -> str:
        return f"{sym}{x:,.2f}" if x < 1000 and x != int(x) else f"{sym}{x:,.0f}"

    text = fmt(lo) if hi is None or lo == hi else (fmt(hi) if lo is None else f"{fmt(lo)} - {fmt(hi)}")
    return f"{text}/{per}" if per else text


def dedupe_keys(title: str, company: str, link: str) -> List[str]:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    keys = [link] if link else []
    if title and company:
        keys.append(f"{norm(title)}|{norm(company)}")
    return keys


# ── Proxy health check ───────────────────────────────────────────────────────
def healthy_proxies(proxies: List[str], timeout: float = 8) -> List[str]:
    """Drop proxies that refuse us, so dead ones don't fail real searches.

    Uses plain HTTP to a tiny endpoint (~300 bytes each, no TLS handshake).
    A 407 means the proxy wants credentials: an IP-authorization list used
    from an IP that is not on the Webshare allowlist.
    """
    if not proxies:
        return []
    from concurrent.futures import ThreadPoolExecutor

    def check(p: str):
        url = f"http://{p}"
        try:
            r = requests.get("http://api.ipify.org", proxies={"http": url, "https": url}, timeout=timeout)
            return p, r.ok, f"HTTP {r.status_code}"
        except Exception as e:
            reason = "407 not authorized for this IP" if "407" in str(e) else type(e).__name__
            return p, False, reason

    with ThreadPoolExecutor(max_workers=min(10, len(proxies))) as pool:
        results = list(pool.map(check, proxies))
    good = [p for p, ok, _ in results if ok]
    for p, ok, reason in results:
        if not ok:
            log.info(f"proxy {p.split('@')[-1]} dropped: {reason}")
    return good


# ── The scrape ───────────────────────────────────────────────────────────────
@dataclass
class SiteResult:
    site: str
    jobs: List[Dict] = field(default_factory=list)
    searches: int = 0
    errors: int = 0
    stopped_by_budget: bool = False


def scrape_site(site: str, cfg, seen: Set[str], plan: List[tuple], meter: ByteMeter,
                proxies: List[str]) -> SiteResult:
    from jobspy import scrape_jobs

    result = SiteResult(site=site)
    today = date.today().isoformat()

    for keyword, location in plan:
        if len(result.jobs) >= cfg.jobs_per_site or result.searches >= cfg.max_searches_per_site:
            break
        if meter.exhausted:
            result.stopped_by_budget = True
            break

        result.searches += 1
        if result.searches > 1:
            time.sleep(random.uniform(3, 6))  # be polite; also eases LinkedIn 429s

        try:
            df = scrape_jobs(
                site_name=[site],
                search_term=keyword,
                location=location,
                distance=cfg.distance_miles,
                is_remote=cfg.remote_only,
                job_type=cfg.job_type or None,
                easy_apply=cfg.easy_apply or None,
                results_wanted=cfg.max_per_search,
                hours_old=cfg.hours_old,
                country_indeed=cfg.country_indeed,
                enforce_annual_salary=cfg.annual_salary,
                linkedin_fetch_description=False,
                proxies=proxies or None,
                verbose=0,
            )
        except BudgetExceeded:
            result.stopped_by_budget = True
            break
        except Exception as e:  # one bad search must not sink the run
            result.errors += 1
            log.warning(f"{site}: '{keyword}' in {location} failed: {str(e)[:200]}")
            continue

        added = 0
        for row in df.to_dict("records"):
            title, company = _clean(row.get("title")), _clean(row.get("company"))
            link = _clean(row.get("job_url"))
            keys = dedupe_keys(title, company, link)
            if not link or not title or any(k in seen for k in keys):
                continue
            seen.update(keys)
            result.jobs.append({
                "Title": title,
                "Company": company,
                "Location": _clean(row.get("location")),
                "Salary": format_salary(row),
                "Site": "LinkedIn" if site == "linkedin" else "Indeed",
                "Link": link,
                "Found": today,
                "_keys": keys,
            })
            added += 1
            if len(result.jobs) >= cfg.jobs_per_site:
                break
        log.info(f"{site}: '{keyword}' in {location} -> {len(df)} returned, {added} new")

        if meter.exhausted:
            result.stopped_by_budget = True
            break

    return result


def scrape_all(cfg, seen: Set[str], today: Optional[date] = None) -> tuple[List[SiteResult], ByteMeter]:
    today = today or date.today()
    meter = ByteMeter(cfg.max_mb_per_run)
    meter.install()
    if "indeed" in cfg.sites:
        if slim_indeed_query(cfg.max_per_search):
            log.info(f"Indeed query trimmed to {cfg.max_per_search} jobs/page, no employer dossier")

    plan = search_plan(cfg.keywords, cfg.locations, today)
    proxies = healthy_proxies(cfg.proxies)
    if cfg.proxies and not proxies:
        log.warning(f"All {len(cfg.proxies)} proxies failed the health check; scraping directly")
    elif cfg.proxies:
        log.info(f"{len(proxies)}/{len(cfg.proxies)} proxies healthy")
    log.info(
        f"{len(cfg.keywords)} keywords x {len(cfg.locations)} locations; "
        f"posted within: {cfg.hours_old or 'any'} h; "
        f"proxies: {len(proxies) or 'none (direct)'}; budget: {cfg.max_mb_per_run} MB"
    )
    if "indeed" in cfg.sites and cfg.hours_old and (cfg.job_type or cfg.remote_only or cfg.easy_apply):
        log.warning("Indeed ignores job_type/remote_only/easy_apply when posted_within is set "
                    "(jobspy limitation); LinkedIn applies all of them")
    results = []
    try:
        for site in cfg.sites:
            results.append(scrape_site(site, cfg, seen, plan, meter, proxies))
    finally:
        meter.uninstall()
    return results, meter
