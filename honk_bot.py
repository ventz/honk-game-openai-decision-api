"""Play Reddit's Honk (the flappy-goose Devvit game) with OpenAI's Decisions API.

Attaches to a Chrome started with remote debugging, finds the game's devvit.net
iframe on the open Reddit tab, and runs an observe -> decide -> tap loop.

Modes:
  state    read goose/obstacle positions from the game, ask /v1/decisions as text
  vision   send each frame to /v1/decisions as an image
  capture  save frames to ./frames, no input sent
"""
import argparse
import asyncio
import base64
import io
import os
import re
import statistics
import time
from pathlib import Path

from openai import AsyncOpenAI
from PIL import Image
from playwright.async_api import Frame, async_playwright

CDP_URL = os.environ.get("HONK_CDP_URL", "http://127.0.0.1:9222")
FRAME_HOST = "webview.devvit.net"
MODEL = "gpt-6-luna"
LOOKAHEAD = 260      # px ahead of the goose that counts as "the obstacle coming up"
FLAP_RISE = 52       # px a flap lifts the goose (median of 61 flaps measured from run traces)
AIM_BIAS = 10        # px above center: late flaps (falling out the bottom) are the commoner death
AIM = -1.0           # fixed aim as a fraction of the safe band (0 top, 1 bottom); < 0 = derive from FLAP_RISE
PASS_PX = 30         # keep watching a pipe until it is this far behind the goose (--pass-px 4
                     # switches sooner; not yet shown to help)
LEAN = -1            # px margin when leaning toward the next gap (see STATE_JS); < 0 disables.
                     # --lean 12 tied the default at ~155ms latency; untested beyond that
TICK_S = 0.025       # start a new decision this often (50ms skipped fatal moments in endless mode)
MAX_IN_FLIGHT = 8    # fresh (younger than ABANDON_S) requests allowed at once; must cover
                     # latency / TICK_S, or ticks get skipped exactly when a flap is due
ABANDON_S = 0.25     # a request unanswered this long stops blocking new ones; its answer is ignored
MAX_PENDING = 16     # hard ceiling including abandoned requests
TAP_SETTLE_S = 0.06  # a tap needs a few frames before the game state reflects it

VISION_Q = [{
    "type": "choice",
    "name": "pos",
    "instructions": "Flappy Bird style game. Compare the goose's height with the middle of the "
                    "opening between the green pipes directly ahead of it.",
    "choices": [
        {"value": "above", "description": "The goose is higher than the middle of the opening."},
        {"value": "below", "description": "The goose is lower than the middle of the opening."},
    ],
}]

# Ask a fact, not an action: "flap if below, don't if above" came back inverted (p=0.99 to
# flap when above), while the plain factual predicate scored 1.00 / 0.00 on the same inputs.
STATE_Q = [{
    "type": "predicate",
    "name": "below",
    "instructions": "Will the goose be below the target height?",
}]

