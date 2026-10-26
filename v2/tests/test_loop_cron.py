"""What a SCHEDULED run must guarantee.

Two properties that only matter once nobody is watching the terminal:

  1. The exit code tells the truth. `main()` used to return 0 unconditionally,
     so a cycle that never reached the M4 was indistinguishable from a healthy
     one: exit 0 while nothing ran.

  2. A dead gate never costs a posting. The LinkedIn search is filtered to a
     narrow recency window, so an hour not scraped is gone for good; a job
     stored ungated is merely queued. Scraping must therefore continue while
     the M4 is down.
"""
from unittest.mock import patch

import pytest

from v2 import loop


def _summary(**over):
    base = dict(gated=0, surfaced=0, skipped=0, by_source={}, m4_down=False, stored=0)
    base.update(over)
    return base


# --- exit codes -------------------------------------------------------------

def test_exit_0_when_cycle_completes():
    with patch.object(loop, "cycle", return_value=_summary(gated=5)), \
         patch.object(loop, "migrate_gate_columns", return_value=[]):
        assert loop.main(["--once"]) == 0


def test_exit_1_when_m4_is_down():
    """A scheduled run must FAIL loudly when the gate never ran."""
    with patch.object(loop, "cycle", return_value=_summary(m4_down=True)), \
         patch.object(loop, "migrate_gate_columns", return_value=[]):
        assert loop.main(["--once"]) == 1


def test_exit_1_even_if_some_jobs_were_gated_before_the_drop():
    """A partial cycle is still a failed cycle — rows remain pending."""
    with patch.object(loop, "cycle", return_value=_summary(gated=12, m4_down=True)), \
         patch.object(loop, "migrate_gate_columns", return_value=[]):
        assert loop.main(["--once"]) == 1


def test_no_scrape_mode_also_reports_health():
    with patch.object(loop, "cycle", return_value=_summary(m4_down=True)), \
         patch.object(loop, "migrate_gate_columns", return_value=[]):
        assert loop.main(["--no-scrape"]) == 1


# --- scraping survives a dead gate ------------------------------------------

@pytest.fixture
def gate_down():
    """gate_pending reports the M4 unreachable, gating nothing."""
    with patch.object(loop, "gate_pending",
                      return_value=dict(gated=0, surfaced=0, skipped=0,
                                        by_source={}, m4_down=True)) as g:
        yield g


def test_scrape_runs_even_when_m4_is_down(gate_down):
    raw = [{"id": "1", "title": "Dev", "company": "Acme", "description": "d"}]
    with patch("shared.scraper.job_scraper.run_scrape", return_value=raw) as scrape, \
         patch.object(loop, "_store_new_jobs", return_value=1) as store, \
         patch.object(loop, "get_session"):
        summary = loop.cycle(offline=False)

    assert scrape.called, "a dead gate must not cost us the scrape"
    assert store.called, "scraped jobs must still be stored, ungated"
    assert summary["stored"] == 1
    assert summary["m4_down"] is True


def test_second_gate_pass_is_skipped_when_m4_is_down(gate_down):
    """Storing is worth it; hammering a dead gate a second time is not."""
    with patch("shared.scraper.job_scraper.run_scrape", return_value=[]), \
         patch.object(loop, "_store_new_jobs", return_value=0), \
         patch.object(loop, "get_session"):
        loop.cycle(offline=False)

    assert gate_down.call_count == 1, "should not retry the gate in the same cycle"


def test_healthy_cycle_still_gates_twice():
    """Backlog pass, then the freshly-stored rows."""
    ok = dict(gated=1, surfaced=1, skipped=0, by_source={"linkedin": 1}, m4_down=False)
    with patch.object(loop, "gate_pending", return_value=ok) as g, \
         patch("shared.scraper.job_scraper.run_scrape", return_value=[]), \
         patch.object(loop, "_store_new_jobs", return_value=0), \
         patch.object(loop, "get_session"):
        summary = loop.cycle(offline=False)

    assert g.call_count == 2
    assert summary["gated"] == 2, "both passes fold into the summary"


def test_no_scrape_flag_still_skips_scraping(gate_down):
    with patch("shared.scraper.job_scraper.run_scrape") as scrape, \
         patch.object(loop, "get_session"):
        loop.cycle(offline=False, scrape=False)

    assert not scrape.called
