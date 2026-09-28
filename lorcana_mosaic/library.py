"""Turn cached Lorcana card images into a searchable tile library."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np
from PIL import Image, features

from .colors import image_to_lab

# Crop presets, as (left, top, right, bottom) fractions of the card image.
# "full" keeps the whole card (the mosaic reads as a wall of cards).
# "art"  keeps only the illustration (the mosaic reads much more like a photo).
CROP_PRESETS: dict[str, tuple[float, float, float, float]] = {
    "full": (0.0, 0.0, 1.0, 1.0),
    "art": (0.06, 0.05, 0.94, 0.55),
    "art-wide": (0.04, 0.04, 0.96, 0.62),
}


def check_avif() -> None:
    if not features.check("avif"):
        raise RuntimeError(
            "This Pillow build cannot read AVIF, which is the format Lorcast serves.\n"
            "Fix with either:  pip install -U 'pillow>=11.3'   or   pip install pillow-avif-plugin"
        )


@dataclass
class Library:
    ids: list[str]
    labels: list[str]
    groups: list[str]  # used for the "don't repeat nearby" rule
    paths: list[Path]
    features: np.ndarray  # (N, grid*grid*3) Lab
    mean_rgb: np.ndarray  # (N, 3) uint8
    crop_box: tuple[float, float, float, float]
    grid: int
    aspect: float = field(default=2048 / 1468)  # tile height / width

    def __len__(self) -> int:
        return len(self.ids)


# -- filtering ---------------------------------------------------------------


def filter_cards(
    cards: Iterable[dict],
    sets: Sequence[str] | None = None,
    inks: Sequence[str] | None = None,
    rarities: Sequence[str] | None = None,
    types: Sequence[str] | None = None,
    lang: str | None = "en",
    portrait_only: bool = True,
) -> list[dict]:
    def norm(values: Sequence[str] | None) -> set[str] | None:
        return {v.strip().lower() for v in values if v.strip()} if values else None

    want_sets, want_inks = norm(sets), norm(inks)
    want_rar, want_types = norm(rarities), norm(types)

    out = []
    for c in cards:
        if lang and (c.get("lang") or "en").lower() != lang.lower():
            continue
        if portrait_only and (c.get("layout") or "normal").lower() != "normal":
            continue  # Locations are landscape and would break the grid
        if want_sets and str((c.get("set") or {}).get("code", "")).lower() not in want_sets:
            continue
        if want_inks and (c.get("ink") or "").lower() not in want_inks:
            continue
        if want_rar and (c.get("rarity") or "").lower() not in want_rar:
            continue
        if want_types:
            have = {t.lower() for t in (c.get("type") or [])}
            if not (have & want_types):
                continue
        out.append(c)
    return out


def card_label(card: dict) -> str:
    name = card.get("name", "?")
    version = card.get("version")
    label = f"{name} - {version}" if version else name
    s = (card.get("set") or {}).get("code")
    num = card.get("collector_number")
    return f"{label} [{s}/{num}]" if s and num else label


# -- feature extraction ------------------------------------------------------


def _crop(img: Image.Image, box: tuple[float, float, float, float]) -> Image.Image:
    w, h = img.size
    l, t, r, b = box
    return img.crop((int(l * w), int(t * h), int(r * w), int(b * h)))


def _signature(path: Path, box: tuple[float, float, float, float], grid: int):
    """-> (lab feature vector, mean rgb, dhash, aspect) for one card."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        im = _crop(im, box)
        aspect = im.height / im.width
        # BOX resize == area average, which is exactly what we want per cell.
        cell = np.asarray(im.resize((grid, grid), Image.BOX), dtype=np.uint8)
        mean = np.asarray(im.resize((1, 1), Image.BOX), dtype=np.uint8).reshape(3)
        gray = np.asarray(im.convert("L").resize((9, 8), Image.BOX), dtype=np.int16)

    lab = image_to_lab(cell).reshape(-1)
    bits = (gray[:, 1:] > gray[:, :-1]).reshape(-1)
    dhash = np.packbits(bits).view(np.uint64)[0] if bits.size == 64 else np.uint64(0)
    return lab.astype(np.float32), mean, np.uint64(dhash), aspect


