"""Photomosaic assembly with explicit variety control.

The matching problem is "for every cell of the grid, pick the card that looks
most like it". Done naively that picks the same handful of cards over and over,
because a few cards happen to sit closest to the most common colours. Three
knobs fight that here:

1. ``max_reuse``      - hard cap on how many times one card may appear.
2. ``min_distance``   - a card (or a character name) may not reappear within
                        this Chebyshev radius of itself.
3. ``candidates`` + ``temperature`` - instead of always taking the single best
                        match, sample among the K best allowed ones.

Set temperature to 0 and min_distance to 0 and max_reuse to 0 to get the
classic, boring, "same card everywhere" behaviour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from .colors import image_to_lab
from .library import Library, _crop


@dataclass
class MosaicOptions:
    cols: int = 60
    rows: int | None = None
    tile_width: int = 120
    # variety
    candidates: int = 10
    tolerance: float = 5.0  # only swap in a different card if it costs <= this dE per cell
    temperature: float = 3.0  # softmax spread, in dE per cell (0 = always the best allowed)
    max_reuse: int | None = None  # None -> auto, 0 -> unlimited
    min_distance: int = 3
    repel_by: str = "name"  # "name" | "card"
    order: str = "hard-first"  # "hard-first" | "random"
    # fidelity
    blend: float = 0.0  # blend a flat target colour over the card
    overlay: float = 0.0  # blend the actual target crop over the card
    # layout
    gap: int = 0
    background: str = "#101010"
    seed: int | None = None


@dataclass
class MosaicResult:
    image: Image.Image
    manifest: list[dict]
    stats: dict = field(default_factory=dict)


def _target_features(target: Image.Image, cols: int, rows: int, grid: int) -> np.ndarray:
    """Average Lab colour of each sub-cell of each grid cell, flattened."""
    small = target.resize((cols * grid, rows * grid), Image.BOX)
    arr = np.asarray(small, dtype=np.uint8)
    lab = image_to_lab(arr)  # (rows*g, cols*g, 3)
    lab = lab.reshape(rows, grid, cols, grid, 3).transpose(0, 2, 1, 3, 4)
    return lab.reshape(rows * cols, grid * grid * 3).astype(np.float32)


def _top_candidates(tf: np.ndarray, lf: np.ndarray, keep: int, chunk: int = 512):
    """Per-tile shortlist of the `keep` closest cards. Returns (idx, dist)."""
    keep = int(min(keep, lf.shape[0]))
    n_tiles = tf.shape[0]
    idx_out = np.zeros((n_tiles, keep), dtype=np.int32)
    dst_out = np.zeros((n_tiles, keep), dtype=np.float32)

    lib_sq = (lf ** 2).sum(axis=1)[None, :]
    for start in range(0, n_tiles, chunk):
        block = tf[start : start + chunk]
        d = (block ** 2).sum(axis=1)[:, None] + lib_sq - 2.0 * (block @ lf.T)
        np.maximum(d, 0, out=d)
        part = np.argpartition(d, keep - 1, axis=1)[:, :keep] if keep < d.shape[1] else np.tile(
            np.arange(d.shape[1]), (d.shape[0], 1)
        )
        part_d = np.take_along_axis(d, part, axis=1)
        order = np.argsort(part_d, axis=1)
        idx_out[start : start + chunk] = np.take_along_axis(part, order, axis=1)
        dst_out[start : start + chunk] = np.sqrt(np.take_along_axis(part_d, order, axis=1))
    return idx_out, dst_out


def _auto_max_reuse(n_tiles: int, n_cards: int) -> int:
    return max(2, math.ceil(n_tiles / max(n_cards, 1) * 2))


def plan_grid(target: Image.Image, library: Library, opt: MosaicOptions) -> tuple[int, int, int, int]:
    tile_w = max(4, int(opt.tile_width))
    tile_h = max(4, int(round(tile_w * library.aspect)))
    cols = max(1, int(opt.cols))
    if opt.rows:
        rows = max(1, int(opt.rows))
    else:
        rows = max(1, round(cols * (target.height / target.width) * (tile_w / tile_h)))
    return cols, rows, tile_w, tile_h


def build_mosaic(
    target: Image.Image,
    library: Library,
    opt: MosaicOptions,
    log: Callable[[str], None] = print,
) -> MosaicResult:
    rng = np.random.default_rng(opt.seed)
    target = target.convert("RGB")
    cols, rows, tile_w, tile_h = plan_grid(target, library, opt)
    n_tiles = rows * cols
    n_cards = len(library)

    log(f"grid: {cols} x {rows} = {n_tiles} cards, tile {tile_w}x{tile_h}px, library {n_cards} cards")
    if n_tiles > n_cards:
        log(f"note: {n_tiles} cells vs {n_cards} distinct cards - reuse is unavoidable")

    tf = _target_features(target, cols, rows, library.grid)

    shortlist = max(opt.candidates * 12, 96)
    idx, dist = _top_candidates(tf, library.features, shortlist)

    max_reuse = opt.max_reuse if opt.max_reuse is not None else _auto_max_reuse(n_tiles, n_cards)
    if max_reuse and max_reuse * n_cards < n_tiles:
        needed = math.ceil(n_tiles / n_cards)
        log(f"variety: a cap of {max_reuse} cannot fill {n_tiles} cells with {n_cards} cards -> raising to {needed}")
        max_reuse = needed
    if max_reuse:
        log(f"variety: max {max_reuse} uses per card, no repeat within {opt.min_distance} cells "
            f"(by {opt.repel_by}), sampling up to {opt.candidates} matches within "
            f"{opt.tolerance} dE (T={opt.temperature})")

    groups = np.array(library.groups if opt.repel_by == "name" else library.ids)
    group_id = {g: i for i, g in enumerate(dict.fromkeys(groups.tolist()))}
    group_of = np.array([group_id[g] for g in groups.tolist()], dtype=np.int32)

    assigned = np.full((rows, cols), -1, dtype=np.int32)
    assigned_group = np.full((rows, cols), -1, dtype=np.int32)
    uses = np.zeros(n_cards, dtype=np.int32)
    errors = np.zeros(n_tiles, dtype=np.float32)

    # Cells with no good match at all should pick before easy cells do, otherwise
    # skies and walls eat the reuse budget of the few cards a dark eye needs.
    jitter = rng.random(n_tiles).astype(np.float32)
    if opt.order == "hard-first":
        order = np.argsort(-(dist[:, 0] + jitter))
    else:
        order = rng.permutation(n_tiles)

    tol = max(0.0, opt.tolerance) * library.grid
    temp = max(opt.temperature, 0.0) * library.grid
    d = max(0, int(opt.min_distance))
    relaxed = 0

    for t in order:
        r, c = divmod(int(t), cols)
        if d > 0:
            r0, r1 = max(0, r - d), min(rows, r + d + 1)
            c0, c1 = max(0, c - d), min(cols, c + d + 1)
            window = assigned_group[r0:r1, c0:c1]
            banned = set(window[window >= 0].tolist())
        else:
            banned = set()

        cand = idx[t]
        cand_d = dist[t]

        allowed: list[int] = []
        allowed_d: list[float] = []
        for j, card in enumerate(cand):
            card = int(card)
            if max_reuse and uses[card] >= max_reuse:
                continue
            if banned and int(group_of[card]) in banned:
                continue
            allowed.append(card)
            allowed_d.append(float(cand_d[j]))
            if len(allowed) >= opt.candidates:
                break

        if not allowed:
            # The shortlist is exhausted: search the whole library instead of
            # silently dumping the same card here over and over.
            relaxed += 1
            diff = library.features - tf[t]
            row = np.sqrt(np.einsum("ij,ij->i", diff, diff))
            cap_ok = uses < max_reuse if max_reuse else np.ones(n_cards, dtype=bool)
            near_ok = ~np.isin(group_of, np.fromiter(banned, dtype=np.int32, count=len(banned))) \
                if banned else np.ones(n_cards, dtype=bool)
            for mask in (cap_ok & near_ok, cap_ok, near_ok):
                if mask.any():
                    masked = np.where(mask, row, np.inf)
                    best = int(np.argmin(masked))
                    allowed, allowed_d = [best], [float(row[best])]
                    break
            else:
                # Every card is at its cap: fall back to the least used one.
                best = int(np.argmin(uses.astype(np.float64) * 1e6 + row))
                allowed, allowed_d = [best], [float(row[best])]

        # Variety is only free when several cards match about equally well, so
        # never consider a card that is more than `tolerance` worse than the
        # best one still available for this cell.
        if len(allowed) > 1 and tol > 0:
            limit = allowed_d[0] + tol
            cut = len(allowed)
            for j, dv in enumerate(allowed_d):
                if dv > limit:
                    cut = j
                    break
            allowed, allowed_d = allowed[:cut] or allowed[:1], allowed_d[:cut] or allowed_d[:1]

        if temp > 0 and len(allowed) > 1:
            a = np.asarray(allowed_d, dtype=np.float64)
            w = np.exp(-(a - a.min()) / temp)
            pick = int(rng.choice(len(allowed), p=w / w.sum()))
        else:
            pick = 0

        card = allowed[pick]
        assigned[r, c] = card
        assigned_group[r, c] = group_of[card]
        uses[card] += 1
        errors[t] = allowed_d[pick]

    if relaxed:
        log(f"variety: {relaxed} cells needed relaxed constraints (library too small for the rules)")

    image = _render(target, library, assigned, opt, tile_w, tile_h)

    manifest = [
        {
            "row": int(r),
            "col": int(c),
            "card_id": library.ids[int(assigned[r, c])],
            "label": library.labels[int(assigned[r, c])],
        }
        for r in range(rows)
        for c in range(cols)
    ]

    used = int((uses > 0).sum())
    top = np.argsort(-uses)[:5]
    stats = {
        "cols": cols,
        "rows": rows,
        "cells": n_tiles,
        "library": n_cards,
        "distinct_cards_used": used,
        "variety_ratio": round(used / min(n_tiles, n_cards), 3),
        "max_uses": int(uses.max()),
        "mean_error": round(float(errors.mean()) / library.grid, 2),
        "most_used": [(library.labels[int(i)], int(uses[int(i)])) for i in top if uses[int(i)] > 0],
        "relaxed_cells": relaxed,
    }
    return MosaicResult(image=image, manifest=manifest, stats=stats)


def _render(
    target: Image.Image,
    library: Library,
    assigned: np.ndarray,
    opt: MosaicOptions,
    tile_w: int,
    tile_h: int,
) -> Image.Image:
    rows, cols = assigned.shape
    gap = max(0, int(opt.gap))
    width = cols * tile_w + gap * (cols + 1)
    height = rows * tile_h + gap * (rows + 1)
    canvas = Image.new("RGB", (width, height), opt.background)

    over = target.resize((cols * tile_w, rows * tile_h), Image.LANCZOS) if opt.overlay > 0 else None
    flat = np.asarray(target.resize((cols, rows), Image.BOX), dtype=np.uint8) if opt.blend > 0 else None

    cache: dict[int, Image.Image] = {}

    def tile_for(card: int) -> Image.Image:
        if card not in cache:
            with Image.open(library.paths[card]) as im:
                im = _crop(im.convert("RGB"), library.crop_box)
                cache[card] = im.resize((tile_w, tile_h), Image.LANCZOS)
        return cache[card]

    for r in range(rows):
        for c in range(cols):
            card = int(assigned[r, c])
            tile = tile_for(card)
            if flat is not None:
                colour = tuple(int(v) for v in flat[r, c])
                tile = Image.blend(tile, Image.new("RGB", tile.size, colour), opt.blend)
            if over is not None:
                patch = over.crop((c * tile_w, r * tile_h, (c + 1) * tile_w, (r + 1) * tile_h))
                tile = Image.blend(tile, patch, opt.overlay)
            x = gap + c * (tile_w + gap)
            y = gap + r * (tile_h + gap)
            canvas.paste(tile, (x, y))
    return canvas
