"""Per-model sampling/behaviour profiles.

Different model families have different *documented-best* settings, and running
one model on another's settings is a confounded test. qwen2.5 is tuned for
greedy decoding (temperature 0, deterministic — the certified gate); Qwen3's own
docs say greedy decoding *degrades* it and give different settings for thinking
vs non-thinking mode. One project, one config table keyed by model name — not two
forks.

A model not in the table falls back to {}, i.e. the gate's existing
DEFAULT_OPTIONS in qwen_gate.build_payload — so unknown/old models behave exactly
as before. Nothing here changes qwen2.5's certified behaviour: its profile IS the
current defaults.

Sources (verified 2026-06-24):
  https://qwen.readthedocs.io/en/latest/getting_started/quickstart.html
  - Qwen3 non-thinking: temperature 0.7, top_p 0.8, top_k 20, min_p 0
  - Qwen3 thinking:     temperature 0.6, top_p 0.95, top_k 20, min_p 0
  - "DO NOT use greedy decoding (temperature 0)" for Qwen3.
"""

# num_ctx is held at 8192 across profiles to match the certified gate's context
# window (the prompt + longest job description fit; no silent truncation).
_NUM_CTX = 8192

MODEL_PROFILES = {
    # The certified incumbent. Greedy + fixed seed = deterministic, reproducible.
    "qwen2.5:14b": {
        "options": {"temperature": 0, "seed": 11, "num_ctx": _NUM_CTX},
        "no_think": False,   # qwen2.5 has no thinking mode; flag is a no-op here
    },
    # Qwen3 non-thinking (the gate default candidate): terse verdicts, no <think>.
    "qwen3:14b": {
        "options": {"temperature": 0.7, "top_p": 0.8, "top_k": 20,
                    "num_ctx": _NUM_CTX},
        "no_think": True,
    },
    # Qwen3 thinking-ON variant — addressed by the ':think' alias so a single
    # --model value selects both the weights and the behaviour. The eval maps
    # this alias back to the real 'qwen3:14b' tag before calling Ollama.
    "qwen3:14b:think": {
        "options": {"temperature": 0.6, "top_p": 0.95, "top_k": 20,
                    "num_ctx": _NUM_CTX},
        "no_think": False,
    },
    # Qwen3 GREEDY (temperature 0) — against Qwen's general guidance, but a
    # job-eligibility gate WANTS deterministic verdicts. This isolates
    # temperature as the variable vs qwen3:14b (temp 0.7): same model, same
    # no-think, only the sampling differs. Watch for the repetition/degradation
    # the docs warn about (would show as n_errors > 0).
    "qwen3:14b:greedy": {
        "options": {"temperature": 0, "seed": 11, "num_ctx": _NUM_CTX},
        "no_think": True,
    },
}

# Aliases that are not real Ollama tags -> the tag to actually send.
_ALIAS_TO_TAG = {"qwen3:14b:think": "qwen3:14b",
                 "qwen3:14b:greedy": "qwen3:14b"}


def resolve_model(name: str) -> str:
    """Map a profile alias to the real Ollama model tag."""
    return _ALIAS_TO_TAG.get(name, name)


def profile_for(name: str) -> dict:
    """Return {'options': dict, 'no_think': bool} for a model name/alias.
    Unknown models -> empty options + no_think False (i.e. current behaviour)."""
    p = MODEL_PROFILES.get(name)
    if p is None:
        return {"options": {}, "no_think": False}
    return {"options": dict(p["options"]), "no_think": p["no_think"]}
