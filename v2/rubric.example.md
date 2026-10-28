# Eligibility Rubric — example

Copy this file to `v2/rubric.md` (gitignored) and rewrite it from your own
hand labels. The gate injects sections 0–3 into the model's prompt word for
word; sections 4 and 5 are notes to you and never reach the model. Keep the
`## 0.` … `## 5.` headings: the prompt builder cuts on them.

**Authority:** your labels in `data/labels.json` win over this file, and this
file wins over guesses. Give every rule its evidence count from the labels.
Strength tiers: **STRONG** ≥8 supporting labels, **SUPPORTED** 3–7, **WEAK**
1–2 (recorded, not enforced).

**How the rules were found:** write a one-line note on every job you label
(why APPLY, why SKIP), cluster the notes by stated reason, then check each
cluster against the label counts. Reasons generalize; keyword co-occurrence
does not.

The candidate below is made up. The rule structure (S1–S6, C/H/P) is what the
gate prompt expects.

---

## 0. The candidate, as the labels show them

- Real stack: **Go and Python backend services, PostgreSQL, Docker.** Some
  React for internal tools, not enough to be hired on.
- Identity: IC backend engineer. Bogotá, Colombia. Full-remote required;
  "remote" restricted to countries they are not in is a SKIP.
- **CONSIDER is part of the act-set.** A gate that buries CONSIDER buries
  applications.

---

## 1. SKIP rules (the gate may auto-skip ONLY on these)

Two kinds, checked in two passes. **PHASE 1 = objective hard gates** (S1,
S4, S5): a fact is present in the text or it is not. CHECK THESE FIRST.
**PHASE 2 = stack/role judgment** (S2, S3, S6): surface-biased — skip only on
a clear, quotable match. Every skip must quote the disqualifying text; if you
cannot quote it, you may not skip on that rule.

---

### PHASE 1 — objective hard gates (check first)

### S1. Explicit geographic eligibility the candidate fails — STRONG (n SKIPs)
Fire S1 only when the description states applicants MUST be located in, or
authorized to work in, a specific country/region that does not include
Colombia or "worldwide". The disqualifying phrase must actually appear in
the description.
**Boundary (surface, not S1):** the location metadata field; a company
"based in" somewhere; a city in the title; "remote" / "100% remote"; a
time-zone preference; a region merely *named* without an explicit "must be
located/authorized" requirement.

### S4. Visa / clearance demanded — SUPPORTED (n SKIPs)
"Requires security clearance", "visa required." Partially covered by the
hard gates' no_sponsorship regex.

### S5. Unpaid — SUPPORTED (n SKIPs)
"No stipend", "unpaid", "equity only." Owned by `hard_gates.py`; the model is
the typo-tolerant backstop.

---

### PHASE 2 — stack/role judgment (surface-biased; skip only on a clear quote)

### S2. Primary stack the candidate doesn't work in — STRONG (n SKIPs)
Role's PRIMARY language/platform is Java, C#/.NET, PHP or Ruby.
**Boundary:** a language merely LISTED among others the candidate has →
HOLD, not SKIP. Primary = the title says it, the requirements lead with it,
or it dominates the description. Go/Python/PostgreSQL ARE the candidate's
stack — never skip those as wrong-stack.

### S3. Wrong field/role type — STRONG (n SKIPs)
Data science, mobile, QA, UI/UX-led frontend, DevRel, scrum.
**Boundary:** SRE and platform roles are a stretch to HOLD, never auto-skip.

### S6. Title is Staff/Principal ONLY — SUPPORTED (n SKIPs)
Skip ONLY when the TITLE contains "Staff" or "Principal". "Senior", "Sr.",
and "Lead" are NOT skips — surface them every time.

---

## 2. Surface-for-user rules (gate must NOT decide)

### C1. Learnable gap in an otherwise-matching role — STRONG
One missing tool next to a matching stack (Kafka on a Go service, a cloud
the candidate hasn't used) → CONSIDER, never SKIP.

### C2/H3. Ambiguous remote scope — SUPPORTED
"Remote (LATAM)" with no stated restriction, "remote within the region."
Ambiguous → the candidate chooses. The gate may attach its guess and reason,
never a verdict.

### H1. Seniority stretch — STRONG
Senior/lead titles or 6–8 years required on otherwise-fitting roles. Must
remain visible.

### H2. Gig / freelance / staffing-agency — SUPPORTED
Distrust, not refusal → HOLD.

### H4. Secondary-stack mismatch — SUPPORTED
Required-but-not-primary Java/PHP alongside the candidate's stack → HOLD
(see the S2 boundary).

### P1. Home-region positive signal — WEAK
A posting that names Colombia or LATAM as eligible. Boost, never gate.

---

## 3. APPLY profile (what the gate should rank up)

Backend or platform IC roles in Go or Python with no flags.

---

## 4. Known label conflicts (to rule on — not bugs in the gate)

1. **Job 12** labeled CONSIDER although its location matches an S1 SKIP
   pattern. Slip, or an exception worth a rule?

---

## 5. What this means for the Qwen prompt

- Section 0 goes into the prompt as the candidate profile; the model never
  infers the stack from CV text.
- Three-way output: `skip` (only S1–S6, rule named), `surface`,
  `surface_priority`. Uncertainty → surface.
- Evaluate by rule cluster (S1/S2/S3…), not just overall recall: a miss
  pattern on one rule is fixable; a blended number is not.
