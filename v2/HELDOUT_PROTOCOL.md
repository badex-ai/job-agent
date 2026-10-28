# Held-Out Evaluation Protocol — pre-registered 2026-06-24

**This file is written BEFORE a single held-out label exists, and committed
before the eligibility-only re-scope is built.** Its purpose is to fix the
scoring function, the baseline, and the pass/fail rule in advance, so the
result of the re-scope cannot be rationalized after the numbers come in. This
is the discipline that was missing the first time (a change is
only "better" if it improves a number on held-out labels the prompt wasn't
tuned against — and the 200 are now all tuning data).

Once the held-out set is labeled and measured against, it is SEALED: it may be
measured against, never tuned against. Editing this protocol after seeing a
result is the exact failure it exists to prevent. If a definition here turns
out to be wrong, that is a finding to record openly — not a quiet edit.

---

## 1. The sets (already sealed)

`data/heldout_unlabeled.json` (git-ignored, local-only), sealed by
`v2/seal_heldout.py` with `seed=20260624` from the 739 never-labeled DB jobs
that carry a real description:

- **HEADLINE — 100 jobs, pure random.** Carries the real base rate of incoming
  traffic. **This is the only set whose FP/FN rate we report as a headline
  number.** A move here means what it says.
- **STRESS — 50 jobs, stratified** (survivors of the objective hard gates only).
  Concentrates S2/S3 stack/role judgment cases. Its base rate is distorted by
  design, so it is a **diagnostic**: it tells us *whether and where* the
  re-scope changes S2/S3 behaviour, never a headline rate.

Disjoint. Labeled by hand via `v2/labeling/label_heldout.py` (which hides each
job's `set` and `fired_gates` so the labeling can't be biased).

---

## 2. The scoring function (pinned to the existing harness)

Scoring is exactly what `v2/eval_gate.py` already computes — no new metric is
invented for the held-out run. Pinned definitions (eval_gate.py:280–281):

- The four labels collapse to two action-classes:
  - `SKIP` → "don't surface"
  - `APPLY / CONSIDER / HOLD` → the **act-set**, "surface" (CONSIDER and HOLD
    MUST stay visible — rubric §0).
- Gate verdicts `surface` and `surface_priority` both mean "surface".
- **FN (false negative — the dangerous error, §6):** a job labeled `SKIP` that
  the gate surfaced. Wastes an application slot quietly.
- **FP (false positive — buried good job):** a job labeled act-set that the
  gate skipped. The cost the re-scope is meant to cut.

FN is the error minimized first (§6). FP is the error the re-scope attacks.

---

## 3. Baseline — measured on the 200, stated honestly

The frozen baseline (qwen2.5:14b, guard arm, §9.3) on the **200 tuning labels**:

| | FN (leaked SKIP) | FP (buried good) | n |
|---|---|---|---|
| 0–99   | 12 | 20 | 100 |
| 100–199 | 6 | 16 | 100 |
| **full 200** | **18** | **36** | 200 |

Source artifacts: `data/eval_guard_rep2_0_99.json`, `…_100_199.json`.

**This baseline is on the 200, NOT on the held-out set, and the two are not
directly comparable** — the held-out set has a different (true) base rate, and
~9% of the headline 100 fire an objective hard gate vs the 200's ~48% SKIP mix.
So we do NOT pre-register a predicted held-out FN/FP number; predicting one
would be false precision. What we pre-register is the **decision rule** below,
which is expressed in terms of the *change* the re-scope produces, measured by
running BOTH arms (current gate, re-scoped gate) over the SAME held-out labels
in the SAME session — the two-arm protocol §9.3 already established as the only
trustworthy comparison.

---

## 4. The decision rule (the part that must not move)

The eligibility-only re-scope changes the gate so it **auto-skips ONLY on the
objective hard-NO rules (S1 geo / S4 visa / S5 unpaid / S6 staff-only)** and
**surfaces every S2/S3 stack/role judgment** for the human/ensemble. The
hypothesis: this cuts FP sharply (the buried-good-job class is mostly S2/S3
over-skips) at little or no FN cost (the objective rules already catch the
crisp SKIPs).

Run both arms over the labeled HEADLINE set. Let baseline = current gate,
rescoped = eligibility-only gate. The re-scope **PASSES** iff, on HEADLINE:

1. **FP strictly drops:** `FP(rescoped) < FP(baseline)`. This is the whole
   point; no FP improvement = no reason to ship.
2. **FN does not materially rise:** `FN(rescoped) <= FN(baseline) + 1`. The +1
   tolerance absorbs this gate's known run-to-run non-determinism (§9.3: single
   jobs flip on Metal float-order); a rise of ≥2 is a real regression on the
   error we care about most, and FAILS.

If both hold → ship the re-scope and record HEADLINE FP/FN as the new honest
baseline. If FP drops but FN rises ≥2 → the re-scope trades the dangerous error
for the cheap one; do NOT ship as-is, investigate which S-rule leaked. If FP
does not drop → the FP was not where we thought; the re-scope is a no-op and the
diagnosis behind the re-scope is wrong — reopen it.

**STRESS set is read second, only to explain the headline**, never to override
it: it shows *which* S2/S3 cases moved. A re-scope that passes on HEADLINE but
looks ugly on STRESS is still a pass (STRESS over-weights the hard cases by
construction); a re-scope that fails on HEADLINE is a fail regardless of STRESS.

---

## 5. Harness gap to close before measuring (pre-registered, not yet built)

`eval_gate.py` reads `data/labels.json` hardcoded (line 421) and slices it with
`--offset/--limit`. It **cannot yet point at the held-out file.** Before any
held-out measurement it needs a `--labels <path>` argument (and to read the
held-out schema: `jobs[].label` + `description` + `title` + `location`, which
already matches). This is a mechanical change to make BEFORE labeling is done,
and it does not touch the scoring math in §2 — so it cannot be used to move the
decision rule. Pinning it here records that the rule was set before the reader
was wired to the new file.

---

## 6. What would invalidate this protocol (stated so it's falsifiable)

- If labeling reveals the held-out base rate is wildly off from the DB pool
  (e.g. headline turns out ~50% SKIP, not ~9% hard-gated + judgment), the
  random draw was unrepresentative — record it, do not silently re-draw.
- If both arms can't be run in one session (M4 thermal/availability), the
  comparison is cross-session and §9.1's lesson applies — the result is not
  trustworthy and must be re-run, not reported.
- If the re-scope passes §4 but action-agreement is still far below §6's 85%
  bar, that is expected and not a failure of THIS experiment — the re-scope
  targets FP, not the soft-label agreement ceiling, which §9.3 already ruled a
  structural/ensemble problem, not a gate-prompt one.
