# Findings

What we learned about OpenAI's Decisions API (`/v1/decisions`, `gpt-6-luna`, public beta) by using it to play Honk.

> These come from one small project: one level, 10 runs per setup, measured on 2026-10-08. Treat them as hypotheses to check on your own task, not as settled rules.

## Contents

- [Results](#results)
- [1. Ask a fact, keep the action in code](#1-ask-a-fact-keep-the-action-in-code)
- [2. Plain words beat raw coordinates](#2-plain-words-beat-raw-coordinates)
- [3. Vision was not usable for fine positions](#3-vision-was-not-usable-for-fine-positions)
- [4. The control loop matters as much as the model](#4-the-control-loop-matters-as-much-as-the-model)
- [5. Latency](#5-latency)
- [6. Screenshots that flash](#6-screenshots-that-flash)
- [How we got there](#how-we-got-there)

## Results

The level is "the easiest level you'll ever play" on r/honk: one pipe pair, a hazard on the floor, and a finish line.

| Setup | Completed the level | Median decision time |
|---|---|---|
| Game state as text, factual question, tap-settle fix | **10/10**, then 2/3 in a later check | 105-115ms |
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

The one failure after that fix was not a wrong answer. Every request in flight was slow at once, so no answer arrived for about 350ms while the goose fell from the middle of the gap into the bottom pipe. The model's last answer before the stall was correct. The bot has no fallback when every answer is late; adding one would make it more reliable, but the code rather than the model would be making that call. The final `END` line of each failure trace in `logs/` now records how many requests were still unanswered.

Other parts of the loop:

- **Overlapping requests.** A new decision starts every 50ms with up to 3 in flight, and only the newest answer acts. A single slow call (up to ~1.5s) therefore can't freeze control.
- **Predicting the latency.** The bot describes where the goose *will be* when a tap sent now lands, about 7 frames ahead, using the level's gravity (0.4 px/frame²).
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

## How we got there

1. Synthetic benchmarks (`benchmarks/`): fast (~110-130ms), but it couldn't place a bird relative to a gap.
2. Live, screenshots only: 0 taps, falls every time.
3. Live, game state as numbers: wrong flaps in both directions.
4. Live, game state as one sentence with an action rule: still wrong. The offline polarity test exposed the inversion.
5. Factual predicate: 7/10.
6. Tap-settle fix: 10/10.
