"""Tests for v2/qwen_gate.py. All HTTP is mocked — must pass with Ollama down."""

import json
import urllib.error

import pytest

from v2 import qwen_gate
from v2.qwen_gate import (
    DEFAULT_OLLAMA_URL,
    GATE_SCHEMA,
    GateError,
    JSON_ONLY_INSTRUCTION,
    build_payload,
    gate_job,
    ollama_url,
    parse_verdict,
)

GOOD_VERDICT = {
    "reasoning": "Steel-man: could be a fit, but the posting is India-only.",
    "verdict": "skip",
    "confidence": 90,
    "tags": ["india_based"],
}


def fake_transport(verdict=GOOD_VERDICT, calls=None):
    """Transport double: records (url, payload, timeout), returns a canned response."""

    def transport(url, payload, timeout):
        if calls is not None:
            calls.append({"url": url, "payload": payload, "timeout": timeout})
        return {"response": json.dumps(verdict)}

    return transport


def run_gate(transport, **kwargs):
    return gate_job(
        "You are a job-screening gate.",
        "Platform Engineer",
        "Acme",
        "Remote (Worldwide)",
        "Kubernetes, Terraform, on-call.",
        transport=transport,
        **kwargs,
    )


# ── schema ────────────────────────────────────────────────────────────────

def test_schema_property_order_reasoning_first():
    # Ollama fills fields in declaration order; reasoning MUST come first.
    assert list(GATE_SCHEMA["properties"]) == [
        "reasoning", "verdict", "confidence", "tags",
    ]


def test_schema_shapes():
    props = GATE_SCHEMA["properties"]
    assert props["reasoning"]["type"] == "string"
    assert props["verdict"]["enum"] == ["skip", "surface", "surface_priority"]
    assert props["confidence"]["minimum"] == 0
    assert props["confidence"]["maximum"] == 100
    assert props["tags"] == {"type": "array", "items": {"type": "string"}}
    assert set(GATE_SCHEMA["required"]) == {"reasoning", "verdict", "confidence", "tags"}


# ── payload ───────────────────────────────────────────────────────────────

def test_payload_settings_match_claude_md_4a():
    payload = build_payload("sys", "job")
    assert payload["options"] == {"temperature": 0, "seed": 11, "num_ctx": 8192}
    assert payload["keep_alive"] == "5m"
    assert payload["stream"] is False
    assert payload["format"] is GATE_SCHEMA
    assert payload["model"] == "qwen2.5:7b"


def test_payload_system_prompt_has_json_only_instruction():
    payload = build_payload("You are a gate.", "job")
    assert payload["system"].startswith("You are a gate.")
    assert JSON_ONLY_INSTRUCTION in payload["system"]


def test_payload_options_override_merges_with_defaults():
    payload = build_payload("sys", "job", options={"temperature": 0.2})
    assert payload["options"]["temperature"] == 0.2
    assert payload["options"]["seed"] == 11
    assert payload["options"]["num_ctx"] == 8192


def test_payload_is_json_serializable():
    json.dumps(build_payload("sys", "job"))


# ── endpoint resolution ───────────────────────────────────────────────────

def test_default_ollama_url(monkeypatch):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    assert ollama_url() == DEFAULT_OLLAMA_URL


def test_env_overrides_ollama_url(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434/")
    assert ollama_url() == "http://localhost:11434"


# ── gate_job ──────────────────────────────────────────────────────────────

def test_gate_job_happy_path(monkeypatch):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    calls = []
    verdict = run_gate(fake_transport(calls=calls))
    assert verdict == GOOD_VERDICT
    assert len(calls) == 1
    assert calls[0]["url"] == DEFAULT_OLLAMA_URL
    payload = calls[0]["payload"]
    assert "Platform Engineer" in payload["prompt"]
    assert "Kubernetes, Terraform, on-call." in payload["prompt"]
    assert JSON_ONLY_INSTRUCTION in payload["system"]


def test_gate_job_uses_env_url(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9999")
    calls = []
    run_gate(fake_transport(calls=calls))
    assert calls[0]["url"] == "http://127.0.0.1:9999"


def test_gate_job_truncates_description():
    calls = []
    gate_job(
        "sys", "title", "co", "loc", "x" * 10_000,
        transport=fake_transport(calls=calls),
    )
    assert "x" * 6000 in calls[0]["payload"]["prompt"]
    assert "x" * 6001 not in calls[0]["payload"]["prompt"]


def test_gate_job_accepts_all_three_verdicts():
    for v in ("skip", "surface", "surface_priority"):
        verdict = run_gate(fake_transport(verdict={**GOOD_VERDICT, "verdict": v}))
        assert verdict["verdict"] == v


def test_transport_errors_propagate():
    def down(url, payload, timeout):
        raise urllib.error.URLError("connection refused")

    with pytest.raises(urllib.error.URLError):
        run_gate(down)


# ── parse_verdict failure modes ───────────────────────────────────────────

def test_non_json_response_raises_gate_error():
    with pytest.raises(GateError, match="non-JSON"):
        parse_verdict({"response": "   \n\n  "})


def test_json_non_object_raises_gate_error():
    with pytest.raises(GateError, match="not an object"):
        parse_verdict({"response": "[1, 2]"})


def test_missing_field_raises_gate_error():
    bad = {k: v for k, v in GOOD_VERDICT.items() if k != "reasoning"}
    with pytest.raises(GateError, match="missing required fields"):
        parse_verdict({"response": json.dumps(bad)})


def test_unknown_verdict_value_raises_gate_error():
    with pytest.raises(GateError, match="unknown verdict"):
        parse_verdict({"response": json.dumps({**GOOD_VERDICT, "verdict": "apply"})})


def test_module_importable_without_network():
    # Importing must never touch the network; reaching here proves it.
    assert callable(qwen_gate.gate_job)
