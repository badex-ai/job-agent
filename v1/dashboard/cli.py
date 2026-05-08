"""
CLI Dashboard — Rich TUI for job management.

Features:
- Browse jobs with scores
- Mark jobs as applied with tracking (channel, version, tone)
- View follow-up reminders
- Record responses and interview scheduling
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Prompt, Confirm
from rich.align import Align
from rich.text import Text
from rich.layout import Layout
from sqlalchemy.orm import sessionmaker
from shared.database.models import Job, get_engine
from shared.database.tracking import (
    mark_applied,
    mark_followup_sent,
    mark_response_received,
    get_jobs_needing_followup,
    get_application_stats,
    get_followup_summary,
)
from v1.analyzer.cv_matcher import generate_report
import json
from datetime import datetime

console = Console()
engine = get_engine()
Session = sessionmaker(bind=engine)

# Valid choices for application tracking
CHANNELS = ["website", "linkedin", "email"]
MESSAGE_VERSIONS = ["A", "B", "C"]
MESSAGE_TONES = ["formal", "casual", "story"]
RESPONSE_TYPES = ["positive", "negative", "later"]


def get_jobs(filters: dict = None) -> list[Job]:
    """Fetch jobs from database with optional filters."""
    session = Session()
    query = session.query(Job)
    
    if filters:
        if filters.get("status"):
            query = query.filter(Job.status == filters["status"])
        if filters.get("min_score"):
            query = query.filter(Job.overall_score >= filters["min_score"])
        if filters.get("action"):
            query = query.filter(Job.recommended_action == filters["action"])
    
    jobs = query.order_by(Job.overall_score.desc()).all()
    session.close()
    return jobs


def display_job_list():
    """Show jobs in a scrollable table."""
    jobs = get_jobs({"status": "new"})
    
    if not jobs:
        console.print(Panel("📭 No new jobs to review", style="bold yellow"))
        return
    
    table = Table(title="📋 Jobs to Review", show_header=True, header_style="bold cyan")
    table.add_column("ID", style="dim", width=3)
    table.add_column("Title", width=28)
    table.add_column("Company", width=18)
    table.add_column("Score", justify="center", width=6)
    table.add_column("Action", justify="center", width=8)
    table.add_column("Contact", justify="center", width=7)
    table.add_column("Status", width=8)

    for i, job in enumerate(jobs[:50], 1):  # Show first 50
        score_color = "green" if job.overall_score >= 80 else "yellow" if job.overall_score >= 60 else "red"
        score_text = Text(f"{job.overall_score:.0f}", style=f"bold {score_color}")

        action = Text(job.recommended_action or "—", style="dim")
        status_color = "green" if job.status == "applied" else "cyan" if job.status == "reviewed" else "white"
        status_text = Text(job.status or "new", style=status_color)

        # Contact info indicator
        has_email = job.company_email or job.recruiter_email
        has_linkedin = job.recruiter_linkedin
        contact_indicator = ""
        if has_email and has_linkedin:
            contact_indicator = Text("📧🔗", style="green")
        elif has_email:
            contact_indicator = Text("📧", style="cyan")
        elif has_linkedin:
            contact_indicator = Text("🔗", style="blue")
        else:
            contact_indicator = Text("—", style="dim")

        table.add_row(str(i), job.title[:26], job.company[:16], score_text, action, contact_indicator, status_text)
    
    console.print(table)


def display_job_detail(job_id: int):
    """Show detailed view of a single job."""
    session = Session()
    job = session.query(Job).filter(Job.id == job_id).first()
    session.close()
    
    if not job:
        console.print(Panel("❌ Job not found", style="bold red"))
        return
    
    # Header
    console.print()
    console.print(f"[bold cyan]{job.title}[/bold cyan] @ [bold]{job.company}[/bold]")
    console.print(f"[dim]{job.location} • {job.source} • Posted {job.date_posted}[/dim]")
    
    # Score breakdown
    console.print()
    score_table = Table(title="Score Breakdown", show_header=False, box=None)
    score_table.add_row("Overall", f"[bold green]{job.overall_score:.0f}/100[/bold green]")
    score_table.add_row("CV Match", f"{job.cv_match:.0f}/100")
    score_table.add_row("Level Fit", f"{job.level_fit:.0f}/100")
    score_table.add_row("Culture Fit", f"{job.culture_fit:.0f}/100")
    console.print(score_table)
    
    # Contact info (if available)
    has_contact = any([job.company_email, job.recruiter_email, job.recruiter_linkedin, job.recruiter_name])
    if has_contact:
        console.print()
        contact_table = Table(title="📇 Contact Info", show_header=False, box=None)
        if job.recruiter_name:
            contact_table.add_row("Recruiter", f"[bold]{job.recruiter_name}[/bold]")
        if job.company_email:
            contact_table.add_row("Email", f"[cyan]{job.company_email}[/cyan]")
        if job.recruiter_email:
            contact_table.add_row("Recruiter Email", f"[cyan]{job.recruiter_email}[/cyan]")
        if job.recruiter_linkedin:
            contact_table.add_row("LinkedIn", f"[blue]{job.recruiter_linkedin}[/blue]")
        console.print(contact_table)

    # Why good fit / concerns
    if job.why_good_fit:
        console.print(Panel(job.why_good_fit, title="✅ Why This Fits", style="green"))

    if job.why_concerns:
        console.print(Panel(job.why_concerns, title="⚠️  Concerns", style="yellow"))
    
    # Red flags
    try:
        red_flags = json.loads(job.red_flags or "[]")
        if red_flags:
            console.print("[red]🚩 Red Flags:[/red]")
            for flag in red_flags:
                console.print(f"  • {flag}")
    except:
        pass
    
    # Full markdown report
    if job.report_markdown:
        console.print()
        console.print(Markdown(job.report_markdown))
    
    # Job description
    console.print()
    console.print(Panel("Job Description", title="📄 Full Description", style="dim"))
    if job.description:
        console.print(job.description[:2000])
        if len(job.description) > 2000:
            console.print("[dim]...[/dim]")
    
    # Action buttons
    console.print()
    console.print(Align.center("[bold]Actions[/bold]"))

    action = Prompt.ask(
        "Choose action",
        choices=["review", "apply", "skip", "hold", "back"],
        default="back"
    )

    if action == "back":
        return

    if action == "apply":
        # Collect application tracking data
        _handle_apply_action(job_id)
    else:
        # Handle non-apply actions
        session = Session()
        job = session.query(Job).filter(Job.id == job_id).first()

        if action == "review":
            job.status = "reviewed"
            console.print("[green]✓ Marked as reviewed[/green]")
        elif action == "skip":
            job.status = "ignored"
            console.print("[yellow]✓ Marked as skipped[/yellow]")
        elif action == "hold":
            job.status = "hold"
            console.print("[cyan]✓ Marked as on hold[/cyan]")

        session.commit()
        session.close()

    # Ask for score feedback (for all actions except back)
    feedback = Confirm.ask("Was the AI score accurate?", default=True)
    if not feedback:
        feedback_text = Prompt.ask("What was wrong? (brief)")
        console.print(f"[dim]Feedback noted: {feedback_text}[/dim]")


def _handle_apply_action(job_id: str) -> None:
    """
    Handle the apply action with full tracking data collection.

    Prompts for:
    - Channel (website/linkedin/email)
    - Message version (A/B/C) - optional for A/B testing
    - Message tone (formal/casual/story) - optional
    """
    console.print()
    console.print("[bold cyan]📝 Recording Application[/bold cyan]")
    console.print()

    # Required: How did you apply?
    channel = Prompt.ask(
        "How did you apply?",
        choices=CHANNELS,
        default="website"
    )

    # Optional: Message version for A/B testing
    version = None
    if Confirm.ask("Record message version for A/B testing?", default=False):
        version = Prompt.ask(
            "Message version",
            choices=MESSAGE_VERSIONS,
            default="A"
        )

    # Optional: Message tone
    tone = None
    if Confirm.ask("Record message tone?", default=False):
        tone = Prompt.ask(
            "Message tone",
            choices=MESSAGE_TONES,
            default="formal"
        )

    # Record the application
    success, error = mark_applied(job_id, via=channel, version=version, tone=tone)

    if success:
        console.print()
        console.print("[bold green]✓ Application recorded![/bold green]")
        console.print(f"  Channel: [cyan]{channel}[/cyan]")
        if version:
            console.print(f"  Version: [cyan]{version}[/cyan]")
        if tone:
            console.print(f"  Tone: [cyan]{tone}[/cyan]")
        console.print()
        console.print("[dim]First follow-up reminder scheduled for 3 days from now[/dim]")
    else:
        console.print(f"[bold red]✗ Failed to record application: {error}[/bold red]")


def display_followups():
    """Show jobs needing follow-up and allow marking follow-ups as sent."""
    console.clear()

    jobs = get_jobs_needing_followup()
    summary = get_followup_summary()

    # Header with summary
    console.print(Align.center("[bold cyan]📬 Follow-up Manager[/bold cyan]"))
    console.print()

    summary_table = Table(show_header=False, box=None, padding=(0, 2))
    summary_table.add_row(
        "[red]Overdue[/red]",
        f"[bold red]{summary['overdue']}[/bold red]"
    )
    summary_table.add_row(
        "[yellow]Due Today[/yellow]",
        f"[bold yellow]{summary['due_today']}[/bold yellow]"
    )
    summary_table.add_row(
        "[cyan]Next 3 Days[/cyan]",
        f"[bold]{summary['upcoming_3_days']}[/bold]"
    )
    summary_table.add_row(
        "[dim]Awaiting Response[/dim]",
        f"[bold]{summary['total_awaiting']}[/bold]"
    )
    console.print(Align.center(summary_table))
    console.print()

    if not jobs:
        console.print(Panel(
            "No follow-ups due right now. Great job staying on top of things!",
            style="green"
        ))
        Prompt.ask("Press ENTER to continue", default="")
        return

    # Show jobs needing follow-up
    table = Table(
        title="Jobs Needing Follow-up",
        show_header=True,
        header_style="bold yellow"
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Company", width=18)
    table.add_column("Title", width=25)
    table.add_column("Applied", width=10)
    table.add_column("Follow-up #", justify="center", width=10)
    table.add_column("Due", width=12)

    for i, job in enumerate(jobs[:20], 1):
        applied_date = job.applied_at.strftime("%m/%d") if job.applied_at else "?"
        followup_num = f"{(job.followup_count or 0) + 1}/3"

        # Calculate days overdue
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

        table.add_row(
            str(i),
            (job.company or "?")[:16],
            (job.title or "?")[:23],
            applied_date,
            followup_num,
            due_text
        )

    console.print(table)
    console.print()

    # Actions
    action = Prompt.ask(
        "Action",
        choices=["send", "response", "back"],
        default="back"
    )

    if action == "back":
        return

    job_num = Prompt.ask("Job # (from list above)", default="1")
    try:
        idx = int(job_num) - 1
        if 0 <= idx < len(jobs):
            job = jobs[idx]

            if action == "send":
                _handle_followup_sent(job)
            elif action == "response":
                _handle_response_received(job)
    except (ValueError, IndexError):
        console.print("[red]Invalid job number[/red]")
        Prompt.ask("Press ENTER to continue", default="")


def _handle_followup_sent(job: Job) -> None:
    """Record that a follow-up was sent."""
    console.print()
    console.print(f"Recording follow-up for: [bold]{job.title}[/bold] @ {job.company}")

    success, error = mark_followup_sent(job.id)

    if success:
        new_count = (job.followup_count or 0) + 1
        console.print(f"[green]✓ Follow-up #{new_count} recorded![/green]")

        if new_count >= 3:
            console.print("[dim]This was the final follow-up. No more will be scheduled.[/dim]")
        else:
            console.print(f"[dim]Next follow-up will be scheduled automatically.[/dim]")
    else:
        console.print(f"[red]✗ Failed: {error}[/red]")

    Prompt.ask("Press ENTER to continue", default="")


def _handle_response_received(job: Job) -> None:
    """Record that a response was received from the company."""
    console.print()
    console.print(f"Recording response for: [bold]{job.title}[/bold] @ {job.company}")
    console.print()

    response_type = Prompt.ask(
        "Response type",
        choices=RESPONSE_TYPES,
        default="positive"
    )

    interview_scheduled = False
    if response_type == "positive":
        interview_scheduled = Confirm.ask("Interview scheduled?", default=False)

    success, error = mark_response_received(
        job.id,
        response_type=response_type,
        interview_scheduled=interview_scheduled
    )

    if success:
        console.print(f"[green]✓ Response recorded: {response_type}[/green]")
        if interview_scheduled:
            console.print("[bold green]🎉 Congratulations on the interview![/bold green]")
        console.print("[dim]No more follow-ups will be scheduled for this job.[/dim]")
    else:
        console.print(f"[red]✗ Failed: {error}[/red]")

    Prompt.ask("Press ENTER to continue", default="")


def display_stats():
    """Show application statistics for A/B testing analysis."""
    console.clear()

    stats = get_application_stats()

    console.print(Align.center("[bold cyan]📊 Application Statistics[/bold cyan]"))
    console.print()

    # Overview
    overview = Table(title="Overview", show_header=False, box=None)
    overview.add_row("Total Applied", f"[bold]{stats['total_applied']}[/bold]")
    overview.add_row("Awaiting Response", f"[yellow]{stats['awaiting_response']}[/yellow]")
    overview.add_row("Responses Received", f"[green]{stats['responses_received']}[/green]")
    overview.add_row("Interviews Scheduled", f"[bold green]{stats['interviews_scheduled']}[/bold green]")
    console.print(overview)
    console.print()

    # By Channel
    channel_table = Table(title="By Channel", show_header=True, header_style="cyan")
    channel_table.add_column("Channel", width=12)
    channel_table.add_column("Count", justify="center", width=8)
    channel_table.add_column("Response Rate", justify="center", width=14)

    for channel in ["website", "linkedin", "email"]:
        count = stats["by_channel"].get(channel, 0)
        rate = stats["response_rate_by_channel"].get(channel, 0)
        rate_style = "green" if rate > 20 else "yellow" if rate > 10 else "dim"
        channel_table.add_row(
            channel.capitalize(),
            str(count),
            Text(f"{rate}%", style=rate_style) if count > 0 else Text("—", style="dim")
        )
    console.print(channel_table)
    console.print()

    # By Message Version (A/B Testing)
    if any(stats["by_version"].get(v, 0) > 0 for v in ["A", "B", "C"]):
        version_table = Table(title="A/B Test: Message Versions", show_header=True, header_style="cyan")
        version_table.add_column("Version", width=10)
        version_table.add_column("Count", justify="center", width=8)
        version_table.add_column("Response Rate", justify="center", width=14)

        for version in ["A", "B", "C"]:
            count = stats["by_version"].get(version, 0)
            rate = stats["response_rate_by_version"].get(version, 0)
            if count > 0:
                rate_style = "green" if rate > 20 else "yellow" if rate > 10 else "dim"
                version_table.add_row(
                    f"Version {version}",
                    str(count),
                    Text(f"{rate}%", style=rate_style)
                )
        console.print(version_table)
        console.print()

    Prompt.ask("Press ENTER to continue", default="")


def display_dashboard():
    """Show main dashboard with all navigation options."""
    while True:
        console.clear()

        # Gather stats
        session = Session()
        total_new = session.query(Job).filter(Job.status == "new").count()
        total_reviewed = session.query(Job).filter(Job.status == "reviewed").count()
        total_applied = session.query(Job).filter(Job.status == "applied").count()
        total_evaluated = session.query(Job).filter(Job.overall_score > 0).count()
        session.close()

        # Get follow-up summary for alert
        followup_summary = get_followup_summary()
        followups_due = followup_summary["overdue"] + followup_summary["due_today"]

        # Header
        console.print(Align.center("[bold cyan]🤖 Job Agent Dashboard[/bold cyan]"))
        console.print()

        # Alert for follow-ups due
        if followups_due > 0:
            console.print(Align.center(
                f"[bold yellow]⚠️  {followups_due} follow-up(s) due![/bold yellow]"
            ))
            console.print()

        # Stats
        stats_table = Table(show_header=False, box=None, padding=(0, 2))
        stats_table.add_row("[cyan]New Jobs[/cyan]", f"[bold green]{total_new}[/bold green]")
        stats_table.add_row("[cyan]Reviewed[/cyan]", f"[bold]{total_reviewed}[/bold]")
        stats_table.add_row("[cyan]Applied[/cyan]", f"[bold]{total_applied}[/bold]")
        stats_table.add_row("[cyan]Evaluated[/cyan]", f"[bold]{total_evaluated}[/bold]")
        console.print(Align.center(stats_table))

        console.print()
        console.print(Align.center("[bold]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold]"))
        console.print()

        # Menu with follow-ups option
        choice = Prompt.ask(
            "Choose",
            choices=["list", "high", "followups", "stats", "refresh", "exit"],
            default="list"
        )

        if choice == "exit":
            console.print("[yellow]Goodbye! 👋[/yellow]")
            break

        elif choice == "list":
            console.clear()
            display_job_list()

            job_num = Prompt.ask("View job # (or ENTER to go back)", default="-1")
            if job_num != "-1":
                try:
                    jobs = get_jobs({"status": "new"})
                    if 0 < int(job_num) <= len(jobs):
                        display_job_detail(jobs[int(job_num) - 1].id)
                except (ValueError, IndexError):
                    console.print("[red]Invalid job number[/red]")

        elif choice == "high":
            console.clear()
            jobs = get_jobs({"status": "new", "min_score": 80})
            if jobs:
                table = Table(
                    title="🔥 High-Scoring Jobs (80+)",
                    show_header=True,
                    header_style="bold green"
                )
                table.add_column("#", style="dim", width=3)
                table.add_column("Company", width=20)
                table.add_column("Title", width=35)
                table.add_column("Score", justify="center", width=8)

                for i, job in enumerate(jobs[:20], 1):
                    table.add_row(
                        str(i),
                        (job.company or "?")[:18],
                        (job.title or "?")[:33],
                        f"[bold green]{job.overall_score:.0f}[/bold green]"
                    )
                console.print(table)
                console.print()

                # Allow viewing details of high-scoring jobs
                job_num = Prompt.ask("View job # (or ENTER to go back)", default="-1")
                if job_num != "-1":
                    try:
                        idx = int(job_num) - 1
                        if 0 <= idx < len(jobs):
                            display_job_detail(jobs[idx].id)
                    except (ValueError, IndexError):
                        pass
            else:
                console.print("[yellow]No high-scoring jobs yet[/yellow]")
                Prompt.ask("Press ENTER to continue", default="")

        elif choice == "followups":
            display_followups()

        elif choice == "stats":
            display_stats()

        elif choice == "refresh":
            console.print("[cyan]Refreshing job data...[/cyan]")
            # This would trigger a new scrape cycle in a full implementation
            console.print("[dim]Run 'python main.py' to fetch new jobs[/dim]")
            Prompt.ask("Press ENTER to continue", default="")


if __name__ == "__main__":
    display_dashboard()
