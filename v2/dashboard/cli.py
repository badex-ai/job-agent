"""v2 triage CLI — the daily queue over the gated SQLite (SPEC §5b / §12 step 3).

The queue presents today's UNDECIDED SURFACED jobs (status='new' AND
gate_verdict in surface_priority/surface), surface_priority first, with the
gate's *reason* as the hero element — v2 has no score. The user clears each with
one keypress in the four-way vocabulary (apply/consider/hold/skip); every action
routes through record_decision() so a Decision row (the training signal) is
written alongside the Job.status mutation.

Two seams, deliberately split:
  * apply_queue_action(job_id, key, ...) — the pure dispatch: maps a key to the
    record_decision (+ mark_applied on apply) calls. Unit-tested headless.
  * run_queue() — the interactive Rich loop that prompts and calls the seam.

This module is the queue only (step 3). Rescue (§5c), the follow-ups/stats port
(§5d) and export (§7) are later steps and intentionally absent here.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import json
import webbrowser
from typing import List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm

from shared.database.models import Job, get_session
from shared.database.decisions import record_decision
from shared.database.tracking import mark_applied

console = Console()

# SPEC §5b: the queue is surfaced + undecided, surface_priority first.
QUEUE_VERDICTS = ("surface_priority", "surface")

# SPEC §5b: one-key actions. The four-way + navigation.
ACTION_KEYS = ("a", "c", "h", "s", "o", "n", "b")
_KEY_TO_LABEL = {"a": "APPLY", "c": "CONSIDER", "h": "HOLD", "s": "SKIP"}

# SPEC §5b skip tagging: the rubric's hard/semantic rule ids (S1..S6).
SKIP_RULES = ("S1", "S2", "S3", "S4", "S5", "S6")

# Apply-flow vocab (mirrors v1/dashboard/cli.py — the tracking contract is shared).
CHANNELS = ("website", "linkedin", "email")
MESSAGE_VERSIONS = ("A", "B", "C")
MESSAGE_TONES = ("formal", "casual", "story")


# --- data access -----------------------------------------------------------

def get_queue(session=None) -> List[Job]:
    """Today's undecided surfaced jobs, surface_priority first (SPEC §5b).

    Re-queried on each pass: a decided job has status != 'new' and drops out, so
    the queue is resumable with no separate session state (§5b). Detaches the
    rows so callers can read them after the session closes.
    """
    own = session is None
    session = session or get_session()
    try:
        # surface_priority must sort before surface; within a tier, oldest first
        # for a stable, deterministic order (v2 has no score to rank by).
        priority = Job.gate_verdict == "surface_priority"
        jobs = (
            session.query(Job)
            .filter(Job.status == "new", Job.gate_verdict.in_(QUEUE_VERDICTS))
            .order_by(priority.desc(), Job.scraped_at.asc(), Job.id.asc())
            .all()
        )
        for job in jobs:
            session.expunge(job)
        return jobs
    finally:
        if own:
            session.close()


# --- the dispatch seam (headless, unit-tested) -----------------------------

def apply_queue_action(
    job_id: str,
    key: str,
    *,
    rule: Optional[str] = None,
    note: Optional[str] = None,
    channel: Optional[str] = None,
    version: Optional[str] = None,
    tone: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Map one queue keypress to its decision writes. Returns (ok, error).

    The four-way keys each write a Decision (source_view='queue'). 'a' (apply)
    additionally calls mark_applied — record_decision sets status='applied' and
    snapshots the label; mark_applied owns the apply side effects (channel +
    follow-up schedule), single-writer for that fact (SPEC §5d, the option-1 seam).

    Navigation keys (o/n/b) record nothing and are handled by the caller; passing
    one here is a programming error and returns an error tuple.
    """
    label = _KEY_TO_LABEL.get(key)
    if label is None:
        return False, f"{key!r} is not a decision key (one of {tuple(_KEY_TO_LABEL)})"

    ok, err = record_decision(
        # a lightweight detached Job carrying only id is enough; record_decision
        # re-queries the row by id and reads the gate_* snapshot off the DB copy.
        _load_job(job_id),
        label,
        rule=rule,
        note=note,
        source_view="queue",
    )
    if not ok:
        return False, err

    if label == "APPLY":
        applied_ok, applied_err = mark_applied(
            job_id, via=channel or "website", version=version, tone=tone
        )
        if not applied_ok:
            # the Decision is already written (append-only); surface the tracking
            # failure so the caller can tell the user the follow-up wasn't scheduled.
            return False, f"decision recorded but mark_applied failed: {applied_err}"

    return True, None


