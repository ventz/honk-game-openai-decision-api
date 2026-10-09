"""Latency + accuracy benchmark: can the Decisions API make a reflex call on a game frame?

Draws synthetic side-scroller frames (player on the left, an obstacle at a known
distance), asks gpt-6-luna "jump now?" via /v1/decisions, and reports round-trip
latency per frame size alongside whether the answer matched ground truth.
"""
import base64
import io
import statistics
import sys
import time

from openai import OpenAI
from PIL import Image, ImageDraw

client = OpenAI()

QUESTIONS = [
    {
        "type": "predicate",
        "name": "jump",
        "instructions": (
            "Side-scrolling game. The white square on the left is the player. "
            "Is the red obstacle close enough that the player must jump right now "
            "(the gap between them is smaller than the player's own width)?"
        ),
    }
]


def frame(width: int, gap_px: int) -> str:
    """Return a PNG data URL with the obstacle gap_px (in a 320-wide frame) from the player."""
    s = width / 320
    h = int(width * 9 / 16)
    img = Image.new("RGB", (width, h), "#1d2b53")
    d = ImageDraw.Draw(img)
    ground = int(h * 0.8)
    d.rectangle([0, ground, width, h], fill="#3d6b35")
    px0, pw = int(30 * s), int(24 * s)
    d.rectangle([px0, ground - pw, px0 + pw, ground], fill="white")
    ox = px0 + pw + int(gap_px * s)
    d.rectangle([ox, ground - int(20 * s), ox + int(16 * s), ground], fill="#ff004d")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def ask(url: str):
    t0 = time.perf_counter()
    dec = client.decisions.create(
        model="gpt-6-luna",
        input=[{"role": "user", "content": [{"type": "input_image", "image_url": url}]}],
        questions=QUESTIONS,
    )
    dt = time.perf_counter() - t0
    a = dec.answers[0]
    p = None if a.type == "refusal" else a.probability
    return dt, p, dec.usage.input_tokens


def main() -> None:
    # gap < 24px (player width) means "must jump"
    gaps = [8, 16, 60, 120, 200, 12, 90, 20]
    ask(frame(160, 100))  # warm the connection so TLS setup isn't counted
    for width in (160, 320, 640):
        times, correct, toks = [], 0, 0
        for gap in gaps:
            dt, p, t = ask(frame(width, gap))
            times.append(dt)
            toks += t
            truth = gap < 24
            ok = p is not None and (p >= 0.5) == truth
            correct += ok
            print(f"  w={width:3d} gap={gap:3d} truth={'JUMP' if truth else 'wait'} "
                  f"p={p if p is None else round(p, 2)} {dt * 1000:4.0f}ms {'ok' if ok else 'MISS'}")
        times.sort()
        print(f"w={width}: median {statistics.median(times) * 1000:.0f}ms, "
              f"min {times[0] * 1000:.0f}ms, max {times[-1] * 1000:.0f}ms, "
              f"{correct}/{len(gaps)} correct, ~{toks // len(gaps)} tokens/frame\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