# Screen-space state: goose hitbox, velocity, dangerous obstacles, and a safe target y.
STATE_JS = """([look, RISE, PASS_PX, BIAS, AIM, LEAN]) => {
  const g = window.gameInstance, c = g.customLevelHandler;
  const b = g.bird.hitbox.getBounds();
  const goose = {top: b.y, bottom: b.y + b.height, left: b.x, right: b.x + b.width};
  const H = g.app.screen.height;
  const obs = [];
  const objs = (c && c.isCustomLevel) ? c.genericObjects.filter(o => o.dangerous) : [];
  for (const o of objs) {
    const s = o.sprite || o.container; if (!s) continue;
    const r = s.getBounds();
    if (r.x + r.width < goose.left || r.x > goose.right + look) continue;
    obs.push([r.y, r.y + r.height]);
  }
  // Endless mode: steer by exactly one pipe pair, the nearest one not yet PASS_PX behind the
  // goose. Mixing in the next pipe while still inside this one can leave no common gap at all,
  // and dropping this one as soon as the goose's left edge clears it clips its lip on the way out.
  // Its top/bottom hitboxes are separate (the container spans the gap too). The next pipe may not
  // have spawned yet; the caller then holds the last gap's height.
  // Copy numbers out of getBounds() right away: Pixi may reuse the returned object, so holding a
  // reference while measuring the next pipe silently overwrote the "best" pipe.
  const box = o => { const r = o.getBounds(); return {x: r.x, y: r.y, w: r.width, h: r.height}; };
  // Later in a run endless mode switches to single pipes: only a top half or only a bottom half
  // (spawnTop), many of them moving, so take whichever halves exist.
  let gapId = null;
  const ahead = [];
  for (const p of (g.pipeHandler.pipes || [])) {
    const half = o => (o && o.getBounds) ? box(o) : null;
    const top = half(p.topHitbox), bot = half(p.bottomHitbox);
    const parts = [top, bot].filter(r => r && r.w >= 1);
    if (!parts.length) continue;
    const x = Math.min(...parts.map(r => r.x)), right = Math.max(...parts.map(r => r.x + r.w));
    if (right < goose.left - PASS_PX) continue;
    ahead.push({x, parts, top: top && top.w >= 1 ? top : null, bot: bot && bot.w >= 1 ? bot : null, id: p.initialX});
  }
  ahead.sort((a, z) => a.x - z.x);
  const best = ahead[0], next = ahead[1];
  if (best) {
    for (const r of best.parts) obs.push([r.y, r.y + r.h]);
    gapId = best.id;
  }
  // The ground kills too: treat its top edge as the bottom of the playable area.
  const fl = g.floorHandler;
  const floorTop = (fl && fl.floor && !fl.isFloorDisabled) ? Math.min(H, fl.floor.getBounds().y) : H;
  // Free vertical bands between obstacles; aim at the band nearest the goose.
  obs.sort((a, z) => a[0] - z[0]);
  const bands = []; let cur = 0;
  for (const [t, bt] of obs) { if (t > cur) bands.push([cur, Math.min(t, floorTop)]); cur = Math.max(cur, bt); }
  if (cur < floorTop) bands.push([cur, floorTop]);
  const h = goose.bottom - goose.top, gy = (goose.top + goose.bottom) / 2;
  const fit = bands.filter(([t, bt]) => bt - t > h * 1.4);
  let target = floorTop * 0.5, band = [0, floorTop];
  if (fit.length && obs.length) {
    fit.sort((a, z) => Math.abs((a[0]+a[1])/2 - gy) - Math.abs((z[0]+z[1])/2 - gy));
    band = fit[0];
    // The goose oscillates between the flap point (its lowest) and RISE px above it. Put the flap
    // point so that oscillation is centered in the band: target = middle + RISE/2. In endless
    // mode (~114px of room) that leaves ~31px either side; aiming at 55% left only ~11px above.
    const lo = band[0] + h / 2, hi = band[1] - h / 2;
    target = AIM >= 0 ? lo + (hi - lo) * AIM : Math.min(hi, (lo + hi) / 2 + RISE / 2 - BIAS);
    // Pipes come every ~45 frames of open air, and the next gap can be 300px away: too far to
    // cover from a standing start. So lean toward the next gap while still in this one, keeping
    // LEAN px clear of the top lip after a flap's rise, and 2.5x that above the bottom lip: a tap
    // lands up to a tick (~8px) late on top of latency jitter, and 12px there sank into the lip.
    if (next && LEAN >= 0) {
      const nlo = (next.top ? next.top.y + next.top.h : 0) + h / 2;
      const nhi = (next.bot ? Math.min(next.bot.y, floorTop) : floorTop) - h / 2;
      const ntarget = Math.min(nhi, (nlo + nhi) / 2 + RISE / 2 - BIAS);
      target = Math.max(Math.min(ntarget, Math.max(target, hi - LEAN * 2.5)), Math.min(target, lo + RISE + LEAN));
    }
  }
  return {H: Math.round(H), y: Math.round(gy), vy: +g.bird.velocity.toFixed(2), target: Math.round(target), safeTop: Math.round(band[0] + h / 2), safeBottom: Math.round(band[1] - h / 2), n: obs.length, gapId: gapId,
          score: g.pipeHandler.score, endless: !(c && c.isCustomLevel),
          started: g.gameStarted, over: g.gameOver, dead: g.bird.isDead,
          done: !!(c && c.levelCompleted), loading: g.isLoading};
}"""