def _load_job(job_id: str) -> Optional[Job]:
    """Fetch a detached Job by id (or None). record_decision tolerates a job
    whose row is missing and reports it, so None is a valid pass-through."""
    session = get_session()
    try:
        job = session.query(Job).filter(Job.id == job_id).first()
        if job is not None:
            session.expunge(job)
        return job if job is not None else Job(id=job_id)
    finally:
        session.close()


# --- rendering -------------------------------------------------------------

def _tags(job: Job) -> str:
    try:
        tags = json.loads(job.gate_tags or "[]")
        return ", ".join(tags) if tags else "—"
    except (ValueError, TypeError):
        return job.gate_tags or "—"


def render_job(job: Job, position: int, total: int, tiers: dict) -> Panel:
    """One job's card: title @ company, location, verdict, gate_reason (hero),
    tags. Never crashes on a missing reason (SPEC §9)."""
    verdict_style = "bold green" if job.gate_verdict == "surface_priority" else "cyan"
    loc = job.location or ("remote" if job.is_remote else "—")
    reason = job.gate_reason or "[dim](no reason given by gate)[/dim]"

    body = (
        f"[bold]{job.title or '—'}[/bold] @ [bold]{job.company or '—'}[/bold]\n"
        f"[dim]{loc} · {job.source or '—'}[/dim]\n\n"
        f"verdict: [{verdict_style}]{job.gate_verdict}[/{verdict_style}]"
        f"   tags: {_tags(job)}\n\n"
        f"{reason}"
    )
    header = (
        f"[{position}/{total}]  "
        f"priority {tiers.get('surface_priority', 0)} · surface {tiers.get('surface', 0)}"
    )
    return Panel(body, title=header, border_style=verdict_style)


def _tier_counts(jobs: List[Job]) -> dict:
    counts = {"surface_priority": 0, "surface": 0}
    for j in jobs:
        if j.gate_verdict in counts:
            counts[j.gate_verdict] += 1
    return counts


# --- interactive loop ------------------------------------------------------

def _prompt_apply_tracking() -> dict:
    """Collect apply tracking (channel required; version/tone optional) — same
    contract as v1's _handle_apply_action, feeding mark_applied via the seam."""
    channel = Prompt.ask("How did you apply?", choices=list(CHANNELS), default="website")
    version = None
    if Confirm.ask("Record message version (A/B test)?", default=False):
        version = Prompt.ask("Version", choices=list(MESSAGE_VERSIONS), default="A")
    tone = None
    if Confirm.ask("Record message tone?", default=False):
        tone = Prompt.ask("Tone", choices=list(MESSAGE_TONES), default="formal")
    return {"channel": channel, "version": version, "tone": tone}


