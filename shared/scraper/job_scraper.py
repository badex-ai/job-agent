"""
Job Scraper — uses LinkedIn's public guest API (no login required)
and JobSpy for Indeed/Google as fallback.

LinkedIn guest endpoint is the same one Google's crawler uses —
publicly accessible, no authentication, no account risk.
"""
import logging
import time
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from typing import List, Dict
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from shared.config import LINKEDIN_GUEST_API, LINKEDIN_FILTERS, SEARCH_KEYWORDS

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


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
            "date_posted": date_el.get("datetime", "") if date_el else "",
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
        resp = requests.get(detail_url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        desc_el = soup.find("div", class_="description__text")
        description = desc_el.get_text(separator="\n", strip=True) if desc_el else ""

        apply_el = soup.find("a", class_="apply-button")
        apply_link = apply_el["href"] if apply_el else ""

        return description, apply_link
    except Exception as e:
        logger.warning(f"Failed to fetch job description for {job_url}: {e}")
        return "", ""


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
        resp = requests.get(
            LINKEDIN_GUEST_API,
            params=params,
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 429:
            logger.warning("LinkedIn rate limit hit — sleeping 60s")
            time.sleep(60)
            return []
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.find_all("div", class_="base-card")

        jobs = []
        for card in cards:
            job = _parse_job_card(card)
            if job and job.get("id"):
                # Fetch full description with a small delay to avoid 429
                time.sleep(1.5)
                desc, apply = fetch_job_description(job["job_url"])
                job["description"] = desc
                job["apply_link"] = apply
                jobs.append(job)

        logger.info(f"LinkedIn [{keyword}] → {len(jobs)} jobs")
        return jobs

    except Exception as e:
        logger.error(f"LinkedIn scrape failed for [{keyword}]: {e}")
        return []


def scrape_indeed(keyword: str) -> List[Dict]:
    """
    Scrape Indeed using JobSpy — best scraper, no rate limiting.
    Falls back gracefully if jobspy not installed.
    """
    try:
        from jobspy import scrape_jobs
        results = scrape_jobs(
            site_name=["indeed"],
            search_term=keyword,
            location="Worldwide",
            results_wanted=25,
            hours_old=2,
            is_remote=True,
            job_type="internship",
            description_format="markdown",
        )
        jobs = []
        for _, row in results.iterrows():
            jobs.append({
                "id": f"indeed_{row.get('id', '')}",
                "title": row.get("title", ""),
                "company": row.get("company", ""),
                "location": row.get("location", ""),
                "job_url": row.get("job_url", ""),
                "apply_link": row.get("job_url", ""),
                "date_posted": str(row.get("date_posted", "")),
                "description": row.get("description", ""),
                "source": "indeed",
                "is_remote": True,
            })
        logger.info(f"Indeed [{keyword}] → {len(jobs)} jobs")
        return jobs
    except ImportError:
        logger.warning("jobspy not installed — skipping Indeed. Run: pip install python-jobspy")
        return []
    except Exception as e:
        logger.error(f"Indeed scrape failed for [{keyword}]: {e}")
        return []


def scrape_wellfound(keyword: str) -> List[Dict]:
    """
    Scrape Wellfound (formerly AngelList Talent) using JobSpy.
    Great for startup jobs and early-stage companies.
    """
    try:
        from jobspy import scrape_jobs
        results = scrape_jobs(
            site_name=["wellfound"],
            search_term=keyword,
            location="Worldwide",
            results_wanted=15,
            hours_old=2,
            is_remote=True,
            description_format="markdown",
        )
        jobs = []
        for _, row in results.iterrows():
            jobs.append({
                "id": f"wellfound_{row.get('id', '')}",
                "title": row.get("title", ""),
                "company": row.get("company", ""),
                "location": row.get("location", ""),
                "job_url": row.get("job_url", ""),
                "apply_link": row.get("job_url", ""),
                "date_posted": str(row.get("date_posted", "")),
                "description": row.get("description", ""),
                "source": "wellfound",
                "is_remote": True,
            })
        logger.info(f"Wellfound [{keyword}] → {len(jobs)} jobs")
        return jobs
    except Exception as e:
        logger.debug(f"Wellfound scrape skipped for [{keyword}]: {e}")
        return []


def scrape_linkedin_jobs(keyword: str) -> List[Dict]:
    """
    Scrape LinkedIn Jobs using JobSpy as fallback to guest API.
    """
    try:
        from jobspy import scrape_jobs
        results = scrape_jobs(
            site_name=["linkedin"],
            search_term=keyword,
            location="Worldwide",
            results_wanted=15,
            hours_old=2,
            is_remote=True,
            description_format="markdown",
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
                "date_posted": str(row.get("date_posted", "")),
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

        # Wellfound (startups)
        wellfound_jobs = scrape_wellfound(keyword)
        for job in wellfound_jobs:
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
