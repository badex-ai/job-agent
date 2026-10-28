import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = "claude-sonnet-4-20250514"

# Gmail SMTP
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

# Scheduling
SCRAPE_INTERVAL_MINUTES = int(os.getenv("SCRAPE_INTERVAL_MINUTES", 60))
MIN_MATCH_SCORE = int(os.getenv("MIN_MATCH_SCORE", 60))

# LinkedIn guest API — no login required, public endpoint
LINKEDIN_GUEST_API = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)

# The person's own search setup lives in the gitignored _profile.yml (copy
# _profile.example.yml): job types map search keywords to the CV used for
# them, and Indeed passes say which countries to scan.
_PROFILE_PATH = Path(__file__).resolve().parent / "_profile.yml"
_PROFILE = (
    yaml.safe_load(_PROFILE_PATH.read_text()) or {} if _PROFILE_PATH.exists() else {}
)

JOB_TYPES = _PROFILE.get("job_types") or {}

# (country, results_wanted, remote_only). Indeed is per-country — there is no
# worldwide domain, and country_indeed="worldwide" returns nothing, so "the
# whole market" is spelled out as the countries that actually post remote
# roles. Your own country usually goes in with remote_only false: onsite and
# hybrid there are real options.
INDEED_PASSES = [tuple(p) for p in _PROFILE.get("indeed_passes") or [
    ("usa", 20, True),
    ("uk", 15, True),
    ("canada", 10, True),
]]

# Flatten all keywords for scraping
SEARCH_KEYWORDS = []
for job_type in JOB_TYPES.values():
    SEARCH_KEYWORDS.extend(job_type["keywords"])

# Filters applied to every LinkedIn search
LINKEDIN_FILTERS = {
    "location": "Worldwide",
    "f_WT": "2",       # remote only
    # Recency window, in seconds. MUST be >= the scheduled scrape interval or
    # postings falling between two runs are never seen: the search simply does
    # not return them, and nothing downstream can recover what was never
    # fetched. Sized to the 3-hourly cron (v2/cron_run.sh) with headroom for a
    # run that starts late or takes longer than usual.
    "f_TPR": "r14400",  # posted in last 4 hours
    "sortBy": "R",     # most relevant
}

# Database
DB_PATH = "data/jobs.db"
CV_PATH = "data/cv.txt"
