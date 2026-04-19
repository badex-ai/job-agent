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

# Search terms to rotate through on each run
SEARCH_KEYWORDS = [
    "devops engineer",
    "cloud engineer",
    "kubernetes engineer",
    "cloud developer",
    
    

]

# Filters applied to every LinkedIn search
LINKEDIN_FILTERS = {
    "location": "Worldwide",
    "f_WT": "2",       # remote only
    "f_TPR": "r3600",  # posted in last 1 hour
    "sortBy": "R",     # most relevant
}

# Database
DB_PATH = "data/jobs.db"
CV_PATH = "data/cv.txt"