# On-page banner over the Reddit tab so a watcher can see what is flying the goose and each decision.
HUD_JS = """(t) => { let d = document.getElementById('__honk_hud');
  if (!d) { d = document.createElement('div'); d.id = '__honk_hud';
    d.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#111;color:#ff0;' +
      'font:bold 22px monospace;padding:8px;text-align:center;pointer-events:none';
    document.body.appendChild(d); }
  d.textContent = t; }"""

client = AsyncOpenAI()


async def find_game_frame(browser) -> Frame:
    # Frames of an already-open page report blank URLs over CDP, so ask each one where it is.
    for ctx in browser.contexts:
        for page in ctx.pages:
            for frame in page.frames:
                try:
                    host = await asyncio.wait_for(frame.evaluate("location.host"), 2)
                except Exception:
                    continue
                if FRAME_HOST in host and await frame.locator("canvas").count():
                    return frame
    raise SystemExit(f"No {FRAME_HOST} game canvas found: open the honk post and press Play first.")


VISION_WIDTH = 360  # full 1448px frames took ~550ms per decision; small ones ~120ms


class Grabber:
    """Canvas screenshots via raw CDP Page.captureScreenshot, clipped to the canvas.

    Always captured at scale 1 and shrunk in Python: a clip with scale != 1 (or Playwright's
    scale="css") makes Chrome re-render the page per capture, which shows as visible flashing.
    """

    def __init__(self, cdp, box: dict, width: int | None):
        self.cdp = cdp
        self.width = width
        self.clip = {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"], "scale": 1}

    async def __call__(self) -> bytes:
        r = await self.cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 70, "clip": self.clip})
        raw = base64.b64decode(r["data"])
        if not self.width:
            return raw
        img = Image.open(io.BytesIO(raw))
        img = img.resize((self.width, round(img.height * self.width / img.width)))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return buf.getvalue()


async def decide_vision(jpeg: bytes) -> tuple[bool, float]:
    url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    dec = await client.decisions.create(
        model=MODEL,
        input=[{"role": "user", "content": [{"type": "input_image", "image_url": url}]}],
        questions=VISION_Q,
    )
    a = dec.answers[0]
    if a.type != "choice":
        return False, 0.0
    return a.choice == "below", a.confidence


GRAVITY = 0.4        # px/frame^2, from the level data
FPS = 60
FAST_FALL = 6.0       # px/frame; beyond this, predict one extra tick ahead
MAX_GAP_SPEED = 3.0   # px/frame cap on the estimated gap motion; larger values are target switches
latency_frames = 7.0  # decision round-trip in frames; updated from measured latency during a run


PLAN_SLOTS = 1        # moments asked about per request; > 1 turns each answer into a short climb plan
SLOT_NAMES = "ABCDEF"
FLAP_V = 6.4          # px/frame upward right after a flap: a flap sets the velocity, and v^2/2g = FLAP_RISE
PLAN_GAP = 8          # frames between the flaps of a climb plan (~133ms, under one decision round-trip)


def predict_y(s: dict, extra: float = 0.0) -> int:
    """Screen y of the goose when a tap sent now (plus `extra` frames) would land.

    When falling fast, also count the wait until the next decision (one tick): skipping a flap now
    means the next chance comes ~3 frames later, ~30px at terminal speed.
    """
    t = latency_frames + extra + (TICK_S * FPS if s["vy"] > FAST_FALL else 0)
    return round(s["y"] + s["vy"] * t + 0.5 * GRAVITY * t * t)


def predict_target(s: dict, extra: float = 0.0) -> int:
    """Gap middle when a tap sent now (plus `extra` frames) would land; moving pipes drift."""
    return round(s["target"] + s.get("target_v", 0.0) * (latency_frames + extra))


def plan_questions(slots: int) -> list[dict]:
    if slots == 1:
        return STATE_Q
    return [{"type": "predicate", "name": f"below_{k}",
             "instructions": f"Will the goose be below the target height at moment {SLOT_NAMES[k]}?"}
            for k in range(slots)]


