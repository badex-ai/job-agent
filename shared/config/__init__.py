import os
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

# Job type CVs — map keywords to CV files
# REFINED: Only keywords that match your target stack + experience
JOB_TYPES = {
    "devops": {
        "keywords": [
            "devops engineer",
            "kubernetes engineer",
            "platform engineer",
            "site reliability engineer",
            "infrastructure engineer",
            "cloud engineer",
            "aws devops",
        ],
        "cv_file": "data/cv_devops.txt",
    },
    "frontend": {
        "keywords": [
            "frontend developer",
            "react developer",
            "typescript developer",
            "next.js developer",
            "web developer",
        ],
        "cv_file": "data/cv_frontend.txt",
    },
    "fullstack": {
        "keywords": [
            "fullstack engineer",
            "full stack developer",
            "backend developer",
            "nodejs developer",
            "typescript backend",
            "python backend",
            "fastapi developer",
        ],
        "cv_file": "data/cv_fullstack.txt",
    },
}

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
