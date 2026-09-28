"""Generate a synthetic Lorcast-shaped cache (fake cards) to test the pipeline offline.

    python tools/make_fake_cache.py --cache /tmp/fakecache --count 600
"""

from __future__ import annotations

import argparse
import colorsys
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw

INKS = ["Amber", "Amethyst", "Emerald", "Ruby", "Sapphire", "Steel"]
INK_HUE = {"Amber": 0.10, "Amethyst": 0.78, "Emerald": 0.35, "Ruby": 0.99, "Sapphire": 0.58, "Steel": 0.55}


def fake_card_image(rng: random.Random, ink: str, size=(488, 681)) -> Image.Image:
    w, h = size
    hue = (INK_HUE[ink] + rng.uniform(-0.03, 0.03)) % 1.0
    sat = 0.15 if ink == "Steel" else rng.uniform(0.45, 0.85)
    frame = tuple(int(v * 255) for v in colorsys.hsv_to_rgb(hue, sat, rng.uniform(0.35, 0.8)))
    img = Image.new("RGB", size, frame)
    d = ImageDraw.Draw(img)
    # "art" area with a random gradient-ish block
    a_hue = rng.random()
    top = tuple(int(v * 255) for v in colorsys.hsv_to_rgb(a_hue, rng.uniform(0.2, 0.9), rng.uniform(0.3, 1.0)))
    bot = tuple(int(v * 255) for v in colorsys.hsv_to_rgb((a_hue + rng.uniform(0, 0.2)) % 1, 0.6, rng.uniform(0.1, 0.9)))
    x0, y0, x1, y1 = int(0.06 * w), int(0.05 * h), int(0.94 * w), int(0.55 * h)
    for i, y in enumerate(range(y0, y1)):
        t = i / max(1, y1 - y0 - 1)
        c = tuple(int(top[k] * (1 - t) + bot[k] * t) for k in range(3))
        d.line([(x0, y), (x1, y)], fill=c)
    d.rectangle([int(0.08 * w), int(0.6 * h), int(0.92 * w), int(0.9 * h)], fill=(235, 230, 220))
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, required=True)
    ap.add_argument("--count", type=int, default=400)
    ap.add_argument("--size", default="normal")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    img_dir = args.cache / "images" / args.size
    img_dir.mkdir(parents=True, exist_ok=True)

    cards, index = [], {}
    for i in range(args.count):
        ink = INKS[i % len(INKS)]
        cid = f"crd_fake{i:05d}"
        url = f"https://cards.lorcast.io/card/digital/{args.size}/{cid}.avif?1"
        p = img_dir / f"{cid}.avif"
        fake_card_image(rng, ink).save(p, quality=70)
        index[cid] = url
        cards.append({
            "id": cid,
            "name": f"Character {i % 97}",
            "version": f"Variant {i // 97}",
            "layout": "normal",
            "lang": "en",
            "ink": ink,
            "type": ["Character"],
            "rarity": rng.choice(["Common", "Uncommon", "Rare"]),
            "collector_number": str(i + 1),
            "set": {"id": "set_fake", "code": "F", "name": "Fake Set"},
            "image_uris": {"digital": {args.size: url}},
        })

    (args.cache).mkdir(parents=True, exist_ok=True)
    (args.cache / "cards.json").write_text(json.dumps(cards, indent=1))
    (img_dir / "index.json").write_text(json.dumps(index))
    print(f"wrote {len(cards)} fake cards to {args.cache}")


if __name__ == "__main__":
    main()
