"""
Job Agent Dashboard — Streamlit UI
Minimal interface, maximum analysis.
Shows ranked jobs, match scores, AI-generated messages, outreach status.
"""
import streamlit as st
import json
import subprocess
from datetime import datetime
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from shared.database.models import get_session, Job

st.set_page_config(
    page_title="Job Agent",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Minimal dark styling ─────────────────────────────────────────
st.markdown("""
<style>
    .main { background-color: #0d0d0d; }
    .stApp { background-color: #0d0d0d; color: #e8e8e8; }
    .metric-card {
        background: #1a1a1a;
        border: 1px solid #2a2a2a;
        border-radius: 8px;
        padding: 16px;
        margin: 8px 0;
    }
    .score-high { color: #00d97e; font-weight: 700; font-size: 1.4rem; }
    .score-mid  { color: #f6c90e; font-weight: 700; font-size: 1.4rem; }
    .score-low  { color: #e64545; font-weight: 700; font-size: 1.4rem; }
    .tag {
        display: inline-block;
        background: #1e2a1e;
        color: #00d97e;
        border: 1px solid #00d97e44;
        border-radius: 4px;
        padding: 2px 8px;
        font-size: 0.75rem;
        margin: 2px;
    }
    .tag-missing {
        background: #2a1e1e;
        color: #e64545;
        border: 1px solid #e6454544;
    }
    .message-box {
        background: #111;
        border-left: 3px solid #00d97e;
        padding: 12px 16px;
        font-family: monospace;
        font-size: 0.85rem;
        white-space: pre-wrap;
        border-radius: 0 8px 8px 0;
    }
    h1, h2, h3 { color: #e8e8e8 !important; }
    .stButton > button {
        background: #1a1a1a;
        color: #00d97e;
        border: 1px solid #00d97e;
        border-radius: 6px;
    }
    .stButton > button:hover {
        background: #00d97e;
        color: #0d0d0d;
    }
</style>
""", unsafe_allow_html=True)


# ── Helpers ──────────────────────────────────────────────────────
def score_color(score: float) -> str:
    if score >= 75: return "score-high"
    if score >= 55: return "score-mid"
    return "score-low"


def load_jobs(status_filter=None, min_score=0, source_filter=None):
    session = get_session()
    q = session.query(Job)
    if status_filter and status_filter != "All":
        q = q.filter(Job.status == status_filter.lower())
    if source_filter and source_filter != "All":
        q = q.filter(Job.source == source_filter.lower())
    q = q.filter(Job.match_score >= min_score)
    jobs = q.order_by(Job.match_score.desc()).all()
    session.close()
    return jobs


def mark_status(job_id: str, new_status: str):
    session = get_session()
    job = session.query(Job).filter_by(id=job_id).first()
    if job:
        job.status = new_status
        if new_status == "applied":
            job.applied = True
        if new_status == "linkedin_sent":
            job.linkedin_sent = True
        session.commit()
    session.close()


# ── Sidebar ──────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚡ Job Agent")
    st.markdown("---")

    min_score = st.slider("Min Match Score", 0, 100, 60)
    status_filter = st.selectbox(
        "Status", ["All", "New", "Reviewed", "Applied", "Ignored"]
    )
    source_filter = st.selectbox("Source", ["All", "LinkedIn", "Indeed"])

    st.markdown("---")
    if st.button("▶ Run RAPH Cycle Now"):
        with st.spinner("Running scrape + analysis..."):
            subprocess.Popen(
                ["python", "agents/loop.py", "--once"],
                cwd=os.path.dirname(os.path.dirname(__file__))
            )
        st.success("Cycle started — refresh in ~2 min")

    st.markdown("---")
    st.markdown("**Outreach sent via email:** auto")
    st.markdown("**LinkedIn messages:** review below")


# ── Header metrics ───────────────────────────────────────────────
all_jobs = load_jobs(min_score=0)
new_jobs = [j for j in all_jobs if j.status == "new"]
applied = [j for j in all_jobs if j.applied]
queued = [j for j in all_jobs if j.linkedin_queued and not j.linkedin_sent]

st.markdown("# ⚡ Job Agent Dashboard")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total Jobs", len(all_jobs))
c2.metric("New (unreviewed)", len(new_jobs))
c3.metric("LinkedIn Queue", len(queued))
c4.metric("Applied", len(applied))

st.markdown("---")

# ── Job list ─────────────────────────────────────────────────────
jobs = load_jobs(status_filter=status_filter, min_score=min_score, source_filter=source_filter)

if not jobs:
    st.info("No jobs match current filters. Run a RAPH cycle or adjust filters.")
else:
    st.markdown(f"### {len(jobs)} jobs — sorted by match score")

    for job in jobs:
        strengths = json.loads(job.strengths or "[]")
        missing   = json.loads(job.missing_skills or "[]")

        with st.expander(
            f"[{job.match_score:.0f}%]  {job.title}  ·  {job.company}  ·  {job.source.upper()}",
            expanded=False,
        ):
            col1, col2 = st.columns([1, 2])

            with col1:
                st.markdown(
                    f'<div class="{score_color(job.match_score)}">'
                    f'{job.match_score:.0f}% match</div>',
                    unsafe_allow_html=True
                )
                st.markdown(f"**{job.title}**")
                st.markdown(f"{job.company} · {job.location}")
                st.markdown(f"Source: `{job.source}` · Posted: {job.date_posted}")
                if job.job_url:
                    st.markdown(f"[View Job ↗]({job.job_url})")
                if job.apply_link:
                    st.markdown(f"[Apply Link ↗]({job.apply_link})")

                st.markdown("**Strengths:**")
                for s in strengths:
                    st.markdown(f'<span class="tag">{s}</span>', unsafe_allow_html=True)

                if missing:
                    st.markdown("**Gaps:**")
                    for m in missing:
                        st.markdown(f'<span class="tag tag-missing">{m}</span>', unsafe_allow_html=True)

            with col2:
                tab1, tab2, tab3 = st.tabs(["LinkedIn Message", "Email Draft", "Job Description"])

                with tab1:
                    msg = job.drafted_message or "No message drafted."
                    st.markdown(
                        f'<div class="message-box">{msg}</div>',
                        unsafe_allow_html=True
                    )
                    if st.button("📋 Copy", key=f"copy_li_{job.id}"):
                        st.code(msg)
                    b1, b2 = st.columns(2)
                    if b1.button("✅ Sent on LinkedIn", key=f"li_sent_{job.id}"):
                        mark_status(job.id, "linkedin_sent")
                        st.rerun()

                with tab2:
                    email = job.drafted_email or "No email drafted."
                    st.markdown(
                        f'<div class="message-box">{email}</div>',
                        unsafe_allow_html=True
                    )

                with tab3:
                    desc = job.description or "No description available."
                    st.text_area("Full Description", desc, height=300, key=f"desc_{job.id}", disabled=True)

            # Status controls
            st.markdown("---")
            s1, s2, s3, s4 = st.columns(4)
            if s1.button("✅ Mark Applied", key=f"applied_{job.id}"):
                mark_status(job.id, "applied")
                st.rerun()
            if s2.button("👀 Mark Reviewed", key=f"reviewed_{job.id}"):
                mark_status(job.id, "reviewed")
                st.rerun()
            if s3.button("🗑 Ignore", key=f"ignore_{job.id}"):
                mark_status(job.id, "ignored")
                st.rerun()
            s4.markdown(f"Current: `{job.status}`")
