"""
Job Scraper — uses LinkedIn's public guest API (no login required)
and JobSpy for Indeed/Google as fallback.

LinkedIn guest endpoint is the same one Google's crawler uses —
publicly accessible, no authentication, no account risk.
"""
import logging
import random
import time
import json
import urllib.parse
import urllib.request
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from typing import List, Dict
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from shared.config import (
    INDEED_PASSES, LINKEDIN_GUEST_API, LINKEDIN_FILTERS, SEARCH_KEYWORDS,
)

logger = logging.getLogger(__name__)

# pandas/JobSpy hand back NaT/None for a missing date, and str() turns those into
# the literal text "None"/"nan" — a truthy 4-char string that every downstream
# "is it empty?" check waves through, so a job with no date looks like a job with
# a date until you try to parse it. Normalise junk to "" at the boundary, before
# it can reach the DB. (v2/dedup.clean_date does the same for the merge path;
# duplicated here on purpose — shared/ must not import from v2/.)
_JUNK_DATE = {"", "None", "nan", "NaT", "none", "NaN", "null", "NaTType"}


def _clean_date(value) -> str:
    """Return a usable date string, or '' for pandas-null junk ('None'/'nan')."""
    s = "" if value is None else str(value).strip()
    return "" if s in _JUNK_DATE else s

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Rate-limit backoff for the two LinkedIn `requests` paths. A single 429 used to
# abandon a whole page (search) or store a blank description (per-job fetch),
# and the gate reads the description, so a throttled fetch silently poisoned the
# data — this is the 1035/1975-blank incident recorded in commit a5d84f7. Retry
# with exponential backoff + jitter instead of giving up on the first 429.
RETRY_BASE_DELAY = 2.0   # seconds; grows 2s, 4s, 8s across attempts
RETRY_MAX_DELAY = 60.0   # cap a single backoff sleep, before jitter (~1.25x)


class DescriptionFetchError(Exception):
    """A job-detail fetch failed (throttle survived retries, network, parse).

    Distinct from a fetch that SUCCEEDED but found no description div — that
    returns an empty string, an honest "this posting has no body". This is
    raised only when we could not read the page at all, so a caller can tell a
    transient failure (back off, retry later) from a genuinely empty posting
    (leave it be). Without this split a throttled fetch and an empty posting
    were indistinguishable, both silently stored as blank."""


def _linkedin_get(url: str, *, params: Dict = None, timeout: float,
                  max_retries: int = 3) -> requests.Response:
    """GET a LinkedIn guest endpoint, backing off on 429s.

    On HTTP 429 (or a transient connection error) it sleeps with exponential
    backoff and jitter, honouring a `Retry-After` header when present, up to
    `max_retries` extra attempts. A 429 that survives the budget is raised as an
    HTTPError so the caller's existing `except` logs it and returns [] — same
    contract as before, just after we have genuinely tried rather than folding on
    the first throttle. Non-429 HTTP errors are not retried (they will not fix
    themselves); they raise immediately via raise_for_status().
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS,
                                timeout=timeout)
        except requests.exceptions.RequestException as exc:
            # Connection reset / read timeout: treat like a soft throttle and
            # retry, since LinkedIn drops connections under load rather than 429.
            last_exc = exc
            if attempt == max_retries:
                raise
        else:
            if resp.status_code != 429:
                resp.raise_for_status()
                return resp
            last_exc = requests.exceptions.HTTPError(
                "429 Too Many Requests", response=resp)
            if attempt == max_retries:
                raise last_exc

        delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
        retry_after = _retry_after_seconds(last_exc)
        if retry_after is not None:
            delay = min(retry_after, RETRY_MAX_DELAY)
        delay += random.uniform(0, delay * 0.25)  # jitter, de-synchronise calls
        logger.warning(
            "LinkedIn throttled (%s) — backing off %.1fs (attempt %d/%d)",
            getattr(last_exc, "args", [""])[0] if last_exc else "conn",
            delay, attempt + 1, max_retries,
        )
        time.sleep(delay)
    # Unreachable: the final attempt always returns or raises above.
    raise last_exc  # pragma: no cover


def _http_status(exc) -> int:
    """The HTTP status carried by an exception's response, or None if it has none.

    Lets a caller tell a permanent 404/410 (posting gone) from a transient 429
    or a bare connection error (no response at all)."""
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) if resp is not None else None


def _retry_after_seconds(exc) -> float:
    """Parse a Retry-After header (delay-seconds form) off a 429 response, if any."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None  # HTTP-date form is not worth parsing for a guest endpoint