def run_queue() -> None:
    """The daily queue loop: clear surfaced jobs one keypress at a time.

    Re-queries each pass so the queue is always live and resumable. Done when the
    queue is empty → an explicit 'cleared for today' state (SPEC §5b/§9).
    """
    while True:
        jobs = get_queue()
        if not jobs:
            console.print(Panel("✅ Queue cleared for today — nothing left to triage.",
                                style="bold green"))
            return

        tiers = _tier_counts(jobs)
        job = jobs[0]                       # always act on the head of the live queue
        position = 1
        total = len(jobs)

        console.clear()
        console.print(render_job(job, position, total, tiers))
        console.print(
            "[dim]a[/dim] apply  [dim]c[/dim] consider  [dim]h[/dim] hold  "
            "[dim]s[/dim] skip  [dim]o[/dim] open  [dim]n[/dim] next  [dim]b[/dim] back"
        )

        key = Prompt.ask("Action", choices=list(ACTION_KEYS), default="n")

        if key == "b":
            return
        if key == "n":
            # defer: no decision. Move it behind the rest of this pass by nudging
            # status nowhere — instead just re-loop; to avoid an infinite loop on a
            # single deferred job, advance to the next undecided in THIS list.
            _defer_loop(jobs, tiers)
            return
        if key == "o":
            if job.apply_link:
                webbrowser.open(job.apply_link)
                console.print(f"[dim]opened {job.apply_link}[/dim]")
            else:
                console.print("[yellow]no apply_link on this job[/yellow]")
            Prompt.ask("press ENTER", default="")
            continue

        # a decision key (a/c/h/s)
        rule = note = None
        kwargs = {}
        if key == "s":
            if Confirm.ask("Tag the rule this skip corrects?", default=False):
                rule = Prompt.ask("Rule", choices=list(SKIP_RULES), default="S2")
        if key == "a":
            if job.applied:                 # idempotent apply (SPEC §9)
                console.print(Panel(
                    f"Already applied via {job.applied_via or '?'} "
                    f"on {job.applied_at}. Re-open the posting with 'o'.",
                    style="yellow"))
                Prompt.ask("press ENTER", default="")
                continue
            kwargs = _prompt_apply_tracking()

        ok, err = apply_queue_action(job.id, key, rule=rule, note=note, **kwargs)
        if ok:
            console.print(f"[green]✓ recorded {_KEY_TO_LABEL[key]}[/green]")
        else:
            console.print(f"[bold red]✗ {err}[/bold red]")
            Prompt.ask("press ENTER", default="")
        # loop re-queries; the decided job is gone, the next head appears.


def _defer_loop(jobs: List[Job], tiers: dict) -> None:
    """Walk the current in-memory pass without re-querying, so 'next' can skip
    forward through jobs without re-deciding earlier ones. A pragmatic local
    affordance; the outer loop owns correctness via re-query on every decision."""
    idx = 1
    while idx < len(jobs):
        job = jobs[idx]
        console.clear()
        console.print(render_job(job, idx + 1, len(jobs), tiers))
        console.print(
            "[dim]a[/dim] apply  [dim]c[/dim] consider  [dim]h[/dim] hold  "
            "[dim]s[/dim] skip  [dim]o[/dim] open  [dim]n[/dim] next  [dim]b[/dim] back"
        )
        key = Prompt.ask("Action", choices=list(ACTION_KEYS), default="n")
        if key == "b":
            return
        if key == "n":
            idx += 1
            continue
        if key == "o":
            if job.apply_link:
                webbrowser.open(job.apply_link)
            Prompt.ask("press ENTER", default="")
            continue
        rule = None
        kwargs = {}
        if key == "s" and Confirm.ask("Tag the rule this skip corrects?", default=False):
            rule = Prompt.ask("Rule", choices=list(SKIP_RULES), default="S2")
        if key == "a":
            if job.applied:
                console.print(Panel("Already applied.", style="yellow"))
                Prompt.ask("press ENTER", default="")
                idx += 1
                continue
            kwargs = _prompt_apply_tracking()
        ok, err = apply_queue_action(job.id, key, rule=rule, **kwargs)
        console.print(f"[green]✓ {_KEY_TO_LABEL[key]}[/green]" if ok
                      else f"[bold red]✗ {err}[/bold red]")
        return  # decision made → bounce to the outer loop's re-query


if __name__ == "__main__":
    run_queue()