def climb_offsets(s: dict) -> list[int]:
    """Offset from the target (positive = below) at moments A, B, C, ... PLAN_GAP frames apart,
    each assuming the goose flapped at every earlier moment.

    Climbing to a much higher gap was the commonest endless death: an answer read before the last
    tap registered has to be dropped, which capped the bot at one flap per round-trip (~210ms),
    about 3px of climb per frame. A plan lets one answer chain flaps every PLAN_GAP frames instead.
    Near the target it reduces to one flap, since the moment after a flap is already above it.
    """
    y = predict_y(s)
    offs = [round(y - predict_target(s))]
    for k in range(1, PLAN_SLOTS):
        y += -FLAP_V * PLAN_GAP + 0.5 * GRAVITY * PLAN_GAP ** 2  # a flap sets the velocity
        offs.append(round(y - predict_target(s, k * PLAN_GAP)))
    return offs


def describe(s: dict) -> str:
    """Where the goose will be, relative to the target, when a tap sent now lands.

    With PLAN_SLOTS > 1 it describes a short climb: see climb_offsets.
    """
    # screen y grows downward: positive offset = below the target
    offs = [s["pred"] - s["tgt_pred"]] if PLAN_SLOTS == 1 else climb_offsets(s)
    if PLAN_SLOTS == 1:
        # Signed form scored a clean 1.00 / 0.00 even at 1px, slightly crisper than "N pixels below".
        return f"Goose height relative to target: {-offs[0]:+d} px (negative means below)."
    # One signed line per moment scored 20/20 offline; a single run-on sentence left moment A soft
    # near zero (p=0.44 at -3 px) and later moments mushy.
    return ("\n".join(f"Moment {SLOT_NAMES[k]}: goose height relative to target: {-o:+d} px (negative means below)."
                      for k, o in enumerate(offs))
            + "\nEach moment after A assumes a flap at every earlier moment.")


async def decide_state(s: dict) -> list[float]:
    """Probability that the goose will be below the target, per planned moment."""
    dec = await client.decisions.create(model=MODEL, input=s["text"], questions=plan_questions(PLAN_SLOTS))
    by_name = {a.name: (0.0 if a.type == "refusal" else a.probability) for a in dec.answers}
    names = [q["name"] for q in plan_questions(PLAN_SLOTS)]
    return [by_name.get(n, 0.0) for n in names]


