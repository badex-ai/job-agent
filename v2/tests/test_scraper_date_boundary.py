"""The scraper must never store a pandas-null as the TEXT "None".

754 of 986 rows in the live DB carried the literal string "None" in
date_posted — truthy, 4 characters, and waved through by every "is it empty?"
check downstream, so a dateless job was indistinguishable from a dated one
until something tried to parse it. These tests pin the boundary that stops it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.scraper.job_scraper import _clean_date


@pytest.mark.parametrize("junk", ["None", "nan", "NaT", "none", "NaN", "null", ""])
def test_pandas_null_text_becomes_empty(junk):
    assert _clean_date(junk) == ""


def test_real_none_becomes_empty():
    assert _clean_date(None) == ""


@pytest.mark.parametrize("good", ["2026-04-17", "2026-09-07T12:30:00Z", "2026-01-01"])
def test_real_date_survives_untouched(good):
    assert _clean_date(good) == good


def test_whitespace_is_stripped():
    assert _clean_date("  2026-04-17  ") == "2026-04-17"
    assert _clean_date("  None  ") == ""


def test_pandas_nat_is_caught():
    """The actual production cause: str(NaT) == 'NaT', not ''."""
    pd = pytest.importorskip("pandas")
    assert _clean_date(pd.NaT) == ""
    assert _clean_date(pd.Series([pd.NaT]).iloc[0]) == ""


def test_numpy_nan_is_caught():
    np = pytest.importorskip("numpy")
    assert _clean_date(np.nan) == ""


def test_a_date_is_never_silently_truthy_when_absent():
    """The property that matters: absent must be falsy, present must be truthy."""
    assert not _clean_date(None)
    assert not _clean_date("None")
    assert _clean_date("2026-04-17")