def _cache_key(size: str, box, grid: int, n: int) -> str:
    raw = json.dumps([size, list(box), grid, n], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def build_library(
    cards: Sequence[dict],
    paths: dict[str, Path],
    crop_box: tuple[float, float, float, float],
    grid: int = 3,
    cache_dir: Path | None = None,
    size: str = "normal",
    dedupe_art: int = 0,
    log: Callable[[str], None] = print,
) -> Library:
    check_avif()

    usable = [c for c in cards if c.get("id") in paths]
    if not usable:
        raise RuntimeError("No cached images for the selected cards - run `fetch` first.")

    feat_cache = None
    if cache_dir is not None:
        key = _cache_key(size, crop_box, grid, len(usable))
        feat_cache = Path(cache_dir) / "features" / f"{key}.npz"
        feat_cache.parent.mkdir(parents=True, exist_ok=True)

    ids = [c["id"] for c in usable]
    loaded = None
    if feat_cache is not None and feat_cache.exists():
        blob = np.load(feat_cache, allow_pickle=True)
        if list(blob["ids"]) == ids:
            loaded = blob
            log(f"library: reusing cached features ({feat_cache.name})")

    if loaded is not None:
        feats = loaded["features"].astype(np.float32)
        means = loaded["mean_rgb"].astype(np.uint8)
        hashes = loaded["dhash"].astype(np.uint64)
        aspects = loaded["aspect"].astype(np.float32)
    else:
        log(f"library: analysing {len(usable)} card images (grid {grid}x{grid})")
        feats = np.zeros((len(usable), grid * grid * 3), dtype=np.float32)
        means = np.zeros((len(usable), 3), dtype=np.uint8)
        hashes = np.zeros(len(usable), dtype=np.uint64)
        aspects = np.zeros(len(usable), dtype=np.float32)
        keep = np.ones(len(usable), dtype=bool)
        for i, card in enumerate(usable):
            try:
                f, m, h, a = _signature(paths[card["id"]], crop_box, grid)
            except Exception as exc:  # corrupt/partial download
                log(f"  ! skipping {card_label(card)}: {exc}")
                keep[i] = False
                continue
            feats[i], means[i], hashes[i], aspects[i] = f, m, h, a
            if (i + 1) % 250 == 0:
                log(f"  {i + 1}/{len(usable)}")
        if not keep.all():
            usable = [c for c, k in zip(usable, keep) if k]
            feats, means, hashes, aspects = feats[keep], means[keep], hashes[keep], aspects[keep]
            ids = [c["id"] for c in usable]
        if feat_cache is not None:
            np.savez_compressed(
                feat_cache, ids=np.array(ids), features=feats,
                mean_rgb=means, dhash=hashes, aspect=aspects,
            )

    if dedupe_art > 0:
        keep = _dedupe(hashes, feats, threshold=dedupe_art)
        removed = int((~keep).sum())
        if removed:
            log(f"library: dropped {removed} near-identical artworks (hamming <= {dedupe_art})")
        usable = [c for c, k in zip(usable, keep) if k]
        feats, means, aspects = feats[keep], means[keep], aspects[keep]

    aspect = float(np.median(aspects)) if len(aspects) else 2048 / 1468

    return Library(
        ids=[c["id"] for c in usable],
        labels=[card_label(c) for c in usable],
        groups=[c.get("name", c["id"]) for c in usable],
        paths=[paths[c["id"]] for c in usable],
        features=feats,
        mean_rgb=means,
        crop_box=crop_box,
        grid=grid,
        aspect=aspect,
    )


def _dedupe(hashes: np.ndarray, feats: np.ndarray, threshold: int = 2, lab_tol: float = 6.0) -> np.ndarray:
    """Greedy near-duplicate removal.

    Two cards are treated as the same artwork only if their structure (dhash)
    *and* their colour signature agree. The hash alone is not enough: very
    smooth artworks produce nearly empty hashes and would all collapse
    together.
    """
    n = len(hashes)
    keep = np.ones(n, dtype=bool)
    if n == 0:
        return keep
    bits = np.unpackbits(hashes.astype("<u8").view(np.uint8).reshape(n, 8), axis=1)
    cells = max(1, feats.shape[1] // 3)
    lab_limit = lab_tol * np.sqrt(cells)
    for i in range(n):
        if not keep[i]:
            continue
        rest = np.flatnonzero(keep[i + 1 :]) + i + 1
        if rest.size == 0:
            continue
        hamming = np.count_nonzero(bits[rest] != bits[i], axis=1)
        near = rest[hamming <= threshold]
        if near.size == 0:
            continue
        diff = feats[near] - feats[i]
        lab = np.sqrt(np.einsum("ij,ij->i", diff, diff))
        keep[near[lab <= lab_limit]] = False
    return keep
