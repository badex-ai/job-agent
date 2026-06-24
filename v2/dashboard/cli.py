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
from datetime import datetime
from typing import List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich.align import Align
from rich.text import Text

from shared.database.models import Job, get_session
from shared.database.decisions import record_decision
from shared.database.tracking import (
    mark_applied,
    mark_followup_sent,
    mark_response_received,
    get_jobs_needing_followup,
    get_application_stats,
    get_followup_summary,
)

console = Console()

# SPEC §5b: the queue is surfaced + undecided, surface_priority first.
QUEUE_VERDICTS = ("surface_priority", "surface")

# SPEC §5b: one-key actions. The four-way + navigation.
ACTION_KEYS = ("a", "c", "h", "s", "d", "o", "n", "b")
_KEY_TO_LABEL = {"a": "APPLY", "c": "CONSIDER", "h": "HOLD", "s": "SKIP"}

# SPEC §5b skip tagging: the rubric's hard/semantic rule ids (S1..S6).
SKIP_RULES = ("S1", "S2", "S3", "S4", "S5", "S6")

# Apply-flow vocab (mirrors v1/dashboard/cli.py — the tracking contract is shared).
CHANNELS = ("website", "linkedin", "email")
MESSAGE_VERSIONS = ("A", "B", "C")
MESSAGE_TONES = ("formal", "casual", "story")
RESPONSE_TYPES = ("positive", "negative", "later")


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


