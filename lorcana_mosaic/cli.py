"""CLI:  python -m lorcana_mosaic fetch   /   build   /   info"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

from . import library as lib
from . import lorcast
from .mosaic import MosaicOptions, build_mosaic, plan_grid

DEFAULT_CACHE = Path.home() / ".cache" / "lorcana-mosaic"


def _add_filter_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--set", dest="sets", action="append", help="restrict to set code (repeatable), e.g. --set 1 --set 2")
    p.add_argument("--ink", action="append", help="Amber/Amethyst/Emerald/Ruby/Sapphire/Steel (repeatable)")
    p.add_argument("--rarity", action="append", help="Common/Uncommon/Rare/Super_rare/Legendary/Enchanted/Promo")
    p.add_argument("--type", dest="types", action="append", help="Character/Action/Item/Song/...")


def _load_cards(args) -> tuple[list[dict], dict[str, Path]]:
    cards = lorcast.fetch_catalog(args.cache, query=getattr(args, "query", None), force=False)
    cards = lib.filter_cards(
        cards,
        sets=args.sets,
        inks=args.ink,
        rarities=args.rarity,
        types=args.types,
    )
    paths = {}
    img_dir = lorcast.image_dir(args.cache, args.size)
    for c in cards:
        p = img_dir / f"{c['id']}.avif"
        if p.exists():
            paths[c["id"]] = p
    if not paths:
        sys.exit(f"No images cached in {img_dir}. Run:  python -m lorcana_mosaic fetch --size {args.size}")
    missing = len(cards) - len(paths)
    if missing:
        print(f"note: {missing} selected cards have no cached image (re-run fetch to get them)")
    return cards, paths


def cmd_fetch(args) -> None:
    cards = lorcast.fetch_catalog(args.cache, query=args.query, force=args.force)
    cards = lib.filter_cards(cards, sets=args.sets, inks=args.ink, rarities=args.rarity, types=args.types)
    print(f"fetch: {len(cards)} cards selected")
    lorcast.download_images(cards, args.cache, size=args.size, workers=args.workers)


def cmd_info(args) -> None:
    cards, paths = _load_cards(args)
    by_set: dict[str, int] = {}
    by_ink: dict[str, int] = {}
    for c in cards:
        if c["id"] not in paths:
            continue
        by_set[(c.get("set") or {}).get("code", "?")] = by_set.get((c.get("set") or {}).get("code", "?"), 0) + 1
        by_ink[c.get("ink") or "None"] = by_ink.get(c.get("ink") or "None", 0) + 1
    print(f"cached images: {len(paths)}  (size={args.size})")
    print("by set:", ", ".join(f"{k}={v}" for k, v in sorted(by_set.items())))
    print("by ink:", ", ".join(f"{k}={v}" for k, v in sorted(by_ink.items())))


def cmd_build(args) -> None:
    cards, paths = _load_cards(args)

    crop_box = tuple(args.crop_box) if args.crop_box else lib.CROP_PRESETS[args.crop]
    library = lib.build_library(
        cards,
        paths,
        crop_box=crop_box,
        grid=args.grid,
        cache_dir=args.cache,
        size=args.size,
        dedupe_art=args.dedupe_art,
    )
    print(f"library: {len(library)} usable tiles")

    target = Image.open(args.target).convert("RGB")

    opt = MosaicOptions(
        cols=args.cols,
        rows=args.rows,
        tile_width=args.tile_width,
        candidates=args.candidates,
        tolerance=args.tolerance,
        temperature=args.temperature,
        order=args.order,
        max_reuse=1 if args.unique else args.max_reuse,
        min_distance=args.min_distance,
        repel_by=args.repel_by,
        blend=args.blend,
        overlay=args.overlay,
        gap=args.gap,
        background=args.bg,
        seed=args.seed,
    )

    cols, rows, tw, th = plan_grid(target, library, opt)
    if args.unique and cols * rows > len(library):
        sys.exit(f"--unique needs {cols * rows} distinct cards but only {len(library)} are available; lower --cols")

    result = build_mosaic(target, library, opt)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    save_kwargs = {"quality": 92} if out.suffix.lower() in {".jpg", ".jpeg"} else {}
    result.image.save(out, **save_kwargs)

    print(f"\nsaved {out}  ({result.image.width}x{result.image.height}px)")
    s = result.stats
    print(f"cards placed      : {s['cells']}")
    print(f"distinct cards    : {s['distinct_cards_used']} / {min(s['cells'], s['library'])} "
          f"(variety {s['variety_ratio']:.0%})")
    print(f"max uses of a card: {s['max_uses']}")
    print(f"mean colour error : {s['mean_error']} (approx dE per cell)")
    if s["most_used"]:
        print("most used         : " + "; ".join(f"{n} x{k}" for n, k in s["most_used"]))

    if args.manifest:
        Path(args.manifest).write_text(json.dumps(result.manifest, indent=1))
        print(f"manifest          : {args.manifest}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="lorcana-mosaic", description="Photomosaics made of Disney Lorcana cards")
    p.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help=f"cache directory (default {DEFAULT_CACHE})")
    p.add_argument("--size", choices=lorcast.IMAGE_SIZES, default="normal", help="Lorcast image size to use")
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="download card data and images from Lorcast")
    f.add_argument("--query", help="Lorcast search syntax instead of all sets, e.g. 'set:1 ink:amber'")
    f.add_argument("--force", action="store_true", help="re-download the card catalog")
    f.add_argument("--workers", type=int, default=8)
    _add_filter_args(f)
    f.set_defaults(func=cmd_fetch)

    i = sub.add_parser("info", help="show what is in the local cache")
    _add_filter_args(i)
    i.set_defaults(func=cmd_info)

    b = sub.add_parser("build", help="build a mosaic")
    b.add_argument("target", help="source image")
    b.add_argument("-o", "--output", default="mosaic.jpg")
    b.add_argument("--cols", type=int, default=48, help="cards across (default 48)")
    b.add_argument("--rows", type=int, default=None, help="cards down (default: keep source aspect ratio)")
    b.add_argument("--tile-width", type=int, default=120, help="px width of each card in the output")
    b.add_argument("--crop", choices=sorted(lib.CROP_PRESETS), default="full",
                   help="'full' = whole card, 'art' = illustration only (closer to the photo)")
    b.add_argument("--crop-box", type=float, nargs=4, metavar=("L", "T", "R", "B"),
                   help="custom crop as fractions, overrides --crop")
    b.add_argument("--grid", type=int, default=3, help="NxN colour samples per card used for matching")
    # variety
    b.add_argument("--candidates", type=int, default=10, help="how many good matches to sample from")
    b.add_argument("--tolerance", type=float, default=5.0,
                   help="max colour cost (dE per cell) accepted for the sake of variety; 0 = fidelity first")
    b.add_argument("--temperature", type=float, default=3.0,
                   help="0 = always the best allowed match, higher = more varied")
    b.add_argument("--order", choices=("hard-first", "random"), default="hard-first",
                   help="which cells get first pick of scarce cards")
    b.add_argument("--max-reuse", type=int, default=None, help="max uses per card (0 = unlimited, default auto)")
    b.add_argument("--min-distance", type=int, default=3, help="no repeat within this many cells")
    b.add_argument("--repel-by", choices=("name", "card"), default="name",
                   help="'name' also spreads out different versions of the same character")
    b.add_argument("--unique", action="store_true", help="every card used at most once")
    b.add_argument("--dedupe-art", type=int, default=0, metavar="H",
                   help="drop near-identical artworks (dhash hamming <= H, try 2)")
    # look
    b.add_argument("--blend", type=float, default=0.0, help="0..1 flat colour correction per card")
    b.add_argument("--overlay", type=float, default=0.0, help="0..1 blend of the source image over the cards")
    b.add_argument("--gap", type=int, default=0, help="px gap between cards")
    b.add_argument("--bg", default="#101010", help="background colour behind the gaps")
    b.add_argument("--seed", type=int, default=None)
    b.add_argument("--manifest", help="write a JSON list of which card goes where")
    _add_filter_args(b)
    b.set_defaults(func=cmd_build)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
