"""
CV Matcher — the AI brain.
Sends job description + the candidate's CV to Claude API.
Returns structured JSON with:
- Dimensions: cv_match, level_fit, culture_fit, red_flags
- Overall score (0-100)
- Markdown report with detailed justification
- Sound alert when job is good fit
"""
import json
import logging
import time
import anthropic
import sys
import os
import subprocess
import platform
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, CV_PATH, MIN_MATCH_SCORE, JOB_TYPES
from config.profile_manager import ProfileManager

logger = logging.getLogger(__name__)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
profile = ProfileManager()


def detect_job_type(job_title: str, job_description: str) -> str:
    """Detect job type based on title and description keywords."""
    text = (job_title + " " + job_description).lower()
    
    for job_type, config in JOB_TYPES.items():
        for keyword in config["keywords"]:
            if keyword.lower() in text:
                return job_type
    
    return "fullstack"  # default


def load_cv(job_type: str = "fullstack") -> str:
    """Load CV text from disk for a specific job type."""
    cv_file = JOB_TYPES.get(job_type, {}).get("cv_file", CV_PATH)
    try:
        with open(cv_file, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning(f"CV not found at {cv_file}, falling back to {CV_PATH}")
        try:
            with open(CV_PATH, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            logger.error(f"CV not found at {CV_PATH}. Create data/cv.txt first.")
            return ""


CV_CACHE = {}  # cache loaded CVs per job type


def get_cv(job_type: str = "fullstack") -> str:
    """Get CV for job type with caching."""
    if job_type not in CV_CACHE:
        CV_CACHE[job_type] = load_cv(job_type)
    return CV_CACHE[job_type]


def play_alert_sound():
    """Play system alert sound when job is good fit."""
    try:
        system = platform.system()
        if system == "Darwin":  # macOS
            subprocess.run(["afplay", "/System/Library/Sounds/Glass.aiff"], 
                          check=False, timeout=2)
        elif system == "Linux":  # Linux
            subprocess.run(["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"],
                          check=False, timeout=2)
        elif system == "Windows":  # Windows
            subprocess.run(["powershell", "-c", 
                           "[System.Media.SystemSounds]::Exclamation.Play()"],
                          check=False, timeout=2)
    except Exception as e:
        logger.debug(f"Could not play alert sound: {e}")


ANALYSIS_PROMPT = """
You are an expert technical recruiter AND a senior DevOps engineer.
Analyze this job against the candidate's profile and CV.
Return ONLY raw JSON—no preamble, no markdown, no explanation.

{profile_context}

CANDIDATE CV:
{cv}

JOB DESCRIPTION:
{job_description}

JOB TITLE: {job_title}
COMPANY: {company}

Return EXACTLY this JSON structure:
{{
  "cv_match": <0-100: how well CV skills match job requirements>,
  "level_fit": <0-100: career level alignment (IC, IC+mgr, manager, principal)>,
  "culture_fit": <0-100: alignment with candidate's work style and values>,
  "red_flags": <array of strings: specific concerns (e.g., "no remote", "early-stage startup", "unclear tech stack")>,
  "overall_score": <0-100: weighted average considering all dimensions>,
  "top_strengths": ["strength1", "strength2", "strength3"],
  "gaps": ["gap1", "gap2"],
  "why_good_fit": "<2-3 sentences: specific reasons this is a good match>",
  "why_concerns": "<2-3 sentences: specific concerns or gaps, or 'None identified' if strong fit>",
  "recommended_action": "<'APPLY', 'CONSIDER', 'SKIP', or 'HOLD'>",
  "report_markdown": "# {job_title} @ {company}\n\n**Overall Score: XX/100**\n\n## Dimensions\n- CV Match: XX/100\n- Level Fit: XX/100\n- Culture Fit: XX/100\n\n## Why This Fits\nXXX\n\n## Concerns\nXXX\n\n## Red Flags\n- XXX\n\n## Recommended Action\nXXX"
}}

Scoring guide:
cv_match: 90-100 has almost all skills; 70-89 strong with 1-2 gaps; 50-69 learnable gaps; <50 major missing skills
level_fit: 90-100 perfect level; 70-89 good level; 50-69 slightly junior/senior; <50 wrong level
culture_fit: 90-100 strong values alignment; 70-89 generally aligns; 50-69 some concerns; <50 likely poor fit
overall_score: weighted by importance (cv_match 40%, level_fit 30%, culture_fit 20%, no red_flags 10%)
recommended_action: APPLY (85+), CONSIDER (70-84), HOLD (60-69), SKIP (<60 or red flags are deal-breakers)
"""


def analyse_job(job: dict, retries: int = 3) -> dict:
    """
    Send job + CV + profile to Claude API.
    Returns enriched job dict with analysis fields.
    Plays alert sound if score >= MIN_MATCH_SCORE.
    """
    # Detect job type and get appropriate CV
    job_type = detect_job_type(job.get("title", ""), job.get("description", ""))
    cv = get_cv(job_type)
    
    if not cv:
        return {**job, "job_type": job_type, "overall_score": 0, "cv_match": 0, "level_fit": 0,
                "culture_fit": 0, "red_flags": "[]", "top_strengths": "[]",
                "gaps": "[]", "why_good_fit": "", "why_concerns": "",
                "recommended_action": "SKIP", "report_markdown": ""}

    # Get profile context if available
    profile_context = ""
    if not profile.is_empty():
        eval_context = profile.get_evaluation_context()
        profile_context = f"""
CANDIDATE PROFILE (personality, goals, values):
{eval_context}
"""

    prompt = ANALYSIS_PROMPT.format(
        profile_context=profile_context,
        cv=cv,
        job_description=job.get("description", "")[:3500],  # cap tokens
        job_title=job.get("title", ""),
        company=job.get("company", ""),
    )

    for attempt in range(retries):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()

            # Strip accidental markdown fences
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]

            result = json.loads(raw)

            overall_score = float(result.get("overall_score", 0))
            
            # Play alert sound if good match
            if overall_score >= MIN_MATCH_SCORE:
                logger.info(f"🔔 Job matches! Score: {overall_score}/100")
                play_alert_sound()

            return {
                **job,
                "job_type": job_type,
                "overall_score": overall_score,
                "cv_match": float(result.get("cv_match", 0)),
                "level_fit": float(result.get("level_fit", 0)),
                "culture_fit": float(result.get("culture_fit", 0)),
                "red_flags": json.dumps(result.get("red_flags", [])),
                "top_strengths": json.dumps(result.get("top_strengths", [])),
                "gaps": json.dumps(result.get("gaps", [])),
                "why_good_fit": result.get("why_good_fit", ""),
                "why_concerns": result.get("why_concerns", ""),
                "recommended_action": result.get("recommended_action", "SKIP"),
                "report_markdown": result.get("report_markdown", ""),
            }

        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse error on attempt {attempt+1}: {e}")
            time.sleep(2 ** attempt)
        except anthropic.RateLimitError:
            logger.warning(f"Rate limit — sleeping 30s (attempt {attempt+1})")
            time.sleep(30)
        except Exception as e:
            logger.error(f"Claude API error on attempt {attempt+1}: {e}")
            time.sleep(2 ** attempt)

    # Return job unchanged if all retries fail
    return {**job, "job_type": job_type, "overall_score": 0, "cv_match": 0, "level_fit": 0,
            "culture_fit": 0, "red_flags": "[]", "top_strengths": "[]",
            "gaps": "[]", "why_good_fit": "", "why_concerns": "",
            "recommended_action": "SKIP", "report_markdown": ""}


def analyse_batch(jobs: list[dict]) -> list[dict]:
    """Analyse a batch of jobs. Adds 1s delay between calls."""
    results = []
    for i, job in enumerate(jobs):
        logger.info(f"Analysing job {i+1}/{len(jobs)}: {job.get('title')} @ {job.get('company')}")
        analysed = analyse_job(job)
        results.append(analysed)
        time.sleep(1)  # avoid API rate limits
    return results


def generate_report(job: dict) -> str:
    """Generate a detailed markdown report for a job evaluation."""
    report = job.get("report_markdown", "")
    if report:
        return report
    
    # Fallback if report_markdown wasn't in response
    return f"""# {job.get('title', 'N/A')} @ {job.get('company', 'N/A')}

**Overall Score: {job.get('overall_score', 0):.0f}/100**

## Scoring Breakdown
- CV Match: {job.get('cv_match', 0):.0f}/100
- Level Fit: {job.get('level_fit', 0):.0f}/100  
- Culture Fit: {job.get('culture_fit', 0):.0f}/100

## Why This Works
{job.get('why_good_fit', 'N/A')}

## Concerns & Gaps
{job.get('why_concerns', 'None identified')}

## Red Flags
{format_list(json.loads(job.get('red_flags', '[]')))}

## Top Strengths
{format_list(json.loads(job.get('top_strengths', '[]')))}

## Gaps to Close
{format_list(json.loads(job.get('gaps', '[]')))}

## Recommended Action
**{job.get('recommended_action', 'SKIP')}**

---
*Evaluation powered by Claude + your profile. Update profile with `python setup_profile.py` to personalize scoring.*
"""


def format_list(items: list) -> str:
    """Format list items as markdown bullet points."""
    if not items:
        return "- None"
    return "\n".join(f"- {item}" for item in items)