async def run(mode: str, seconds: float, quiet: bool = False) -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        frame = await find_game_frame(browser)
        box = await frame.locator("canvas").first.bounding_box()
        tap_xy = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        mouse = frame.page.mouse
        cdp = await frame.page.context.new_cdp_session(frame.page)
        grab = Grabber(cdp, box, VISION_WIDTH if mode == "vision" else None)
        title = f"OpenAI Decisions ({MODEL}) flying · {'screenshots' if mode == 'vision' else 'game state'}"

        hud_tasks: set[asyncio.Task] = set()

        async def _hud(text: str) -> None:
            try:
                await frame.page.evaluate(HUD_JS, f"{title} · {text}")
            except Exception:
                pass  # the banner is cosmetic; never let it break a run

        def hud(text: str) -> None:
            # fire-and-forget so the banner never adds latency to the control loop
            t = asyncio.create_task(_hud(text))
            hud_tasks.add(t)
            t.add_done_callback(hud_tasks.discard)

        hud("starting")
        lat_obs, lat_dec, taps, n = [], [], 0, 0
        if mode != "capture":
            # Warm the HTTPS connection: a cold first call takes ~750ms, long enough to fall into a pipe.
            # Warm several connections at once: with requests overlapping, a cold one costs a TLS
            # handshake mid-run, which looks exactly like a latency stall.
            await asyncio.gather(*[client.decisions.create(model=MODEL, input="warmup", questions=plan_questions(PLAN_SLOTS))
                                   for _ in range(MAX_PENDING)])
            # "Try Again" after a death, "Retry" after a completed level.
            retry = frame.get_by_role("button", name=re.compile(r"^(try again|retry)$", re.I))
            if await retry.count():
                await retry.first.click()
                await asyncio.sleep(1.5)
            # Tap until the game reports a live run: endless mode's "TAP to Retry" screen sometimes
            # swallowed the start tap, leaving the goose hovering with nothing to decide.
            for _ in range(6):
                await mouse.click(*tap_xy)
                await asyncio.sleep(0.4)
                st = await frame.evaluate("() => [gameInstance.gameStarted, gameInstance.gameOver]")
                if st[0] and not st[1]:
                    break
                await asyncio.sleep(0.8)
        end = time.perf_counter() + seconds
        if mode == "capture":
            out = Path("frames")
            out.mkdir(exist_ok=True)
            while time.perf_counter() < end:
                n += 1
                (out / f"{n:05d}.jpg").write_bytes(await grab())
                await asyncio.sleep(0.1)
            print(f"captured {n} frames to {out}/")
            return {"trace": [], "cleared_pipe": False, "completed": False, "taps": 0, "decide_ms": None}

        # Overlapping decisions: a new one every TICK_S with up to MAX_IN_FLIGHT pending, so a
        # single slow call (seen up to 1.5s) no longer freezes control. Only the freshest answer
        # acts, and answers observed before the last tap are dropped (they don't know about it).
        in_flight: dict[asyncio.Task, float] = {}  # task -> perf_counter when it was sent
        abandoned = 0
        seen_obstacle = cleared = False
        trace: list[str] = []
        s = {}
        last_applied = 0
        last_tap_at = 0.0  # perf_counter when the last tap was sent
        planned = 0        # taps made from a climb plan rather than the newest answer
        plan = None        # (seq, perf_counter of its first tap) of the climb plan still running

        async def tap() -> None:
            nonlocal taps, last_tap_at
            await mouse.click(*tap_xy)
            taps += 1
            last_tap_at = time.perf_counter()

        async def planned_tap(seq: int, delay: float) -> None:
            nonlocal planned
            await asyncio.sleep(delay)
            if plan and plan[0] == seq and time.perf_counter() > last_tap_at + TAP_SETTLE_S:
                planned += 1
                try:
                    await tap()
                except Exception:
                    pass  # the run ended (page closed) while this tap was scheduled

        async def one(seq: int, s: dict, jpeg: bytes | None, t_obs: float) -> None:
            nonlocal last_applied, abandoned, plan
            if jpeg is not None:
                flap, conf = await decide_vision(jpeg)
                probs = [1.0 if flap else 0.0]
            else:
                probs = await decide_state(s)
                flap, conf = probs[0] >= 0.5, probs[0]
            lat_dec.append(time.perf_counter() - t_obs)
            # Predict ahead by what decisions actually take right now, plus a frame for the tap.
            global latency_frames
            latency_frames = statistics.median(lat_dec[-15:]) * FPS + 1
            # Drop answers older than the newest applied one, and answers from frames read before
            # the last tap had registered: acting on those caused double flaps into the top pipe.
            # Also drop answers that took longer than ABANDON_S: by then the frame they describe
            # is old, and fresher requests have already been sent in their place.
            too_old = time.perf_counter() - t_obs > ABANDON_S
            abandoned += too_old
            stale = seq <= last_applied or t_obs < last_tap_at + TAP_SETTLE_S or too_old
            # A timely "not below" from a frame read after the plan's first tap cancels the plan, even
            # when it's too close to a planned tap to act on. Otherwise each planned tap marked every
            # newer answer stale, nothing could cancel the plan, and it flew the goose into the top
            # pipe. A "below" agrees with the plan, so it keeps running.
            if plan and not flap and not too_old and t_obs > plan[1] + TAP_SETTLE_S:
                plan = None
            if not stale:
                last_applied = seq
                if flap:
                    await tap()
                    # Climb plan: keep flapping every PLAN_GAP frames while each moment is still
                    # below; a fresher answer cancels the rest.
                    plan = (seq, last_tap_at)
                    for k, p_k in enumerate(probs[1:], 1):
                        if p_k < 0.5:
                            break
                        asyncio.create_task(planned_tap(seq, k * PLAN_GAP / FPS))
            line = (f"{seq:3d} y={s['y']:4d} vy={s['vy']:6.2f} target={s['target']:4d} pred={s['pred']:4d} tgt_pred={s['tgt_pred']:4d} "
                    f"band={s['safeTop']}-{s['safeBottom']} decide {lat_dec[-1]*1000:4.0f}ms "
                    f"{'TAP' if flap and not stale else ('tap' if flap else '   ')} {conf:.2f}"
                    f"{' (stale)' if stale else ''}  | {s.get("text", "").replace(chr(10), " ")}")
            trace.append(line)
            if not stale:
                hud(f"{'FLAP' if flap else 'glide'} p={conf:.2f} · {lat_dec[-1]*1000:.0f}ms · taps {taps}")
            if not quiet:
                print(line)

        prev_gap = None  # (gapId, target, time) of the previous observation
        while time.perf_counter() < end:
            t0 = time.perf_counter()
            s = await frame.evaluate(STATE_JS, [LOOKAHEAD, FLAP_RISE, PASS_PX, AIM_BIAS, AIM, LEAN])
            # Between pipes (the next one may not have spawned yet) hold the last gap's height
            # instead of drifting to mid-screen and arriving out of position.
            if s["n"] == 0 and prev_gap and prev_gap[1] is not None:
                s["target"] = prev_gap[1]
            # Vertical speed of the gap we're aiming at, in px/frame, for moving pipes.
            s["target_v"] = 0.0
            if prev_gap and s["gapId"] is not None and prev_gap[0] == s["gapId"] and t0 > prev_gap[2]:
                v = (s["target"] - prev_gap[1]) / ((t0 - prev_gap[2]) * FPS)
                s["target_v"] = max(-MAX_GAP_SPEED, min(MAX_GAP_SPEED, v))
            prev_gap = (s["gapId"], s["target"], t0)
            s["pred"], s["tgt_pred"] = predict_y(s), predict_target(s)
            s["text"] = describe(s)  # frozen now, so the log shows exactly what the model was asked
            jpeg = await grab() if mode == "vision" else None
            lat_obs.append(time.perf_counter() - t0)
            n += 1
            seen_obstacle = seen_obstacle or s["n"] > 0
            cleared = cleared or (seen_obstacle and s["n"] == 0)
            if s["dead"] or s["over"] or s["done"]:
                trace.append(f"END y={s['y']} vy={s['vy']} band={s['safeTop']}-{s['safeBottom']} "
                             f"dead={s['dead']} done={s['done']} unanswered={len(in_flight)}")
                break
            # Count only requests that are still young: when every request stalls at once (seen as
            # ~350ms with no answer), abandoned ones must not block fresh questions.
            fresh = sum(1 for sent in in_flight.values() if t0 - sent < ABANDON_S)
            if fresh < MAX_IN_FLIGHT and len(in_flight) < MAX_PENDING:
                task = asyncio.create_task(one(n, s, jpeg, t0))
                in_flight[task] = t0
                task.add_done_callback(lambda t: in_flight.pop(t, None))
            await asyncio.sleep(max(0.0, TICK_S - (time.perf_counter() - t0)))
        for task in list(in_flight):
            task.cancel()
        hud(f"{'LEVEL COMPLETE' if s.get('done') else 'crashed'} · {taps} taps")
        await asyncio.gather(*hud_tasks, return_exceptions=True)
        result = {"trace": trace, "abandoned": abandoned, "score": s.get("score", 0), "cleared_pipe": cleared, "completed": bool(s.get("done")), "taps": taps,
                  "decide_ms": round(statistics.median(lat_dec) * 1000) if lat_dec else None}
        print(f"{mode}: {n} ticks, {taps} taps, observe median {statistics.median(lat_obs)*1000:.0f}ms"
              + (f", decide median {result['decide_ms']}ms" if lat_dec else "")
              + f" | late answers dropped: {abandoned}"
              + (f" | planned taps: {planned}" if PLAN_SLOTS > 1 else "")
              + (f" | score: {result['score']}" if s.get("endless") else
                 f" | cleared pipe: {cleared}, completed: {result['completed']}"))
        return result


