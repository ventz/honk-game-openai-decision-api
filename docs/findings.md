# Findings

What we learned about OpenAI's Decisions API (`/v1/decisions`, `gpt-6-luna`, public beta) by using it to play Honk.

> These come from one small project: one deliberately easy level ([the easiest level you'll ever play](https://www.reddit.com/r/honk/comments/1tnt19h/the_easiest_level_youll_ever_play/)) plus endless mode, 8-20 runs per setup, measured on 2026-10-08 and 2026-10-09. Treat them as hypotheses to check on your own task, not as settled rules.

## Contents

- [Results](#results)
- [1. Ask a fact, keep the action in code](#1-ask-a-fact-keep-the-action-in-code)
- [2. Plain words beat raw coordinates](#2-plain-words-beat-raw-coordinates)
- [3. Vision was not usable for fine positions](#3-vision-was-not-usable-for-fine-positions)
- [4. The control loop matters as much as the model](#4-the-control-loop-matters-as-much-as-the-model)
- [5. Latency](#5-latency)
- [6. Screenshots that flash](#6-screenshots-that-flash)
- [7. Endless mode (in progress)](#7-endless-mode-in-progress)
- [How we got there](#how-we-got-there)
- [Not yet tested](#not-yet-tested)

## Results

The level is "the easiest level you'll ever play" on r/honk: one pipe pair, a hazard on the floor, and a finish line.

| Setup | Completed the level | Median decision time |
|---|---|---|
| Game state as text, factual question, tap settle, stall handling | **20/20** (rechecked 20/20 on 2026-10-09 with the current code) | 110ms (149ms on the recheck) |
| Same, without stall handling | 12/13 (10/10, then 2/3) | 105-115ms |
| Game state as text, factual question, no tap-settle fix | 7/10 | 125ms |
| Screenshots (360px), factual `above`/`below` choice | 0/10 | ~280ms |
| Game state as text, earlier framings (raw numbers, action rules) | 0/7 single runs, each a different framing | ~120ms |

## 1. Ask a fact, keep the action in code

A predicate written as an action plus a rule came back **inverted**:

```text
instructions: "Flap if the goose will be below the middle; do not flap if above."
input:        "The goose will be 40 pixels above the middle of the gap."
probability:  0.99   <- says flap, the opposite of the rule
```

The same inputs asked as a plain fact were answered perfectly:

| Input | "Flap if below…" (action) | "Will the goose be below the middle of the gap?" (fact) |
|---|---|---|
| 40px below | 0.78 | 1.00 |
| 10px below | 0.37 | 1.00 |
| 10px above | 0.99 | 0.00 |
| 40px above | 0.93 | 0.00 |

An `above`/`below` `choice` was also correct. The bot asks the factual predicate and flaps in code when the answer is "below". That one change took the level from never clearing the pipe to 7/10.

## 2. Plain words beat raw coordinates

Sending numbers (screen `y` that grows downward, velocity, the safe band's edges, a predicted position) gave unreliable answers in both directions. Flipping the axis so up means up didn't fix it, and neither did adding a prediction. A one-sentence description ("will be 40 pixels below the middle of the gap") worked.

## 3. Vision was not usable for fine positions

With live 360px game screenshots and the same factual `above`/`below` question, the model answered "above" on every frame and never flapped (0/10). Synthetic test frames (`benchmarks/`) showed the same bias: predicates sat near p≈0 wherever the bird was. Decisions classifies scenes well, but it couldn't judge pixel-level vertical alignment here.

## 4. The control loop matters as much as the model

With the factual question, every answer in the failed runs was right (p=1.00 or 0.00). All 3 failures out of 10 were double flaps: the bot acted on a frame read before its previous tap had registered, flapped twice, and flew into the top pipe. Dropping any decision based on a frame read less than 60ms after the last tap (`TAP_SETTLE_S`) took the success rate from 7/10 to 10/10.

The one failure after that fix was not a wrong answer. Every request in flight was slow at once, so no answer arrived for about 350ms while the goose fell from the middle of the gap into the bottom pipe. The model's last answer before the stall was correct. The fix keeps the model in charge rather than adding a code fallback. A request with no answer after 250ms (`ABANDON_S`) stops counting toward the in-flight limit (then 3), so fresh questions keep going out during a stall. Its answer is ignored if it arrives, because the frame it describes is old by then. A hard ceiling on pending requests (then 8) stops a flood. Over 20 runs, 10 late answers were dropped (0-2 per run), and all 20 runs completed. The final `END` line of each failure trace in `logs/` records how many requests were still unanswered.

Other parts of the loop:

- **Overlapping requests.** A new decision starts every 25ms with up to 8 in flight (50ms and 3 for the first 20/20; endless needed the faster pace), and only the newest answer acts. A single slow call (up to ~1.5s) therefore can't freeze control.
- **Predicting the latency.** The bot describes where the goose *will be* when a tap sent now lands, about 7-9 frames ahead, by stepping the game's own physics forward frame by frame (see [section 7](#7-endless-mode-in-progress)).
- **Handing over at the start.** The start-up tap now hands control to the model the moment the game reports a live run. A fixed 0.4s wait there (added for endless mode's retry screen) let the goose fall ~100px unattended and dropped the easy level to 8/20 at ~185ms decisions; without it, 20/20 again on 2026-10-09 (149ms median).
- **Warming up the connection.** The first call on a cold HTTPS connection took ~750ms, long enough to fall into a pipe, so the bot sends a warm-up request before the first tap.

## 5. Latency

| Input | Median | Notes |
|---|---|---|
| One short sentence | ~105-125ms | occasional spikes up to ~1.5s |
| 360px JPEG | ~280ms | with 3 requests overlapping |
| Full 1448px JPEG | ~550ms | downscale images before sending |
| First call, cold connection | ~750ms | send a warm-up request |

Reading the game state over CDP takes ~4ms. A canvas screenshot takes ~40ms (raw CDP) or ~80ms (Playwright element screenshot).

## 6. Screenshots that flash

Capturing with any in-browser scaling (a CDP `clip` with `scale` ≠ 1, or Playwright's `scale="css"`) made the visible page flash on every capture. We confirmed it with a phased test that ran one suspect at a time, labeled on screen. Capturing at scale 1 and shrinking in Python doesn't flash. The MP4 recorder also asks for full-size frames for the same reason.

## 7. Endless mode (in progress)

[Endless mode](https://www.reddit.com/r/honk/comments/1o7wahu/endless_mode/) is the real test. The best run so far passed **23 pipes** (on camera), and `--endless` averages about 7 when the API answers in ~140ms. Scores still swing widely: one run scores 23, the next 2. They fall toward 0 when latency climbs past ~250ms, which it did repeatedly during testing (one sequential check: median 362ms, server-side 258ms, nowhere near the rate limit).

| Setup (2026-10-09, ~140ms per decision) | Scores | Mean | Best |
|---|---|---|---|
| Before the physics and tap fixes below | several batches of 8-10 | 2-5 | 19 |
| `--lat-slots 80,...,230` (7 moments), act only on the matching one | 0, 6, 4, 7, 9, 5, 0, 14 | 5.6 | 14 |
| 10 moments, plus a scheduled flap for a later one | 2, 5, 12, 4, 19, 2, 8, 1 | 6.6 | 19 |
| `--endless` (the row above), 10 runs on camera | 10, 4, 8, 4, **23**, 2, 2, 14, 4, 2 | 7.3 | **23** |

What the traces showed:

- **Single, moving pipes.** Later in a run, endless spawns pipes with only a top or only a bottom half, many of them moving, and around score 17-22 clusters of closely spaced pipes. The state reader accepts either half and estimates how fast the gap is moving.
- **The model's answers were right; the geometry was the problem.** Pipes are 297px apart and 74px wide, leaving ~45 frames of open air between them, while consecutive gaps can be 150-330px apart vertically. Runs died climbing too slowly or overshooting the bottom lip after a long fall.
- **Latency bursts kill.** Several runs died with 8-12 answers still outstanding. Healthy latency is a ~120ms median with 2-14% of calls over 250ms; in a bad hour, 25-73% are. There is no `service_tier` on Decisions, server-side processing is ~45ms, and the SDK, raw `httpx`, HTTP/1.1 and HTTP/2 all performed the same.

What went into those numbers:

1. **The game's exact physics.** Sampling the game frame by frame showed it steps at a fixed 60Hz in game units, drawn at `gameInstance.scale` (~1.59) screen px per unit, with drag and a speed cap: `y += scale·v; v = min(10, 0.98·(v + 0.4))`, and a flap sets `v = -6.3`. That predicted a 23-frame fall to within 0.1px. The textbook `y + v·t + ½·g·t²` used before was 59px short over the same fall, so every question described a goose higher than it would really be.
2. **A double-tap race.** The bot recorded the tap time *after* the click returned over CDP. An answer that arrived during the click still saw the old time, passed the tap-settle check and flapped again: about 12% of taps were doubles. Recording the time before sending the click fixed it.
3. **Latency-matched moments (`--lat-slots`).** One prediction assumes one latency, and latency jitters. Instead, each request asks about several moments at once: where the goose will be if the tap lands after 80ms, 105ms, ... 305ms. When the answer comes back, the bot acts on the moment matching how long it actually took. Offline, the model got 96/98 moments right over 7 moments and 139/140 over 10; every miss was one moment late, right at the zero crossing. Asking about 10 moments adds ~23ms.
4. **Scheduled flaps.** If the matching moment doesn't need a flap but a later one does, the bot schedules a tap for that later moment; any fresher answer replaces it. During a burst of slow answers the scheduled tap still lands on time (mean 5.6 → 6.6, best 14 → 19).
5. **Switching gaps sooner and leaning toward the next one** (`--lean 12 --pass-px 4`), used in every row above, though on its own it only tied the default: move on once a pipe is 4px behind instead of 30, and while still in this gap drift toward the next, keeping 12px below the top lip after a flap and 30px above the bottom lip.

`--endless` turns on 3-5 with `--abandon 0.3`. The easy level keeps its single-question defaults.

Ideas that didn't help:

| Flag | Idea | Result (8-10 runs, ~150ms) |
|---|---|---|
| `--plan 3` | Ask about a short climb: moments A, B, C, each assuming a flap at the earlier ones, so one answer can chain flaps | mean 1.75-2.9, worse |

Wording matters with several moments: one signed line per moment ("Moment A: goose height relative to target: +12 px (negative means below).") scored 20/20 offline, while a single run-on sentence left moment A soft near zero (p=0.44 at -3px).

## How we got there

1. Synthetic benchmarks (`benchmarks/`): fast (~110-130ms), but it couldn't place a bird relative to a gap.
2. Live, screenshots only: 0 taps, falls every time.
3. Live, game state as numbers: wrong flaps in both directions.
4. Live, game state as one sentence with an action rule: still wrong. The offline polarity test exposed the inversion.
5. Factual predicate: 7/10.
6. Tap-settle fix: 10/10, then 2/3 when every request stalled at once.
7. Stall handling: 20/20.

## Not yet tested

Only one very easy level is solved, and endless mode is in progress (above). Custom levels with bullets or other moving obstacles are untested.
