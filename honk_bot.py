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
TICK_S = 0.05        # start a new decision this often
MAX_IN_FLIGHT = 3    # fresh (younger than ABANDON_S) requests allowed at once
ABANDON_S = 0.25     # a request unanswered this long stops blocking new ones; its answer is ignored
MAX_PENDING = 8      # hard ceiling including abandoned requests
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
    "instructions": "Will the goose be below the middle of the gap?",
}]

# Screen-space state: goose hitbox, velocity, dangerous obstacles, and a safe target y.
STATE_JS = """(look) => {
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
  for (const p of (g.pipeHandler.pipes || [])) {
    const s = p.sprite || p.container || p.topPipe; if (!s || !s.getBounds) continue;
    const r = s.getBounds();
    if (r.x + r.width < goose.left || r.x > goose.right + look) continue;
    obs.push([r.y, r.y + r.height]);
  }
  // Free vertical bands between obstacles; aim at the center of the band nearest the goose.
  obs.sort((a, z) => a[0] - z[0]);
  const bands = []; let cur = 0;
  for (const [t, bt] of obs) { if (t > cur) bands.push([cur, t]); cur = Math.max(cur, bt); }
  if (cur < H) bands.push([cur, H]);
  const h = goose.bottom - goose.top, gy = (goose.top + goose.bottom) / 2;
  const fit = bands.filter(([t, bt]) => bt - t > h * 1.4);
  let target = H * 0.45, band = [0, H];
  if (fit.length && obs.length) {
    fit.sort((a, z) => Math.abs((a[0]+a[1])/2 - gy) - Math.abs((z[0]+z[1])/2 - gy));
    band = fit[0];
    target = (band[0] + band[1]) / 2;
  }
  return {H: Math.round(H), y: Math.round(gy), vy: +g.bird.velocity.toFixed(2), target: Math.round(target), safeTop: Math.round(band[0] + h / 2), safeBottom: Math.round(band[1] - h / 2), n: obs.length,
          started: g.gameStarted, over: g.gameOver, dead: g.bird.isDead,
          done: !!(c && c.levelCompleted), loading: g.isLoading};
}"""

# On-page banner over the Reddit tab so a watcher can see what is driving and each decision.
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
LATENCY_FRAMES = 7   # ~120ms decision round-trip at 60fps


def predict_y(s: dict) -> int:
    """Screen y of the goose when a tap sent now would land."""
    t = LATENCY_FRAMES
    return round(s["y"] + s["vy"] * t + 0.5 * GRAVITY * t * t)


def describe(s: dict) -> str:
    """Where the goose will be, relative to the gap middle, when a tap sent now lands."""
    off = predict_y(s) - s["target"]  # screen y grows downward: positive = below the target
    return f"The goose will be {abs(off)} pixels {'below' if off > 0 else 'above'} the middle of the gap."


async def decide_state(s: dict) -> tuple[bool, float]:
    dec = await client.decisions.create(
        model=MODEL,
        input=describe(s),
        questions=STATE_Q,
    )
    a = dec.answers[0]
    p = 0.0 if a.type == "refusal" else a.probability
    return p >= 0.5, p


async def run(mode: str, seconds: float, quiet: bool = False) -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        frame = await find_game_frame(browser)
        box = await frame.locator("canvas").first.bounding_box()
        tap_xy = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        mouse = frame.page.mouse
        cdp = await frame.page.context.new_cdp_session(frame.page)
        grab = Grabber(cdp, box, VISION_WIDTH if mode == "vision" else None)
        title = f"OpenAI Decisions ({MODEL}) driving · {'screenshots' if mode == 'vision' else 'game state'}"

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
            await client.decisions.create(model=MODEL, input="warmup", questions=STATE_Q)
            # "Try Again" after a death, "Retry" after a completed level.
            retry = frame.get_by_role("button", name=re.compile(r"^(try again|retry)$", re.I))
            if await retry.count():
                await retry.first.click()
                await asyncio.sleep(1.5)
            await mouse.click(*tap_xy)  # first tap starts the run
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

        async def one(seq: int, s: dict, jpeg: bytes | None, t_obs: float) -> None:
            nonlocal last_applied, last_tap_at, taps, abandoned
            flap, conf = await (decide_vision(jpeg) if jpeg is not None else decide_state(s))
            lat_dec.append(time.perf_counter() - t_obs)
            # Drop answers older than the newest applied one, and answers from frames read before
            # the last tap had registered: acting on those caused double flaps into the top pipe.
            # Also drop answers that took longer than ABANDON_S: by then the frame they describe
            # is old, and fresher requests have already been sent in their place.
            too_old = time.perf_counter() - t_obs > ABANDON_S
            abandoned += too_old
            stale = seq <= last_applied or t_obs < last_tap_at + TAP_SETTLE_S or too_old
            if not stale:
                last_applied = seq
                if flap:
                    await mouse.click(*tap_xy)
                    taps += 1
                    last_tap_at = time.perf_counter()
            line = (f"{seq:3d} y={s['y']:4d} vy={s['vy']:6.2f} target={s['target']:4d} pred={predict_y(s):4d} "
                    f"band={s['safeTop']}-{s['safeBottom']} decide {lat_dec[-1]*1000:4.0f}ms "
                    f"{'TAP' if flap and not stale else ('tap' if flap else '   ')} {conf:.2f}"
                    f"{' (stale)' if stale else ''}")
            trace.append(line)
            if not stale:
                hud(f"{'FLAP' if flap else 'glide'} p={conf:.2f} · {lat_dec[-1]*1000:.0f}ms · taps {taps}")
            if not quiet:
                print(line)

        while time.perf_counter() < end:
            t0 = time.perf_counter()
            s = await frame.evaluate(STATE_JS, LOOKAHEAD)
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
        result = {"trace": trace, "abandoned": abandoned, "cleared_pipe": cleared, "completed": bool(s.get("done")), "taps": taps,
                  "decide_ms": round(statistics.median(lat_dec) * 1000) if lat_dec else None}
        print(f"{mode}: {n} ticks, {taps} taps, observe median {statistics.median(lat_obs)*1000:.0f}ms"
              + (f", decide median {result['decide_ms']}ms" if lat_dec else "")
              + f" | late answers dropped: {abandoned}"
              + f" | cleared pipe: {cleared}, completed: {result['completed']}")
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
    print(f"\n{mode}: cleared the pipe {cleared}/{runs}, completed {done}/{runs}, "
          f"median decide {statistics.median(lat) if lat else '-'}ms")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Play Honk with OpenAI's Decisions API.")
    ap.add_argument("mode", choices=["state", "vision", "capture"])
    ap.add_argument("--seconds", type=float, default=20, help="max length of each run")
    ap.add_argument("--runs", type=int, default=1, help="play this many runs and report a success rate")
    ap.add_argument("--record", type=Path, help="save an MP4 of the Reddit tab, e.g. videos/state.mp4")
    args = ap.parse_args()
    if args.runs > 1 or args.record:
        asyncio.run(batch(args.mode, args.runs, args.seconds, args.record))
    else:
        asyncio.run(run(args.mode, args.seconds))
