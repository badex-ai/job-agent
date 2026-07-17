"""Content-based dedup for scraped jobs — reduce what the gate (M4) must score.

The scraper's per-source `id` is a poor dedup key: LinkedIn re-posts the same
role with a fresh posting id, so 34 re-posts become 34 rows and the gate scores
the same job 34 times (measured: 52% of data/jobs.db rows are dups by
title+company). Deduping HERE, in Python, before the gate, is the highest-leverage
way to cut M4 load — every merged row is a job the M4 never processes.

The key is normalized (title, company). A title+company collision is only a
duplicate if the descriptions are also near-identical (token-set Jaccard >=
DUP_JACCARD) — this protects genuinely different roles that share a title+company
(measured: aggregator/staffing accounts post distinct roles under one name; their
description Jaccard sits <=0.5, well below true dups at >=0.8). Because ~50% of
scraped rows arrive with a BLANK description (LinkedIn 429-throttling), a blank on
either side is treated as a duplicate and the blank is filled from the non-blank
side: a description-less row is ungateable garbage, so merging it into a row that
has text strictly improves what the gate sees.

Pure functions only — no DB, no I/O — so the store path and the one-time backfill
share one tested definition of "same job".
"""
import re

# Threshold chosen from the measured bimodal Jaccard distribution of dup-group
# pairs: true dups cluster >=0.8, distinct same-key roles <=0.5. 0.65 sits in the
# wide empty margin between the modes.
DUP_JACCARD = 0.65

_COMPANY_SUFFIX_RE = re.compile(r"\b(inc|llc|ltd|gmbh|co|corp|limited)\b")
_TAG_RE = re.compile(r"<[^>]+>")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _strip_html(text: str) -> str:
    return _TAG_RE.sub(" ", text or "")


def norm_text(s: str) -> str:
    """Lowercase, strip HTML, collapse non-alphanumerics to single spaces."""
    s = _strip_html(s).lower()
    return _NON_ALNUM_RE.sub(" ", s).strip()


def norm_company(s: str) -> str:
    """norm_text plus dropping trailing legal suffixes (inc/llc/ltd/...)."""
    s = norm_text(s)
    s = _COMPANY_SUFFIX_RE.sub(" ", s)
    return _NON_ALNUM_RE.sub(" ", s).strip()


def content_key(job: dict) -> tuple[str, str]:
    """The dedup key: (normalized title, normalized company)."""
    return (norm_text(job.get("title") or ""), norm_company(job.get("company") or ""))


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(_strip_html(text or "").lower()))


def desc_jaccard(a: str, b: str) -> float:
    """Token-set Jaccard of two descriptions, format-robust (HTML stripped).

    Returns 1.0 if both are empty (nothing distinguishes them). Order-insensitive
    and O(n), so it is safe to call at store time.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def is_blank(desc: str) -> bool:
    return not (desc or "").strip()


def is_duplicate(existing_desc: str, incoming_desc: str) -> bool:
    """Given two rows already sharing a content_key, are they the SAME job?

    Either side blank -> duplicate (a blank row is ungateable; merge and fill).
    Both present -> duplicate iff description Jaccard >= DUP_JACCARD.
    """
    if is_blank(existing_desc) or is_blank(incoming_desc):
        return True
    return desc_jaccard(existing_desc, incoming_desc) >= DUP_JACCARD
