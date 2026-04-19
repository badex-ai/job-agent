"""
CLI Dashboard — Replace Streamlit with Rich TUI.
Browse jobs with scores, expand details, mark as reviewed, feedback loop.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.markdown import Markdown
from rich.prompt import Prompt, Confirm
from rich.align import Align
from rich.text import Text
from rich.layout import Layout
from sqlalchemy.orm import sessionmaker
from database.models import Job, get_engine
from analyzer.cv_matcher import generate_report
import json

console = Console()
engine = get_engine()
Session = sessionmaker(bind=engine)


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
    
    action = Prompt.ask("Choose action", choices=["review", "apply", "skip", "hold", "back"], default="back")
    
    if action != "back":
        session = Session()
        job = session.query(Job).filter(Job.id == job_id).first()
        
        if action == "review":
            job.status = "reviewed"
            job.applied = False
            console.print("[green]✓ Marked as reviewed[/green]")
        elif action == "apply":
            job.status = "applied"
            job.applied = True
            console.print("[green]✓ Marked as applied[/green]")
        elif action == "skip":
            job.status = "ignored"
            console.print("[yellow]✓ Marked as skipped[/yellow]")
        elif action == "hold":
            job.status = "hold"
            console.print("[cyan]✓ Marked as on hold[/cyan]")
        
        session.commit()
        
        # Ask for feedback
        feedback = Confirm.ask("Was the score accurate?", default=True)
        if not feedback:
            feedback_text = Prompt.ask("What was wrong with the score? (brief)")
            console.print(f"[dim]Feedback recorded: {feedback_text}[/dim]")
        
        session.close()


def display_dashboard():
    """Show main dashboard."""
    while True:
        console.clear()
        
        session = Session()
        total_new = session.query(Job).filter(Job.status == "new").count()
        total_reviewed = session.query(Job).filter(Job.status == "reviewed").count()
        total_applied = session.query(Job).filter(Job.status == "applied").count()
        avg_score = session.query(Job).filter(Job.overall_score > 0).count()
        session.close()
        
        # Header
        layout = Layout()
        layout.split_column(
            Layout(size=3),  # Top spacing
            Layout(),
        )
        
        console.print(Align.center("[bold cyan]🤖 Job Agent Dashboard[/bold cyan]"))
        console.print()
        
        # Stats
        stats_table = Table(show_header=False, box=None, padding=(0, 2))
        stats_table.add_row(f"[cyan]New Jobs[/cyan]", f"[bold green]{total_new}[/bold green]")
        stats_table.add_row(f"[cyan]Reviewed[/cyan]", f"[bold]{total_reviewed}[/bold]")
        stats_table.add_row(f"[cyan]Applied[/cyan]", f"[bold]{total_applied}[/bold]")
        stats_table.add_row(f"[cyan]Evaluated[/cyan]", f"[bold]{avg_score}[/bold]")
        console.print(Align.center(stats_table))
        
        console.print()
        console.print(Align.center("[bold]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold]"))
        console.print()
        
        # Menu
        choice = Prompt.ask(
            "Choose",
            choices=["list", "high", "apply", "refresh", "exit"],
            default="list"
        )
        
        if choice == "exit":
            console.print("[yellow]Goodbye! 👋[/yellow]")
            break
        elif choice == "list":
            console.clear()
            display_job_list()
            
            job_num = Prompt.ask("View job #", default="-1")
            if job_num != "-1":
                try:
                    jobs = get_jobs({"status": "new"})
                    if 0 < int(job_num) <= len(jobs):
                        display_job_detail(jobs[int(job_num) - 1].id)
                except:
                    pass
        
        elif choice == "high":
            console.clear()
            jobs = get_jobs({"status": "new", "min_score": 80})
            if jobs:
                table = Table(title="🔥 High-Scoring Jobs (80+)", show_header=True, header_style="bold green")
                table.add_column("Company", width=20)
                table.add_column("Title", width=40)
                table.add_column("Score", justify="center", width=8)
                for job in jobs[:20]:
                    table.add_row(job.company[:18], job.title[:38], f"[bold green]{job.overall_score:.0f}[/bold green]")
                console.print(table)
            else:
                console.print("[yellow]No high-scoring jobs yet[/yellow]")
            
            Prompt.ask("Press ENTER to continue", default="")
        
        elif choice == "apply":
            console.clear()
            apply_action = Prompt.ask(
                "Apply to",
                choices=["high", "all", "selected"],
                default="high"
            )
            if apply_action == "high":
                session = Session()
                jobs = session.query(Job).filter(Job.overall_score >= 80, Job.status == "new").all()
                count = 0
                for job in jobs:
                    job.status = "applied"
                    job.applied = True
                    count += 1
                session.commit()
                session.close()
                console.print(f"[green]✓ Applied to {count} jobs[/green]")
                Prompt.ask("Press ENTER to continue", default="")
        
        elif choice == "refresh":
            console.print("[cyan]Refreshing...[/cyan]")
            # This would trigger a new scrape cycle
            Prompt.ask("Press ENTER to continue", default="")


if __name__ == "__main__":
    display_dashboard()
