# M4 thermal behaviour under the gate

Measured 2026-07-15 with `macmon pipe` on the Mac, aligned against a live
40-job `qwen2.5:14b` eval driven from the ThinkPad over the tunnel. These are
measured numbers, not estimates.

## What runs the GPU hot

The M4's GPU idles ~36 °C. Under **back-to-back** gate inference (`--cooldown 0`)
it heat-soaks in under a minute and rides a **95–101 °C plateau** (peak 101.5 °C
observed), GPU pegged ~97 % busy at ~24 W. Latency held flat at ~8 s/job even at
100 °C, so within a ~5-minute burst it holds clocks and does not hard-throttle.

101 °C does not damage the machine — Apple Silicon throttles or shuts down before
it can hurt itself. The concern is sustained heat and fan noise, not damage.

## The lever is spacing, not quantity

The hot variable is how *close together* the calls are, not how many. The GPU
sheds heat fast:

| idle time after a burst | GPU temp |
|---|---|
| 0 s | ~97 °C |
| 15 s | ~66 °C |
| 60 s | ~50 °C |
| ~3 min | ~43 °C (near idle) |

So a per-call cooldown collapses the average temperature. `--cooldown 60` (the
eval default) means ~8 s of work then ~60 s idle back to ~50 °C — the GPU never
approaches the ceiling. A full 150-job paced run is therefore *not* "hours hot";
it is mostly idle-cooling with brief warm blips, and its length is what keeps it
cool. You never need to send fewer jobs — space them. The scraper/query workflow's
randomised intervals are already the cool pattern.

`eval_gate.py`'s cooldown is a fixed interval, clamped under the 5-minute
`keep_alive` so the model never unloads mid-batch. It used to adapt — backing off
on latency creep past 1.5× a running minimum — but that ratcheted to the cap on a
cool GPU (latency does not track heat on this hardware; see the 2026-07-15 capture
above), so the adaptive arm was removed in favour of the fixed spacing this file
argues for.

## What is NOT a lever

There is no macOS or Ollama GPU thermal knob — no frequency cap, no power limit,
no low-power GPU mode (`NSProcessInfo.thermalState` is read-only; Low Power Mode
does not touch GPU compute). Duty-cycle pacing is the accepted mitigation.

Do **not** reach for `OLLAMA_NUM_GPU` to shed heat: offloading layers to the CPU
changes the Metal-vs-CPU float path and can flip verdicts, so a `NUM_GPU`-capped
run can never be a certified baseline. Leave it alone.

## Measuring again

`macmon pipe -i 1000 > m4_thermal.jsonl` on the Mac; each line is one
JSON sample (`temp.gpu_temp_avg`, `gpu_power`, `gpu_usage`). macmon occasionally
emits a glitch sample (2.4 °C or an absurd high) — filter `10 < t < 110` before
computing stats. `powermetrics` is unreliable for power on Apple Silicon; prefer
macmon.