class Recorder:
    """Records the Reddit tab to MP4 via CDP Page.startScreencast (no OS screen-recording permission).

    Frames arrive with their own timestamps; ffmpeg's concat demuxer replays them at real speed.
    No maxWidth/maxHeight: asking Chrome to scale (as with scaled screenshots) is what caused flashing.
    """

    def __init__(self, out: Path):
        self.out = out
        self.dir = out.with_suffix("")
        self.frames: list[tuple[Path, float]] = []

    async def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        browser = await self._pw.chromium.connect_over_cdp(CDP_URL)
        page = [pg for c in browser.contexts for pg in c.pages if "reddit" in pg.url][0]
        self.cdp = await page.context.new_cdp_session(page)
        self.cdp.on("Page.screencastFrame", self._on_frame)
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 80, "everyNthFrame": 1})

    def _on_frame(self, ev: dict) -> None:
        path = self.dir / f"{len(self.frames):06d}.jpg"
        path.write_bytes(base64.b64decode(ev["data"]))
        self.frames.append((path, ev["metadata"]["timestamp"]))
        asyncio.create_task(self.cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]}))

    async def stop(self) -> None:
        await self.cdp.send("Page.stopScreencast")
        await asyncio.sleep(0.3)
        await self._pw.stop()
        if len(self.frames) < 2:
            print("recorder: no frames captured")
            return
        lines = []
        for (path, ts), (_, nxt) in zip(self.frames, self.frames[1:]):
            lines += [f"file '{path.name}'", f"duration {max(nxt - ts, 0.001):.4f}"]
        lines.append(f"file '{self.frames[-1][0].name}'")
        (self.dir / "frames.txt").write_text("\n".join(lines) + "\n")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "frames.txt",
            "-vf", "scale=1920:-2:flags=lanczos,fps=30", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-crf", "20", "-movflags", "+faststart", str(self.out.resolve()), cwd=self.dir)
        await proc.wait()
        print(f"recorder: {len(self.frames)} frames -> {self.out}")


