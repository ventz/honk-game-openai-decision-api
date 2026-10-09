"""Flappy-style benchmark: is the bird below the next gap's center (i.e. should it flap)?

The question a Flappy bot asks every tick is vertical: bird above or below the gap
it is flying toward. Frames vary the bird's offset from the gap center; ground truth
is "flap" when the bird is below center.
"""
import base64
import io
import statistics
import time

from openai import OpenAI
from PIL import Image, ImageDraw

client = OpenAI()

QUESTIONS = [
    {
        "type": "predicate",
        "name": "flap",
        "instructions": (
            "Flappy Bird style game. The yellow circle is the bird; the green pipes have "
            "an opening between them. Is the bird's center lower on screen than the "
            "middle of the opening in the nearest pipe ahead of it?"
        ),
    }
]

W, H = 288, 384  # portrait, close to the game's aspect


def frame(offset: int) -> str:
    """offset > 0 puts the bird that many px below the gap center."""
    img = Image.new("RGB", (W, H), "#70c5ce")
    d = ImageDraw.Draw(img)
    gap_c, gap_h, px = 190, 110, 170
    d.rectangle([px, 0, px + 50, gap_c - gap_h // 2], fill="#2e8b2e")
    d.rectangle([px, gap_c + gap_h // 2, px + 50, H - 40], fill="#2e8b2e")
    d.rectangle([0, H - 40, W, H], fill="#ded895")
    by = gap_c + offset
    d.ellipse([60, by - 12, 84, by + 12], fill="#f8d33a", outline="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def main() -> None:
    offsets = [-120, -80, -50, -30, -15, 15, 30, 50, 80, 120]
    client.decisions.create(model="gpt-6-luna", input="warmup", questions=QUESTIONS)
    times, correct = [], 0
    for off in offsets:
        t0 = time.perf_counter()
        dec = client.decisions.create(
            model="gpt-6-luna",
            input=[{"role": "user", "content": [{"type": "input_image", "image_url": frame(off)}]}],
            questions=QUESTIONS,
        )
        dt = time.perf_counter() - t0
        times.append(dt)
        a = dec.answers[0]
        p = None if a.type == "refusal" else a.probability
        ok = p is not None and (p >= 0.5) == (off > 0)
        correct += ok
        print(f"offset={off:+4d} truth={'FLAP' if off > 0 else 'glide'} p={p} "
              f"{dt * 1000:4.0f}ms {'ok' if ok else 'MISS'}  ({dec.usage.input_tokens} tok)")
    print(f"median {statistics.median(times) * 1000:.0f}ms, {correct}/{len(offsets)} correct")


if __name__ == "__main__":
    main()