def _parse_job_card(card) -> Dict:
    """Parse a single job card from LinkedIn guest API HTML response."""
    try:
        job_id = card.get("data-entity-urn", "").split(":")[-1]
        title_el = card.find("h3", class_="base-search-card__title")
        company_el = card.find("h4", class_="base-search-card__subtitle")
        location_el = card.find("span", class_="job-search-card__location")
        link_el = card.find("a", class_="base-card__full-link")
        date_el = card.find("time")

        return {
            "id": job_id,
            "title": title_el.get_text(strip=True) if title_el else "",
            "company": company_el.get_text(strip=True) if company_el else "",
            "location": location_el.get_text(strip=True) if location_el else "",
            "job_url": link_el["href"] if link_el else "",
            "date_posted": _clean_date(date_el.get("datetime", "")) if date_el else "",
            "source": "linkedin",
            "description": "",   # filled in by fetch_job_description()
            "apply_link": "",
            "is_remote": True,
        }
    except Exception as e:
        logger.warning(f"Failed to parse job card: {e}")
        return {}


def fetch_job_description(job_url: str) -> tuple[str, str]:
    """
    Fetch full description and apply link from a LinkedIn job detail page.
    Uses the public jobs-guest endpoint.
    Returns (description, apply_link).
    """
    try:
        # Extract numeric job ID from the URL slug (e.g., "...at-company-4397882127")
        slug = job_url.rstrip("/").split("/")[-1].split("?")[0]
        # Job ID is the numeric suffix after the last hyphen
        job_id = slug.split("-")[-1] if slug.split("-")[-1].isdigit() else slug
        detail_url = (
            f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
        )
        resp = _linkedin_get(detail_url, timeout=10)
    except Exception as e:
        # A 404/410 means the posting is GONE, not that the fetch is failing —
        # permanent, and no backoff will bring it back. Treat it like an empty
        # posting (return blank, skip fast) so a batch of expired rows does not
        # sit in the retry backoff. Everything else (throttle survived retries,
        # network, bad URL) is a real fetch failure the caller should back off on
        # and retry later, so it raises rather than storing an indistinguishable
        # blank.
        if _http_status(e) in (404, 410):
            logger.info(f"Job posting gone ({_http_status(e)}) for {job_url}")
            return "", ""
        logger.warning(f"Failed to fetch job description for {job_url}: {e}")
        raise DescriptionFetchError(str(e)) from e

    soup = BeautifulSoup(resp.text, "html.parser")

    desc_el = soup.find("div", class_="description__text")
    description = desc_el.get_text(separator="\n", strip=True) if desc_el else ""

    apply_el = soup.find("a", class_="apply-button")
    apply_link = apply_el["href"] if apply_el else ""

    return description, apply_link