def get_rescue_queue(session=None) -> List[Job]:
    """Undecided gate-SKIPPED jobs to audit (SPEC §5c).

    The complement of get_queue: rows the gate buried (gate_verdict='skip') that
    are still 'new'. A confirm-skip or rescue advances status out of 'new', so
    these drop out — resumable, same as the queue. Oldest first, deterministic."""
    own = session is None
    session = session or get_session()
    try:
        jobs = (
            session.query(Job)
            .filter(Job.status == "new", Job.gate_verdict == "skip")
            .order_by(Job.scraped_at.asc(), Job.id.asc())
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
    return _record(job_id, label, source_view="queue", rule=rule, note=note,
                   channel=channel, version=version, tone=tone)


def _record(
    job_id: str,
    label: str,
    *,
    source_view: str,
    rule: Optional[str] = None,
    note: Optional[str] = None,
    channel: Optional[str] = None,
    version: Optional[str] = None,
    tone: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Write one decision (+ delegate apply side effects). Shared by the queue
    and rescue seams so an APPLY is handled IDENTICALLY in both — only the
    source_view differs. record_decision writes the immutable Decision and
    advances Job.status; mark_applied owns the channel + follow-up schedule
    (single-writer, SPEC §5d). On apply-tracking failure the label is already
    persisted (append-only), so we surface the failure rather than lose it."""
    ok, err = record_decision(
        # a lightweight detached Job carrying only id is enough; record_decision
        # re-queries the row by id and reads the gate_* snapshot off the DB copy.
        _load_job(job_id),
        label,
        rule=rule,
        note=note,
        source_view=source_view,
    )
    if not ok:
        return False, err

    if label == "APPLY":
        applied_ok, applied_err = mark_applied(
            job_id, via=channel or "website", version=version, tone=tone
        )
        if not applied_ok:
            return False, f"decision recorded but mark_applied failed: {applied_err}"

    return True, None


def apply_rescue_action(
    job_id: str,
    key: str,
    *,
    sub_label: Optional[str] = None,
    rule: Optional[str] = None,
    note: Optional[str] = None,
    channel: Optional[str] = None,
    version: Optional[str] = None,
    tone: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Map one rescue keypress to its decision writes (source_view='rescue').

    The rescue view is the selection-bias defence (SPEC §7/C1): the only channel
    that labels gate-SKIPPED jobs, so the training set isn't surfaced-only.

      * 'r' rescue   -> an act-set decision correcting the gate's false-negative.
                        sub_label says which of APPLY/CONSIDER/HOLD; rule is the
                        over-fired gate tag being corrected. An APPLY rescue runs
                        the same mark_applied path as the queue — a real apply is
                        a real apply regardless of where it was surfaced.
      * 'c' confirm  -> a Decision AGREEING with the gate (human_label=SKIP). A
                        confirmed buried job is itself a valuable label.
    """
    if key == "c":
        return _record(job_id, "SKIP", source_view="rescue", rule=rule, note=note)
    if key == "r":
        if sub_label not in ("APPLY", "CONSIDER", "HOLD"):
            return False, ("rescue needs sub_label in (APPLY, CONSIDER, HOLD); "
                           f"got {sub_label!r}")
        return _record(job_id, sub_label, source_view="rescue", rule=rule, note=note,
                       channel=channel, version=version, tone=tone)
    return False, f"{key!r} is not a rescue decision key (use 'r' or 'c')"


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

    Re-queries the DB on every decision so the queue is always live and resumable
    (decided jobs drop out; SPEC §5b/§9). Within one in-memory pass, 'next' walks
    forward by index without re-querying — so deferring a job doesn't bounce you
    back to the head. Done = queue empty → explicit 'cleared for today' state.
    """
    while True:
        jobs = get_queue()
        if not jobs:
            console.print(Panel("✅ Queue cleared for today — nothing left to triage.",
                                style="bold green"))
            return

        tiers = _tier_counts(jobs)
        idx = 0
        while idx < len(jobs):              # walk this pass; a decision re-queries
            job = jobs[idx]
            console.clear()
            console.print(render_job(job, idx + 1, len(jobs), tiers))
            console.print(
                "[dim]a[/dim] apply  [dim]c[/dim] consider  [dim]h[/dim] hold  "
                "[dim]s[/dim] skip  [dim]d[/dim] desc  [dim]o[/dim] open  "
                "[dim]n[/dim] next  [dim]b[/dim] back"
            )
            key = Prompt.ask("Action", choices=list(ACTION_KEYS), default="n")

            if key == "b":
                return
            if key == "n":                  # defer: advance within this pass
                idx += 1
                continue
            if key == "d":                  # read the full posting, stay on this job
                _show_description(job)
                continue
            if key == "o":
                _open(job)
                continue

            # a decision key (a/c/h/s)
            rule = None
            kwargs = {}
            if key == "s" and Confirm.ask("Tag the rule this skip corrects?", default=False):
                rule = Prompt.ask("Rule", choices=list(SKIP_RULES), default="S2")
            if key == "a":
                if job.applied:             # idempotent apply (SPEC §9)
                    console.print(Panel(
                        f"Already applied via {job.applied_via or '?'} "
                        f"on {job.applied_at}. Re-open the posting with 'o'.",
                        style="yellow"))
                    Prompt.ask("press ENTER", default="")
                    idx += 1
                    continue
                kwargs = _prompt_apply_tracking()

            ok, err = apply_queue_action(job.id, key, rule=rule, **kwargs)
            if ok:
                console.print(f"[green]✓ recorded {_KEY_TO_LABEL[key]}[/green]")
            else:
                console.print(f"[bold red]✗ {err}[/bold red]")
                Prompt.ask("press ENTER", default="")
            break                           # decision made → re-query the live queue


def _show_description(job: Job) -> None:
    """Show the full job description on demand (SPEC §5b: 'description available
    on demand'). The gate's reason is the hero element; the raw posting text sits
    one keypress behind it so the user can read the source before deciding."""
    text = (job.description or "").strip()
    if not text:
        console.print("[yellow]no description stored for this job[/yellow]")
    else:
        with console.pager():
            console.print(Panel(
                text,
                title=f"{job.title or '—'} @ {job.company or '—'}  ·  full description",
                border_style="cyan"))
    Prompt.ask("press ENTER", default="")


def _open(job: Job) -> None:
    """Open the posting in a browser (SPEC §5b 'o'). Dead links are deferred (§9):
    the browser opens whatever's there; the user notices a 404 and moves on."""
    if job.apply_link:
        webbrowser.open(job.apply_link)
        console.print(f"[dim]opened {job.apply_link}[/dim]")
    else:
        console.print("[yellow]no apply_link on this job[/yellow]")
    Prompt.ask("press ENTER", default="")


# --- rescue view (SPEC §5c — the selection-bias defence) -------------------

def render_skipped(job: Job, position: int, total: int) -> Panel:
    """A buried job's audit card: the gate's fired rule + reason shown so the
    user can judge whether the skip was a false-negative (SPEC §5c)."""
    loc = job.location or ("remote" if job.is_remote else "—")
    reason = job.gate_reason or "[dim](no reason given by gate)[/dim]"
    body = (
        f"[bold]{job.title or '—'}[/bold] @ [bold]{job.company or '—'}[/bold]\n"
        f"[dim]{loc} · {job.source or '—'}[/dim]\n\n"
        f"buried by: [red]{_tags(job)}[/red]\n\n"
        f"{reason}"
    )
    return Panel(body, title=f"[{position}/{total}]  rescue audit",
                 border_style="red")


def run_rescue() -> None:
    """Audit the gate's buried jobs. Re-queries on every decision (a confirm/
    rescue advances status out of 'new' so the row drops out — resumable). 'next'
    walks forward within the pass by index. Empty = 'nothing buried to audit'."""
    while True:
        jobs = get_rescue_queue()
        if not jobs:
            console.print(Panel("✅ No buried jobs to audit.", style="bold green"))
            return

        idx = 0
        while idx < len(jobs):
            job = jobs[idx]
            console.clear()
            console.print(render_skipped(job, idx + 1, len(jobs)))
            console.print(
                "[dim]r[/dim] rescue (gate was wrong)  [dim]c[/dim] confirm skip  "
                "[dim]d[/dim] desc  [dim]o[/dim] open  [dim]n[/dim] next  [dim]b[/dim] back"
            )
            key = Prompt.ask("Action", choices=["r", "c", "d", "o", "n", "b"], default="n")

            if key == "b":
                return
            if key == "n":
                idx += 1
                continue
            if key == "d":                  # read the full posting before judging the skip
                _show_description(job)
                continue
            if key == "o":
                _open(job)
                continue

            ok, err = _do_rescue_action(job, key)
            if ok is None:                  # user backed out of the rescue prompt
                idx += 1
                continue
            console.print("[green]✓ recorded[/green]" if ok
                          else f"[bold red]✗ {err}[/bold red]")
            if not ok:
                Prompt.ask("press ENTER", default="")
            break                           # decision made → re-query the live queue


def _do_rescue_action(job: Job, key: str):
    """Collect the inputs a rescue/confirm needs and call apply_rescue_action.
    Returns (ok, err); ok is None if the user aborted the rescue sub-prompt."""
    if key == "c":
        return apply_rescue_action(job.id, "c", note=None)

    # key == 'r': pick the act-set label + the over-fired rule being corrected.
    sub = Prompt.ask("Rescue as", choices=["apply", "consider", "hold"], default="consider")
    sub_label = {"apply": "APPLY", "consider": "CONSIDER", "hold": "HOLD"}[sub]

    rule = None
    fired = _fired_tags(job)
    if fired and Confirm.ask(f"Tag the over-fired rule ({', '.join(fired)})?", default=True):
        rule = Prompt.ask("Rule", choices=fired, default=fired[0])

    kwargs = {}
    if sub_label == "APPLY":
        if job.applied:
            console.print(Panel("Already applied.", style="yellow"))
            Prompt.ask("press ENTER", default="")
            return None, None
        kwargs = _prompt_apply_tracking()
    return apply_rescue_action(job.id, "r", sub_label=sub_label, rule=rule, **kwargs)


def _fired_tags(job: Job) -> list:
    """The gate's cited rules, as a list (for the rescue rule-correction prompt)."""
    try:
        tags = json.loads(job.gate_tags or "[]")
        return [t for t in tags if t] or list(SKIP_RULES)
    except (ValueError, TypeError):
        return list(SKIP_RULES)


# --- follow-ups + stats (PORT from v1; data contract already gate-agnostic) -

def run_followups() -> None:
    """Follow-up manager — ported from v1/dashboard/cli.py. The tracking layer
    is score-free (reads applied/next_followup_due, not gate verdicts), so this
    ports with NO data-contract swap (SPEC §5d)."""
    console.clear()
    jobs = get_jobs_needing_followup()
    summary = get_followup_summary()

    console.print(Align.center("[bold cyan]📬 Follow-up Manager[/bold cyan]"))
    console.print()
    summary_table = Table(show_header=False, box=None, padding=(0, 2))
    summary_table.add_row("[red]Overdue[/red]", f"[bold red]{summary['overdue']}[/bold red]")
    summary_table.add_row("[yellow]Due Today[/yellow]", f"[bold yellow]{summary['due_today']}[/bold yellow]")
    summary_table.add_row("[cyan]Next 3 Days[/cyan]", f"[bold]{summary['upcoming_3_days']}[/bold]")
    summary_table.add_row("[dim]Awaiting Response[/dim]", f"[bold]{summary['total_awaiting']}[/bold]")
    console.print(Align.center(summary_table))
    console.print()

    if not jobs:
        console.print(Panel("No follow-ups due right now.", style="green"))
        Prompt.ask("Press ENTER to continue", default="")
        return

    table = Table(title="Jobs Needing Follow-up", show_header=True, header_style="bold yellow")
    table.add_column("#", style="dim", width=3)
    table.add_column("Company", width=18)
    table.add_column("Title", width=25)
    table.add_column("Applied", width=10)
    table.add_column("Follow-up #", justify="center", width=10)
    table.add_column("Due", width=12)
    for i, job in enumerate(jobs[:20], 1):
        applied_date = job.applied_at.strftime("%m/%d") if job.applied_at else "?"
        followup_num = f"{(job.followup_count or 0) + 1}/3"
        if job.next_followup_due:
            days_diff = (datetime.now() - job.next_followup_due).days
            if days_diff > 0:
                due_text = Text(f"{days_diff}d overdue", style="bold red")
            elif days_diff == 0:
                due_text = Text("Today", style="bold yellow")
            else:
                due_text = Text(f"In {-days_diff}d", style="dim")
        else:
            due_text = Text("—", style="dim")
        table.add_row(str(i), (job.company or "?")[:16], (job.title or "?")[:23],
                      applied_date, followup_num, due_text)
    console.print(table)
    console.print()

    action = Prompt.ask("Action", choices=["send", "response", "back"], default="back")
    if action == "back":
        return
    job_num = Prompt.ask("Job # (from list above)", default="1")
    try:
        idx = int(job_num) - 1
        if 0 <= idx < len(jobs):
            if action == "send":
                _handle_followup_sent(jobs[idx])
            elif action == "response":
                _handle_response_received(jobs[idx])
    except (ValueError, IndexError):
        console.print("[red]Invalid job number[/red]")
        Prompt.ask("Press ENTER to continue", default="")


def _handle_followup_sent(job: Job) -> None:
    console.print(f"\nRecording follow-up for: [bold]{job.title}[/bold] @ {job.company}")
    success, error = mark_followup_sent(job.id)
    if success:
        new_count = (job.followup_count or 0) + 1
        console.print(f"[green]✓ Follow-up #{new_count} recorded![/green]")
        if new_count >= 3:
            console.print("[dim]Final follow-up. No more will be scheduled.[/dim]")
    else:
        console.print(f"[red]✗ Failed: {error}[/red]")
    Prompt.ask("Press ENTER to continue", default="")


def _handle_response_received(job: Job) -> None:
    console.print(f"\nRecording response for: [bold]{job.title}[/bold] @ {job.company}\n")
    response_type = Prompt.ask("Response type", choices=list(RESPONSE_TYPES), default="positive")
    interview_scheduled = False
    if response_type == "positive":
        interview_scheduled = Confirm.ask("Interview scheduled?", default=False)
    success, error = mark_response_received(job.id, response_type=response_type,
                                            interview_scheduled=interview_scheduled)
    if success:
        console.print(f"[green]✓ Response recorded: {response_type}[/green]")
        if interview_scheduled:
            console.print("[bold green]🎉 Congratulations on the interview![/bold green]")
    else:
        console.print(f"[red]✗ Failed: {error}[/red]")
    Prompt.ask("Press ENTER to continue", default="")


def run_stats() -> None:
    """Application + A/B stats — ported from v1. Pure tracking layer, no score
    reads, so a verbatim port (SPEC §5d)."""
    console.clear()
    stats = get_application_stats()
    console.print(Align.center("[bold cyan]📊 Application Statistics[/bold cyan]"))
    console.print()
    overview = Table(title="Overview", show_header=False, box=None)
    overview.add_row("Total Applied", f"[bold]{stats['total_applied']}[/bold]")
    overview.add_row("Awaiting Response", f"[yellow]{stats['awaiting_response']}[/yellow]")
    overview.add_row("Responses Received", f"[green]{stats['responses_received']}[/green]")
    overview.add_row("Interviews Scheduled", f"[bold green]{stats['interviews_scheduled']}[/bold green]")
    console.print(overview)
    console.print()

    channel_table = Table(title="By Channel", show_header=True, header_style="cyan")
    channel_table.add_column("Channel", width=12)
    channel_table.add_column("Count", justify="center", width=8)
    channel_table.add_column("Response Rate", justify="center", width=14)
    for channel in ("website", "linkedin", "email"):
        count = stats["by_channel"].get(channel, 0)
        rate = stats["response_rate_by_channel"].get(channel, 0)
        rate_style = "green" if rate > 20 else "yellow" if rate > 10 else "dim"
        channel_table.add_row(channel.capitalize(), str(count),
                              Text(f"{rate}%", style=rate_style) if count > 0
                              else Text("—", style="dim"))
    console.print(channel_table)
    console.print()

    if any(stats["by_version"].get(v, 0) > 0 for v in ("A", "B", "C")):
        version_table = Table(title="A/B Test: Message Versions", show_header=True, header_style="cyan")
        version_table.add_column("Version", width=10)
        version_table.add_column("Count", justify="center", width=8)
        version_table.add_column("Response Rate", justify="center", width=14)
        for version in ("A", "B", "C"):
            count = stats["by_version"].get(version, 0)
            if count > 0:
                rate = stats["response_rate_by_version"].get(version, 0)
                rate_style = "green" if rate > 20 else "yellow" if rate > 10 else "dim"
                version_table.add_row(f"Version {version}", str(count),
                                      Text(f"{rate}%", style=rate_style))
        console.print(version_table)
        console.print()
    Prompt.ask("Press ENTER to continue", default="")


# --- main menu (SPEC §5a) --------------------------------------------------

def menu_counts(session=None) -> dict:
    """The dashboard's at-a-glance counts (queue / rescue / applied). Factored
    out so it's unit-testable headless. 'buried' uses the same status='new' AND
    gate_verdict='skip' predicate the rescue queue depends on (SPEC §7/C1)."""
    own = session is None
    session = session or get_session()
    try:
        return {
            "queue": (session.query(Job)
                      .filter(Job.status == "new",
                              Job.gate_verdict.in_(QUEUE_VERDICTS)).count()),
            "buried": (session.query(Job)
                       .filter(Job.status == "new",
                               Job.gate_verdict == "skip").count()),
            "applied": session.query(Job).filter(Job.status == "applied").count(),
        }
    finally:
        if own:
            session.close()


# single-letter OR full-word -> the view (None = exit). Defined here, below the
# view functions, so every target exists when run() looks it up.
_MENU_DISPATCH = {
    "q": run_queue, "queue": run_queue,
    "r": run_rescue, "rescue": run_rescue,
    "f": run_followups, "followups": run_followups,
    "s": run_stats, "stats": run_stats,
    "x": None, "exit": None,
}


def run() -> None:
    """The daily dashboard: queue · rescue · followups · stats · exit (SPEC §5a).
    A thin dispatcher — each view owns its own loop."""
    while True:
        console.clear()
        counts = menu_counts()
        new_surfaced, buried, applied = counts["queue"], counts["buried"], counts["applied"]
        followups_due = get_followup_summary()
        due = followups_due["overdue"] + followups_due["due_today"]

        console.print(Align.center("[bold cyan]🤖 Job Agent — v2 Triage[/bold cyan]"))
        console.print()
        if due > 0:
            console.print(Align.center(f"[bold yellow]⚠️  {due} follow-up(s) due![/bold yellow]\n"))
        stats_table = Table(show_header=False, box=None, padding=(0, 2))
        stats_table.add_row("[cyan]Queue (surfaced)[/cyan]", f"[bold green]{new_surfaced}[/bold green]")
        stats_table.add_row("[cyan]Rescue (buried)[/cyan]", f"[bold]{buried}[/bold]")
        stats_table.add_row("[cyan]Applied[/cyan]", f"[bold]{applied}[/bold]")
        console.print(Align.center(stats_table))
        console.print()
        console.print(Align.center(
            "[dim]q[/dim] queue  [dim]r[/dim] rescue  [dim]f[/dim] followups  "
            "[dim]s[/dim] stats  [dim]x[/dim] exit"))
        console.print()

        # accept the single letter OR the full word for each option (typing
        # 'queue' every time is friction; 'q' goes straight there).
        choice = Prompt.ask(
            "Choose",
            choices=["q", "queue", "r", "rescue", "f", "followups",
                     "s", "stats", "x", "exit"],
            default="q", show_choices=False)
        action = _MENU_DISPATCH[choice]
        if action is None:                       # exit
            console.print("[yellow]Goodbye! 👋[/yellow]")
            return
        action()


if __name__ == "__main__":
    run()
