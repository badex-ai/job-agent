"""Qwen eligibility-gate client (v2 Phase 4, T1).

Sends one job to Qwen via Ollama /api/generate and returns a structured
verdict. Settings are verified against the Ollama API docs
(https://github.com/ollama/ollama/blob/main/docs/api.md):

- temperature 0, seed 11, num_ctx 8192 — deterministic, no silent truncation
- keep_alive "5m", stream false, format = full JSON Schema
- the schema declares `reasoning` FIRST so the model must deliberate before
  it emits a verdict (Ollama fills schema fields in declaration order)
- an explicit "respond only in JSON" line is appended to the system prompt
  by this client — the documented caveat that `format` alone can emit
  whitespace — so no upstream prompt builder can forget it

The HTTP layer is injectable (`transport=`) so the module is testable with
Ollama unreachable.
"""

import json
import os
import urllib.request

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_TIMEOUT = 120
MAX_DESCRIPTION_CHARS = 6000

# Property order is load-bearing: Ollama fills schema fields in declaration
# order, and `reasoning` first forces deliberation before the verdict.
GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "verdict": {
            "type": "string",
            "enum": ["skip", "surface", "surface_priority"],
        },
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["reasoning", "verdict", "confidence", "tags"],
}

VERDICTS = tuple(GATE_SCHEMA["properties"]["verdict"]["enum"])

JSON_ONLY_INSTRUCTION = (
    "Respond only in JSON. Output a single JSON object matching the required "
    "fields and nothing else — no prose, no markdown fences."
)

# §4a defaults. Pass `options=` to gate_job to experiment without editing this.
DEFAULT_OPTIONS = {
    "temperature": 0,
    "seed": 11,
    "num_ctx": 8192,
}


class GateError(Exception):
    """The gate could not produce a usable verdict for this job."""


def ollama_url() -> str:
    return os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL).rstrip("/")


def build_job_prompt(title: str, company: str, location: str, description: str) -> str:
    return (
        f"Job title: {title}\n"
        f"Company: {company}\n"
        f"Location metadata: {location}\n"
        f"\n"
        f"Description:\n{description[:MAX_DESCRIPTION_CHARS]}\n"
        f"\n"
        f"Return the JSON verdict."
    )


def build_payload(
    system_prompt: str,
    job_prompt: str,
    model: str = DEFAULT_MODEL,
    options: dict | None = None,
) -> dict:
    return {
        "model": model,
        "system": f"{system_prompt.rstrip()}\n\n{JSON_ONLY_INSTRUCTION}",
        "prompt": job_prompt,
        "stream": False,
        "keep_alive": "5m",
        "format": GATE_SCHEMA,
        "options": {**DEFAULT_OPTIONS, **(options or {})},
    }


def http_transport(url: str, payload: dict, timeout: float) -> dict:
    """Default transport: POST to Ollama, return the parsed response object."""
    req = urllib.request.Request(
        f"{url}/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def parse_verdict(response: dict) -> dict:
    """Validate the Ollama response object into a verdict dict, or GateError."""
    text = response.get("response", "")
    try:
        verdict = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise GateError(f"model returned non-JSON response: {text[:200]!r}") from e
    if not isinstance(verdict, dict):
        raise GateError(f"model returned JSON but not an object: {text[:200]!r}")
    missing = [k for k in GATE_SCHEMA["required"] if k not in verdict]
    if missing:
        raise GateError(f"verdict missing required fields {missing}: {verdict!r}")
    if verdict["verdict"] not in VERDICTS:
        raise GateError(f"unknown verdict value: {verdict['verdict']!r}")
    return verdict


def gate_job(
    system_prompt: str,
    title: str,
    company: str,
    location: str,
    description: str,
    *,
    model: str = DEFAULT_MODEL,
    options: dict | None = None,
    transport=http_transport,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Send one job through the Qwen gate; return the parsed verdict dict.

    Raises GateError on a malformed model response. Transport-level failures
    (connection refused, timeout) propagate as-is so callers can distinguish
    "Ollama down" from "model misbehaving".
    """
    payload = build_payload(
        system_prompt,
        build_job_prompt(title, company, location, description),
        model=model,
        options=options,
    )
    response = transport(ollama_url(), payload, timeout)
    return parse_verdict(response)