def scrape_linkedin(keyword: str, start: int = 0) -> List[Dict]:
    """
    Scrape LinkedIn jobs using the public guest API.
    No login. No cookies. Same endpoint Google crawler uses.
    """
    params = {
        "keywords": keyword,
        "start": start,
        **LINKEDIN_FILTERS,
    }
    try:
        resp = _linkedin_get(
            LINKEDIN_GUEST_API,
            params=params,
            timeout=15,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.find_all("div", class_="base-card")

        jobs = []
        for card in cards:
            job = _parse_job_card(card)
            if job and job.get("id"):
                # Space per-job description fetches to stay under the throttle;
                # jitter de-synchronises the cadence so it is less fingerprintable.
                time.sleep(1.5 + random.uniform(0, 0.5))
                try:
                    desc, apply = fetch_job_description(job["job_url"])
                except DescriptionFetchError:
                    # One posting we could not read is not worth losing the whole
                    # keyword's results — keep the job with an empty description
                    # (job_url is preserved, so repair_descriptions.py can recover
                    # it later) and move on.
                    desc, apply = "", ""
                job["description"] = desc
                job["apply_link"] = apply
                jobs.append(job)

        logger.info(f"LinkedIn [{keyword}] → {len(jobs)} jobs")
        return jobs

    except Exception as e:
        logger.error(f"LinkedIn scrape failed for [{keyword}]: {e}")
        return []


def _indeed_pass(keyword: str, country: str, wanted: int,
                 remote_only: bool) -> List[Dict]:
    """One Indeed query against one country's Indeed domain.

    Do NOT add hours_old. jobspy builds Indeed's filters as an if/elif chain
    (indeed/__init__.py: "If hours_old is provided, composite filter for
    job_type/is_remote is not possible") where hours_old wins and is_remote is
    silently dropped — you get recent on-site jobs instead of remote ones, with
    no error. Freshness is the caller's problem; ids make a repeat a no-op.
    """
    from jobspy import scrape_jobs
    results = scrape_jobs(
        site_name=["indeed"],
        search_term=keyword,
        country_indeed=country,
        results_wanted=wanted,
        is_remote=remote_only,
        description_format="markdown",
    )
    jobs = []
    for _, row in results.iterrows():
        jobs.append({
            "id": f"indeed_{row.get('id', '')}",
            "title": row.get("title", ""),
            "company": row.get("company", ""),
            "location": row.get("location", "") or country,
            "job_url": row.get("job_url", ""),
            "apply_link": row.get("job_url", ""),
            "date_posted": _clean_date(row.get("date_posted", "")),
            "description": row.get("description", ""),
            "source": "indeed",
            "is_remote": bool(remote_only),
        })
    return jobs


def scrape_indeed(keyword: str) -> List[Dict]:
    """Indeed: one pass per INDEED_PASSES entry in the profile."""
    jobs, seen = [], set()
    for country, wanted, remote_only in INDEED_PASSES:
        try:
            found = _indeed_pass(keyword, country, wanted, remote_only)
        except ImportError:
            logger.warning("jobspy not installed — skipping Indeed. "
                           "Run: pip install python-jobspy")
            return []
        except Exception as e:
            logger.error(f"Indeed [{keyword}] {country} failed: {e}")
            continue
        new = [j for j in found if j["id"] not in seen]
        seen.update(j["id"] for j in new)
        jobs.extend(new)
        logger.info(f"Indeed [{keyword}] {country}"
                    f"{'' if remote_only else ' (any)'} → "
                    f"{len(found)} ({len(new)} new)")
    logger.info(f"Indeed [{keyword}] → {len(jobs)} jobs total")
    return jobs


def _api_get(url: str, timeout: float = 25.0):
    req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def scrape_remotive(keyword: str) -> List[Dict]:
    """Remote-worldwide roles from Remotive's public API.

    This is the slot Wellfound used to occupy — remote startup/tech roles — but
    served by a board that publishes a documented, key-less API for exactly this
    use. (Wellfound has no public API and its robots.txt disallows /*?role=* and
    /*?jobId=*, i.e. every URL a scraper would need; the only working scrapers
    drive a headless browser against its private GraphQL. Not a dependency worth
    having.) Remotive asks for attribution and roughly 2 req/min, so keep the
    keyword list short and let ids dedupe.

    Descriptions arrive inline, so unlike the LinkedIn path there is no per-job
    fetch to be throttled on — the whole 429-blank-row failure mode is absent.
    """
    url = ("https://remotive.com/api/remote-jobs?limit=25&search="
           + urllib.parse.quote(keyword))
    try:
        payload = _api_get(url)
    except Exception as e:
        logger.error(f"Remotive [{keyword}] failed: {e}")
        return []
    jobs = []
    for row in payload.get("jobs", []):
        jobs.append({
            "id": f"remotive_{row.get('id', '')}",
            "title": row.get("title", ""),
            "company": row.get("company_name", ""),
            # Remotive's location is an eligibility restriction ("Europe, UK"),
            # which is exactly what the gate's S1 rule reads. Keep it verbatim.
            "location": row.get("candidate_required_location", "") or "Remote",
            "job_url": row.get("url", ""),
            "apply_link": row.get("url", ""),
            "date_posted": _clean_date(row.get("publication_date", "")),
            "description": row.get("description", ""),
            "source": "remotive",
            "is_remote": True,
        })
    logger.info(f"Remotive [{keyword}] → {len(jobs)} jobs")
    return jobs


def scrape_himalayas(keyword: str) -> List[Dict]:
    """Remote-worldwide roles from Himalayas' public API (free, no key)."""
    url = ("https://himalayas.app/jobs/api?limit=25&search="
           + urllib.parse.quote(keyword))
    try:
        payload = _api_get(url)
    except Exception as e:
        logger.error(f"Himalayas [{keyword}] failed: {e}")
        return []
    jobs = []
    for row in payload.get("jobs", []):
        restrictions = row.get("locationRestrictions") or []
        jobs.append({
            # guid is the canonical job URL; it is the only stable id here.
            "id": f"himalayas_{row.get('guid', '')}",
            "title": row.get("title", ""),
            "company": row.get("companyName", ""),
            "location": ", ".join(restrictions) if restrictions else "Remote",
            "job_url": row.get("guid", ""),
            "apply_link": row.get("applicationLink", "") or row.get("guid", ""),
            "date_posted": _clean_date(row.get("pubDate", "")),
            "description": row.get("description", ""),
            "source": "himalayas",
            "is_remote": True,
        })
    logger.info(f"Himalayas [{keyword}] → {len(jobs)} jobs")
    return jobs


def scrape_linkedin_jobs(keyword: str) -> List[Dict]:
    """
    Scrape LinkedIn Jobs using JobSpy as fallback to guest API.
    """
    try:
        from jobspy import scrape_jobs
        # linkedin_fetch_description pulls each job's body in a per-job request:
        # without it JobSpy's LinkedIn search returns title-only rows, which the
        # gate cannot score. It is slower and LinkedIn-rate-limited (the reason
        # Remotive/Himalayas were preferred), the accepted cost of a usable row.
        # hours_old=2 removed — like Indeed's, that window is almost always empty
        # and silently starved this source; is_remote carries the remote filter.
        results = scrape_jobs(
            site_name=["linkedin"],
            search_term=keyword,
            results_wanted=15,
            is_remote=True,
            description_format="markdown",
            linkedin_fetch_description=True,
        )
        jobs = []
        for _, row in results.iterrows():
            jobs.append({
                "id": f"linkedin_jobspy_{row.get('id', '')}",
                "title": row.get("title", ""),
                "company": row.get("company", ""),
                "location": row.get("location", ""),
                "job_url": row.get("job_url", ""),
                "apply_link": row.get("job_url", ""),
                "date_posted": _clean_date(row.get("date_posted", "")),
                "description": row.get("description", ""),
                "source": "linkedin",
                "is_remote": True,
            })
        logger.info(f"LinkedIn JobSpy [{keyword}] → {len(jobs)} jobs")
        return jobs
    except Exception as e:
        logger.debug(f"LinkedIn JobSpy scrape skipped for [{keyword}]: {e}")
        return []


def run_scrape() -> List[Dict]:
    """
    Full scrape run — rotates through all keywords, hits multiple job boards.
    Called by the RAPH loop every hour.
    Sources: LinkedIn (guest API), Indeed, Wellfound (startups), and JobSpy fallbacks
    """
    all_jobs = []
    seen_ids = set()

    for keyword in SEARCH_KEYWORDS:
        # LinkedIn guest API (primary source for LinkedIn jobs)
        jobs = scrape_linkedin(keyword)
        for job in jobs:
            if job["id"] not in seen_ids:
                seen_ids.add(job["id"])
                all_jobs.append(job)
        time.sleep(2)

        # Indeed
        indeed_jobs = scrape_indeed(keyword)
        for job in indeed_jobs:
            if job["id"] not in seen_ids:
                seen_ids.add(job["id"])
                all_jobs.append(job)
        time.sleep(2)

        # Remote-worldwide boards. Both publish a free, key-less API and return
        # the description inline, so neither can produce the throttled blank
        # rows the LinkedIn per-job fetch is prone to.
        for fetch in (scrape_remotive, scrape_himalayas):
            for job in fetch(keyword):
                if job["id"] not in seen_ids:
                    seen_ids.add(job["id"])
                    all_jobs.append(job)
            time.sleep(2)

        # LinkedIn JobSpy (fallback if guest API fails)
        linkedin_fallback = scrape_linkedin_jobs(keyword)
        for job in linkedin_fallback:
            if job["id"] not in seen_ids:
                seen_ids.add(job["id"])
                all_jobs.append(job)
        time.sleep(2)

    logger.info(f"Total unique jobs scraped this run: {len(all_jobs)}")
    return all_jobs
