"""Tests for content-based dedup (v2/dedup.py)."""
from v2 import dedup


# --- normalization ----------------------------------------------------------

def test_norm_text_lowercases_and_collapses_punct():
    assert dedup.norm_text("Full-Stack  Developer!") == "full stack developer"


def test_norm_text_catches_hyphen_vs_space_variants():
    # "Full-Stack Developer" and "Full Stack Developer" must key identically.
    assert dedup.norm_text("Full-Stack Developer") == dedup.norm_text("Full Stack Developer")


def test_norm_company_drops_legal_suffix_and_case():
    # "Joveo Ai" vs "Joveo AI" vs "Joveo AI, Inc" collapse to one company.
    assert dedup.norm_company("Joveo Ai") == dedup.norm_company("Joveo AI")
    assert dedup.norm_company("Joveo AI, Inc") == dedup.norm_company("Joveo AI")


def test_content_key_is_title_and_company():
    j = {"title": "Full Stack Developer", "company": "DataAnnotation"}
    assert dedup.content_key(j) == ("full stack developer", "dataannotation")


def test_content_key_tolerates_missing_fields():
    assert dedup.content_key({}) == ("", "")


# --- jaccard / format robustness --------------------------------------------

def test_jaccard_identical_is_one():
    assert dedup.desc_jaccard("build kubernetes platforms", "build kubernetes platforms") == 1.0


def test_jaccard_both_empty_is_one():
    assert dedup.desc_jaccard("", "") == 1.0


def test_jaccard_one_empty_is_zero():
    assert dedup.desc_jaccard("anything here", "") == 0.0


def test_jaccard_strips_html_so_cross_format_matches():
    # Same text, one HTML (Remotive/Himalayas), one plain (LinkedIn extracted).
    html = "<p>We build <b>Kubernetes</b> platforms on AWS.</p>"
    plain = "We build Kubernetes platforms on AWS."
    assert dedup.desc_jaccard(html, plain) == 1.0


def test_jaccard_distinct_roles_score_low():
    a = "Senior DevOps engineer owning terraform and EKS clusters for payments."
    b = "Junior frontend developer building React dashboards for a marketing site."
    assert dedup.desc_jaccard(a, b) < dedup.DUP_JACCARD


# --- is_duplicate: the decision that protects distinct roles -----------------

def test_true_dup_same_description_merges():
    d = "Own our AWS EKS platform, Terraform IaC, and CI/CD pipelines."
    assert dedup.is_duplicate(d, d) is True


def test_distinct_roles_sharing_a_key_are_not_dups():
    # The aggregator failure mode: same title+company, genuinely different roles.
    existing = "DevOps engineer role: Kubernetes, Terraform, AWS, on-call rotation."
    incoming = "Data scientist role: Python, pandas, ML models, experiment design."
    assert dedup.is_duplicate(existing, incoming) is False


def test_blank_incoming_merges_into_existing():
    assert dedup.is_duplicate("real description with detail", "") is True


def test_blank_existing_merges_from_incoming():
    assert dedup.is_duplicate("", "real description with detail") is True


def test_near_identical_above_threshold_merges():
    a = "Build and operate Kubernetes clusters with Terraform and AWS EKS daily."
    b = "Build and operate Kubernetes clusters with Terraform and AWS EKS."
    assert dedup.desc_jaccard(a, b) >= dedup.DUP_JACCARD
    assert dedup.is_duplicate(a, b) is True


# --- clean_date: junk pandas-null strings must be treated as absent -----------

def test_clean_date_rejects_pandas_junk():
    for junk in ("None", "nan", "NaT", "", None, "NaN"):
        assert dedup.clean_date(junk) == ""


def test_clean_date_keeps_real_dates():
    assert dedup.clean_date("2026-05-07") == "2026-05-07"
    assert dedup.clean_date("  2026-05-07  ") == "2026-05-07"
