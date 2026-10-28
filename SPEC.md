# SPEC — v2 Apply/Triage CLI (the gate's labeling front-end)

**Status:** locked 2026-06-21.
**Authority:** this spec serves the labels and `v2/rubric.md`; if they
disagree with a line here, they win and this file is updated.

---

## 0. One-line purpose

A terminal-first daily triage tool over the gated SQLite that lets the user
clear each day's surfaced jobs (apply / consider / hold / skip) **and**, as a
side effect of every decision, accumulates an append-only, four-way-labeled
training set used to re-derive the rubric over time.

It is **two things at once**, and the second is the load-bearing one:
1. a fast triage + application-tracking tool (the obvious job), and
2. **a continuous labeling instrument** — the source of the next batch of
   ground truth that improves the frozen 67%-agreement gate. Triage is the vehicle; the labels are the point.

---

## 1. Why this exists

- The Qwen gate is **frozen at qwen2.5:14b**, action-agreement ~67%, FN 18 /
  FP 35 on the 200 labels. It is **not** trusted for
  autonomous apply — human review is mandatory.
- Model size is exhausted: **32b is off the table** (M4 can't run it). The only
  live improvement lever is **better labels → re-derived rubric + few-shot
  prompt** (NOT a model swap, NOT fine-tuning — see §7).
- The rubric (`v2/rubric.md`) is *derived from labels* ("labels win over this
  file"). This CLI keeps producing labels from real usage, continuing the exact
  process that built the original 200.

---

## 2. Decisions locked (2026-06-21)

| # | Decision | Choice | Why |
|---|---|---|---|
| Signal capture | every decision, append-only, 4-way | **YES** | training set needs the full taste distribution + immutability; a join can't reconstruct decision-time context |
| Storage | separate append-only `decisions` table | **YES** | re-gate/re-decide must never overwrite a label; gate-verdict-at-decision-time must be frozen next to the human label |
| Vocabulary | full four-way apply/consider/hold/skip | **YES** | matches the rubric's label space exactly → zero-translation feedback |
| Queue model | daily, resumable, with a *done* state | **YES** | 5–30 jobs/day is a queue to clear, not a DB to browse |
| Skip recovery | dedicated rescue view | **YES** | rescued skips are the highest-value labels AND the only defence against selection bias (§7) |
| Link rot | **deferred from v1** | open 404 gracefully, move on | real but minor, not on the critical path (§5 scope discipline) |
| Web UI | **deferred** | terminal-first | single user, tiny daily volume; earn web later via the headless core |

---

## 3. Desired outcome (definition of done)

A `v2/dashboard/cli.py` (Rich TUI) the user runs daily that:
1. Presents today's **undecided surfaced** jobs as a resumable queue,
   `surface_priority` first then `surface`, showing the gate's **reason** as the
   hero element (not a score — v2 has no score).
2. Lets the user decide each with one keypress in the **four-way** vocabulary,
   advancing to the next job; **done = queue empty**.
3. On **apply**, reuses the existing `shared/database/tracking.py` machinery
   (channel/version/tone, follow-up scheduling) unchanged.
4. Offers a **dedicated rescue view** of gate-skipped jobs to audit/rescue.
5. **Writes one immutable `decisions` row per action** (the training signal),
   in addition to mutating `Job.status` for the queue.
6. Reuses v1's follow-up manager and stats views (port, swap data contract).

**Done means:** a daily pass empties the queue; every action is captured as a
4-way label paired with the gate's decision-time verdict; the user can audit
skips; the existing apply/follow-up/stats flows still work; and a single export
command produces a clean training file (§7).

---

## 4. Data model

### 4a. New table: `decisions` (append-only — the training set)

In `shared/database/models.py`, new `Decision(Base)`, `__tablename__ =
"decisions"`, created via the existing `Base.metadata.create_all` and an
idempotent `migrate_*` helper mirroring `migrate_gate_columns`.

```
id              Integer PK autoincrement
job_id          String   FK -> jobs.id   (String: Job.id is a String)
decided_at      DateTime (UTC)
-- the gate's call AT DECISION TIME, frozen (never re-read from the Job row):
gate_verdict    String   -- skip / surface / surface_priority (snapshot)
gate_reason     Text     -- snapshot
gate_tags       Text     -- JSON list snapshot (S1..S6 etc.)
gate_model      String   -- snapshot (e.g. qwen2.5:14b)
-- the human label (the new ground truth):
human_label     String   -- APPLY / CONSIDER / HOLD / SKIP  (the rubric's space)
human_rule      String   -- optional: which rule the user is correcting
                         --   (S1..S6 on a reject; or the over-fired tag on a rescue)
note            Text     -- optional free text
source_view     String   -- 'queue' | 'rescue'  (records selection context, §7)
```

**Append-only contract:** rows are inserted, never updated or deleted. Deciding
on the same `job_id` twice (taste changed, posting changed) inserts a SECOND
row; both are kept. The training export reads this table as the source of truth.
A re-gate of the Job row never touches past decisions.

### 4b. Existing `Job` columns reused (already present — do NOT add)

- queue/status: `status` (new/reviewed/applied/ignored — extend usage, see §6),
  `gate_verdict`, `gate_reason`, `gate_tags`, `gate_confidence`.
- apply tracking: `applied`, `applied_at`, `applied_via`, `message_version`,
  `message_tone`, `followup_count`, `last_followup_at`, `next_followup_due`,
  `response_received`, `response_type`, `interview_scheduled`.

`Job.status` still mutates for the queue (a decided job drops out of the daily
queue). The **`decisions` table is the immutable record; `Job.status` is the
mutable UI state.** Both are written on each action.

---

## 5. Interaction (the daily flow)

### 5a. Main menu (port from `v1/dashboard/cli.py`, swap data contract)
`queue` (default) · `rescue` · `followups` · `stats` · `exit`.
Replace every `overall_score` / `recommended_action` / `why_good_fit` read with
`gate_verdict` / `gate_tags` / `gate_reason`.

### 5b. Queue view
- Rows = `Job.status == 'new'` AND `gate_verdict IN ('surface_priority',
  'surface')`, ordered `surface_priority` first.
- Header shows progress: `[3/20]` and counts per tier.
- Per job, display: title @ company · location/remote · **gate_verdict** ·
  **gate_reason** (hero) · gate_tags. Description available on demand
  (key to expand; the real posting opens in browser via `apply_link`).
- One-key actions, auto-advance to next:
  - `a` apply → run the existing `mark_applied(...)` flow (prompts
    channel; optional version/tone) → status='applied' → write Decision.
  - `c` consider → status='reviewed' (act-set; intend to apply) → write Decision.
  - `h` hold → status='reviewed' (uncertain) → write Decision.
  - `s` skip → optional one-key rule tag (S1..S6) → status='ignored' →
    write Decision (human_label=SKIP, human_rule=tag).
  - `o` open `apply_link` in browser. `n` next (defer, no decision). `b` back.
- **Done** when the queue is empty → a clear "queue cleared for today" state.
- **Resumable:** decided jobs leave the queue (status changed); restarting
  resumes on the remaining undecided jobs. No separate session state needed.

### 5c. Rescue view (selection-bias defence — see §7)
- Rows = `gate_verdict == 'skip'` (optionally not-yet-decided), with the gate's
  fired rule (`gate_tags`) and reason shown so the user audits *why* it buried
  each.
- Actions: `r` rescue → promotes to an act-set decision (apply/consider/hold)
  and writes a Decision with `source_view='rescue'`, `human_rule` = the
  over-fired gate tag (this is the FN-correcting label). `c` confirm-skip →
  writes a Decision agreeing with the gate (also valuable: a confirmed skip).
  `n` next.

### 5d. Apply/Follow-up/Stats (PORT, minimal change)
Reuse `shared/database/tracking.py` wholesale: `mark_applied`,
`mark_followup_sent`, `mark_response_received`, `get_jobs_needing_followup`,
`get_application_stats`, `get_followup_summary`. The follow-up manager and the
A/B stats view port from v1 with only the data-source swap.

---

## 6. `Job.status` lifecycle (extend existing string, no new column)

```
new        -> just gated, in the queue, undecided
reviewed   -> consider OR hold decided   (act-set-intent or uncertain; out of queue)
applied    -> apply decided + tracked
ignored    -> skip decided
```
The four-way *human_label* lives in `decisions`; `status` is the coarse UI
bucket. (consider vs hold both map to `reviewed` for the queue, but are
DISTINCT in `decisions.human_label` — the training set keeps the distinction the
rubric needs.)

---

## 7. The retrain loop — design constraints (NOT optional, NOT a footnote)

The user's plan ("use my decisions to retrain") is sound but has two failure
modes that this design must structurally prevent:

**C1 — Selection bias (the dangerous one).** The user only decides on jobs the
gate **surfaced**. Training only on surfaced jobs teaches the rubric to confirm
its own output and stay blind to its **false-negatives** (buried good jobs —
the worst error per §6). **Mitigation, baked into the design:**
- the **rescue view is mandatory**, not optional — it is the only channel that
  produces labels on gate-skipped jobs;
- `decisions.source_view` records 'queue' vs 'rescue' so the export can ensure
  skip-derived labels are represented and the set isn't surfaced-only;
- the retrain step (out of scope for this CLI, but documented here) must
  **include rescued + confirmed skips**, or it is biased by construction.

**C2 — "Fine-tune" is the wrong mechanism at this data scale.** The user will
accumulate tens of labels/week, not thousands. Fine-tuning a 14b needs far more
data, risks catastrophic forgetting, and the M4 can't host the tooling. **The
correct, in-budget mechanism is the one the rubric already uses:** re-derive
`v2/rubric.md` rules + the prompt's **few-shot examples** from the grown label
set (per `v2/rubric.md` §5: "inject §0 verbatim", "few-shot should oversample
S1"). New labels → new exemplars + tightened rule thresholds → better prompt,
**same model**. Reversible, ~dozens of labels suffice. **This CLI must NOT
imply fine-tuning anywhere in UI text; it produces labels, full stop.**

**C3 — Streaming labels are noisier than batch labels.** In-the-moment triage
mis-clicks happen. The append-only table + the ability to insert a corrected
later decision (never overwrite) is the mitigation; a future cleaning pass can
diff `human_label` vs `gate_verdict` to flag suspicious rows.

**Deliverable for the loop:** one export command,
`v2/dashboard/cli.py export` (or a small `v2/export_decisions.py`), that writes
`decisions` → a JSON the rubric-derivation process can read, shaped like
`data/labels.json` (so the existing rubric tooling consumes it unchanged).
Export only; the actual re-derivation is a separate, later task.

---

## 8. Existing systems touched

- **READ/REUSE unchanged:** `shared/database/tracking.py` (all public fns),
  `v1/dashboard/cli.py` (port the Rich bones: menu, table, detail, follow-ups,
  stats), `shared/database/models.py` `Job`, `get_session`, `get_engine`,
  `DB_PATH`.
- **EXTEND:** `shared/database/models.py` — add `Decision(Base)` +
  `migrate_decisions()` (idempotent, mirrors `migrate_gate_columns`).
- **NEW:** `v2/dashboard/cli.py` (the TUI), `v2/dashboard/__init__.py`,
  optionally `v2/export_decisions.py`.
- **Upstream contract (already committed, do not modify):** `v2/loop.py` writes
  `gate_verdict` on rows; this CLI reads it. The seam is the DB only.

---

## 9. Edge cases & failure behaviour

- **Idempotent apply:** applying twice must not double-record or re-prompt; a
  job already `applied` shows its tracking and a "re-open?" affordance, not a
  fresh apply. (Applications are real side effects.)
- **Applied-outside-the-tool drift:** provide a cheap "mark applied (already
  did it elsewhere)" path so the DB doesn't silently diverge from reality.
- **Empty queue:** explicit "cleared for today" state, not an error.
- **Decision on a job whose gate row changed:** the `decisions` row snapshots
  the gate verdict AT DECISION TIME, so a later re-gate can't corrupt history.
- **Re-decide same job:** allowed; inserts a new `decisions` row, keeps the old.
- **Dead `apply_link` (deferred):** `o` opens; if 404, user notices and moves
  on. No link-state tracking in v1 (§2). Add later if it bites.
- **Empty/missing `gate_reason`:** show the gate_tags + a "(no reason)" note;
  never crash.

---

## 10. Scale / performance

- Daily surfaced set: ~5–30 rows. Full table: low thousands. SQLite + Rich is
  comfortably sufficient; no pagination beyond v1's `[:50]` cap needed.
- Single user, single machine. No concurrency, no auth, no hosting.

---

## 11. Out of scope for v1 (explicit, per §5 scope discipline)

- Web UI (terminal-first; earn web later via the headless seam).
- Link-rot tracking / dead-link capture.
- The actual rubric re-derivation / any model retraining (this CLI EXPORTS
  labels; consuming them is a separate task).
- Analytics on the captured signal beyond the existing v1 stats view.
- Auto-apply / any autonomous send (gate is below its bar; human-in-the-loop
  is mandatory).
- Fixing the pre-existing duplicate `get_session`/`init_db` in `models.py`
  (separate `refactor:` commit; do not bundle).

---

## 12. Build order (suggested)

1. `Decision` model + `migrate_decisions()` + a unit test for the append-only
   contract (re-decide inserts, never overwrites).
2. Headless decision-write helper `record_decision(job, human_label, rule,
   note, source_view)` — callable, testable, the single seam every UI action
   goes through.
3. Queue view + four-way actions wired to `record_decision` and (for apply)
   the existing `mark_applied`.
4. Rescue view (the bias defence).
5. Port follow-ups + stats from v1 (data-source swap).
6. `export_decisions` → labels.json-shaped output.

Each step ends green before the next. Commit per logical step; keep scaffolding
and exploratory scripts out of commits (the publish bar: useful to a forker, no
PII/secrets).
```
