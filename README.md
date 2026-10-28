# job-agent

Pulls new job postings every few hours and decides which ones are worth a
person's time before they see them. Plenty of "remote" postings turn out to
mean remote inside one country, and a "full-stack" title can hide a Java job.
This tool reads them first.

A local model makes the call, so postings and personal details stay on your
own machines. The design rule it follows: a bad job that slips through costs
the reader a few seconds, but a good job wrongly skipped is never seen again.
When unsure, it shows the job.

## How it works

1. **Scrape.** LinkedIn's guest search, Indeed (per country, through
   `python-jobspy`), Remotive and Himalayas, using the keywords in your
   profile. New postings land in SQLite (`data/jobs.db`); reposts are merged.
2. **Hard gates** (`v2/hard_gates.py`). A few regexes for things that need no
   judgment: "unpaid", "no sponsorship", "US citizens only". Which ones run is
   set in the profile.
3. **Model gate** (`v2/qwen_gate.py`). Qwen 2.5 14B on Ollama gets the posting,
   the candidate's profile and the rubric, and answers `skip`, `surface` or
   `surface_priority`. A skip must name a rubric rule and quote the sentence in
   the posting that triggers it; `v2/eval_gate.py` checks the quote really is
   in the text and turns an invented one into `surface`.
4. **Review.** `python -m v2.webapp` serves a local page
   (http://127.0.0.1:8765) with the surfaced jobs, the skipped ones in a
   separate tab so a wrong skip can be rescued, and an applications tracker.
   `python v2/dashboard/cli.py` is the same queue in the terminal.

`v2/cron_run.sh` runs one scrape-and-gate cycle; it is meant for cron.

## How the gate was measured

The rubric and the numbers come from a sample labelling set: 200 postings,
each hand-labelled APPLY, CONSIDER, HOLD or SKIP with a one-line note on why
(18 / 39 / 48 / 95). The labels are not in the repo.

- **Keywords can't do this job.** Only 7 of the 95 SKIPs could be caught by
  a keyword rule. The rest needed reading: an eligibility limit in the last
  lines of the description, Java as the real language behind a "full-stack"
  title, "no payement".
- **7B vs 14B.** The first comparison favoured 7B, but the two runs used
  different versions of the evidence check. Re-run on the same harness, 7B's
  skip recall fell from about 60% to 40.8%. 14B won the clean comparison, and
  every result file now records the model that produced it.
- **Where it stopped.** Over all 200, the 14B gate lets 18 skippable jobs
  through and wrongly skips 35, about 67% agreement with the labels. Two full
  runs agreed to within one job. The worst error class was "wrong stack": the model
  skipped jobs whose requirements listed the candidate's language as one
  option among several. A prompt check fixed that pattern. A token-matching
  guard on top fixed 6 more and wrongly let 2 real skips through, so it was
  dropped: whether a stack fits is about meaning, and prompt edits had run
  out. The next step is a stronger model, measured on a sealed held-out set
  (`v2/seal_heldout.py`, `v2/HELDOUT_PROTOCOL.md`).
- **Heat.** On an M4 Mac, back-to-back calls held the chip near 95°C.
  It cools 30°C within 15 seconds, so the loop sleeps between calls
  (`--cooldown`). See `v2/THERMAL.md`.

## Make it yours

Everything personal lives in gitignored files.

1. `pip install -r requirements.txt` (Python 3.12), then install
   [Ollama](https://ollama.com) and `ollama pull qwen2.5:14b`.
2. `cp .env.example .env` and set `OLLAMA_URL` if Ollama runs on another
   machine.
3. `cp shared/config/_profile.example.yml shared/config/_profile.yml`. The
   gate reads location, stack, languages you won't be hired on, salary floor
   and deal-breakers from it; the scraper reads search keywords and which
   countries to scan on Indeed.
4. `cp v2/rubric.example.md v2/rubric.md`, then rewrite the rules from your
   own labels. The example explains how.
5. Put a plain-text CV per job type in `data/` (the `cv_file` paths in the
   profile).
6. Optional, to measure your own gate: label postings into `data/labels.json`
   (a list of jobs with `title`, `company`, `location`, `description`,
   `label`, `notes`) and run `python v2/eval_gate.py`.

Then `python v2/loop.py --once` runs one cycle and `python -m v2.webapp`
shows the result.

`v1/` is the earlier version, which scored jobs against the CV with the Claude
API. It is kept for reference and is not maintained.
