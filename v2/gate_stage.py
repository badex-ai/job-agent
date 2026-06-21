#!/usr/bin/env python3
"""Production gate stage (v2 Phase 5) — one job in, one verdict record out.

This is the SAME gate the eval harness validated, shaped for the loop. It
layers exactly as v2/eval_gate.py does, so production judgment cannot drift
from the measured numbers (the guard-version confound of 2026-06-16 is the
reason this reuse is non-negotiable — see the design notes):

  1. hard_gates.hard_skip  — deterministic, 0-token safety net. A free skip
     on an unambiguous marker (India / ₹ / unpaid / no-sponsorship) avoids an
     M4 inference call entirely.
  2. qwen_gate.gate_job     — the measured 14b gate, on the eval-validated
     build_system_prompt(), with the SAME verify_evidence() downgrade guard
     the eval applies. Imported from eval_gate so there is one implementation,
     not a copy that can rot.

Offline mode (`transport=offline_transport`) returns an HONEST verdict: a
fixed `surface`. Offline we must never silently skip a job — surfacing it
sends it to human review, which is the safe failure. (Contrast eval_gate's
stub, which fakes a marker-based judgment to test the harness MATH; that
behaviour is wrong for production.)

The result record carries everything the DB row needs and nothing it does
not: verdict, the model's reasoning, the model tag, tags, confidence, the
source of the verdict (hard_gate / qwen / offline / error), and run time.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from v2.gate_prompt import build_system_prompt
from v2.hard_gates import hard_skip
from v2.qwen_gate import GateError, gate_job
from v2.eval_gate import verify_evidence

# The chosen substrate (the design notes). The eval default is still 7b for
# back-compat; production pins 14b explicitly so a stale default can never
# silently downgrade the live gate.
GATE_MODEL = "qwen2.5:14b"

SURFACE_VERDICTS = {"surface", "surface_priority"}


def offline_transport(url, payload, timeout):
    """Honest offline verdict: surface everything (-> human review). Never a
    silent skip. Used when the M4/Ollama tunnel is unavailable but we still
    want the loop's wiring, dedup, and persistence to run."""
    import json
    return {"response": json.dumps({
        "reasoning": "offline: Ollama unreachable, surfaced for human review",
        "verdict": "surface", "confidence": 0, "tags": ["offline"],
    })}


def is_surface(verdict: str) -> bool:
    return verdict in SURFACE_VERDICTS


def _record(verdict, reasoning, model, tags, confidence, source):
    return {
        "gate_verdict": verdict,
        "gate_reason": reasoning,
        "gate_model": model,
        "gate_tags": list(tags or []),
        "gate_confidence": confidence,
        "gate_source": source,            # hard_gate | qwen | offline | error
        "gate_run_at": datetime.utcnow(),
        "surfaced": is_surface(verdict),
    }


def gate_one(job: dict, *, system_prompt: str | None = None,
             model: str = GATE_MODEL, transport=None,
             timeout: float = 120.0) -> dict:
    """Gate a single scraped job dict; return a persistable verdict record.

    `job` uses the scraper / labels.json field names (id, title, company,
    location, description). `transport=offline_transport` runs without a
    network. A malformed model response is recorded as a `surface` with
    source='error' — an unparseable gate output must never bury a job.
    """
    # 1) deterministic hard gate — a free, certain skip.
    fired = hard_skip(job)
    if fired:
        return _record("skip", f"hard gate: {fired}", "hard_gate",
                       [fired], 100, "hard_gate")

    # 2) the measured Qwen gate, on the validated prompt + evidence guard.
    if system_prompt is None:
        system_prompt = build_system_prompt()
    kw = {"model": model}
    if transport is not None:
        kw["transport"] = transport
    is_offline = transport is offline_transport
    try:
        v = gate_job(
            system_prompt=system_prompt,
            title=job.get("title", ""),
            company=job.get("company", ""),
            location=job.get("location", ""),
            description=job.get("description", ""),
            timeout=timeout,
            **kw,
        )
    except GateError as e:
        # malformed output: surface (safe) and record why.
        return _record("surface", f"gate error, surfaced: {e}",
                       model, ["error"], 0, "error")

    raw = v["verdict"]
    # Same downgrade guard the eval applies: a skip whose Evidence quote is
    # fabricated, or an S1-only skip that names a place without stating an
    # exclusion, is downgraded to surface.
    eff, downgrade = verify_evidence(
        raw, v.get("reasoning", ""), job.get("description", ""),
        tags=v.get("tags", []))
    reasoning = v.get("reasoning", "")
    if downgrade:
        reasoning = f"{reasoning}\n[downgraded: {downgrade}]"
    return _record(eff, reasoning, model, v.get("tags", []),
                   v.get("confidence"), "offline" if is_offline else "qwen")