async def batch(mode: str, runs: int, seconds: float, record: Path | None = None) -> None:
    recorder = Recorder(record) if record else None
    if recorder:
        await recorder.start()
    results = []
    for i in range(runs):
        print(f"run {i + 1}/{runs}: ", end="", flush=True)
        results.append(await run(mode, seconds, quiet=True))
        await asyncio.sleep(1.0)
    if recorder:
        await recorder.stop()
    logs = Path("logs")
    logs.mkdir(exist_ok=True)
    for i, r in enumerate(results, 1):
        if not r["completed"]:
            (logs / f"{mode}-fail-{i:02d}.txt").write_text("\n".join(r["trace"]) + "\n")
    cleared = sum(r["cleared_pipe"] for r in results)
    done = sum(r["completed"] for r in results)
    lat = [r["decide_ms"] for r in results if r["decide_ms"]]
    scores = [r["score"] for r in results]
    print(f"\n{mode}: cleared the pipe {cleared}/{runs}, completed {done}/{runs}, "
          f"scores {scores} (best {max(scores)}), median decide {statistics.median(lat) if lat else '-'}ms")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Play Honk with OpenAI's Decisions API.")
    ap.add_argument("mode", choices=["state", "vision", "capture"])
    ap.add_argument("--seconds", type=float, default=120, help="max length of each run")
    ap.add_argument("--runs", type=int, default=1, help="play this many runs and report a success rate")
    ap.add_argument("--record", type=Path, help="save an MP4 of the Reddit tab, e.g. videos/state.mp4")
    ap.add_argument("--aim", type=float, help="fixed aim in the safe band, 0 = top edge, 1 = bottom edge")
    ap.add_argument("--tick", type=float, help=f"seconds between decisions (default {TICK_S})")
    ap.add_argument("--in-flight", type=int, help=f"fresh requests allowed at once (default {MAX_IN_FLIGHT})")
    ap.add_argument("--lean", type=int, help=f"px margin when leaning toward the next gap, < 0 disables (default {LEAN})")
    ap.add_argument("--pass-px", type=int, help=f"px past a pipe before switching to the next (default {PASS_PX})")
    ap.add_argument("--plan", type=int, help="moments asked about per request (default 1); > 1 asks about a short climb of flaps")
    args = ap.parse_args()
    if args.aim is not None:
        AIM = args.aim
    if args.tick:
        TICK_S = args.tick
    if args.in_flight:
        MAX_IN_FLIGHT = args.in_flight
    if args.lean is not None:
        LEAN = args.lean
    if args.pass_px is not None:
        PASS_PX = args.pass_px
    if args.plan:
        PLAN_SLOTS = min(args.plan, len(SLOT_NAMES))
    if args.runs > 1 or args.record:
        asyncio.run(batch(args.mode, args.runs, args.seconds, args.record))
    else:
        asyncio.run(run(args.mode, args.seconds))
