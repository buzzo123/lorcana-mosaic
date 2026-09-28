"""Minimal Lorcast API client (https://lorcast.com/docs/api).

Two things matter here:

* ``api.lorcast.com`` asks for 50-100 ms between requests, so every metadata
  call goes through a single-threaded, throttled session.
* ``cards.lorcast.io`` (the image CDN) has no rate limit, so images are
  downloaded with a small thread pool.

Everything is cached on disk. Card data is only worth refreshing about once a
week (or after a set release), per the API docs.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

API_ROOT = "https://api.lorcast.com/v0"
USER_AGENT = "lorcana-mosaic/0.1 (photomosaic hobby tool)"
POLITE_DELAY = 0.12  # seconds between api.lorcast.com requests

IMAGE_SIZES = ("small", "normal", "large")


class LorcastError(RuntimeError):
    pass


class LorcastClient:
    def __init__(self, timeout: float = 30.0):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last_call = 0.0

    # -- metadata -----------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            wait = POLITE_DELAY - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self.session.get(f"{API_ROOT}{path}", params=params, timeout=self.timeout)
            finally:
                self._last_call = time.monotonic()

        if resp.status_code == 429:
            raise LorcastError("Lorcast returned 429 (too many requests) - slow down and retry later")
        if not resp.ok:
            raise LorcastError(f"GET {path} failed: HTTP {resp.status_code}")
        return resp.json()

    def sets(self) -> list[dict]:
        payload = self._get("/sets")
        return payload.get("results", payload if isinstance(payload, list) else [])

    def set_cards(self, code: str) -> list[dict]:
        payload = self._get(f"/sets/{urllib.parse.quote(str(code))}/cards")
        if isinstance(payload, dict):
            return payload.get("results", [])
        return payload or []

    def search(self, query: str, unique: str = "prints") -> list[dict]:
        payload = self._get("/cards/search", params={"q": query, "unique": unique})
        if isinstance(payload, dict):
            return payload.get("results", [])
        return payload or []


# -- catalog on disk --------------------------------------------------------


def catalog_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / "cards.json"


def fetch_catalog(
    cache_dir: Path,
    query: str | None = None,
    force: bool = False,
    log: Callable[[str], None] = print,
) -> list[dict]:
    """Download every card (or the result of a search query) and cache it."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = catalog_path(cache_dir)

    if path.exists() and not force:
        cards = json.loads(path.read_text())
        log(f"catalog: reusing {len(cards)} cards from {path} (use --force to refresh)")
        return cards

    client = LorcastClient()

    if query:
        log(f"catalog: searching {query!r}")
        cards = client.search(query, unique="prints")
    else:
        sets = client.sets()
        log(f"catalog: {len(sets)} sets")
        cards = []
        for s in sets:
            code = s.get("code") or s.get("id")
            try:
                found = client.set_cards(code)
            except LorcastError as exc:
                log(f"  ! set {code}: {exc}")
                continue
            log(f"  set {code:>5} {s.get('name', '?')[:34]:<34} {len(found):>4} cards")
            cards.extend(found)

    # A card can appear once per print; keep them all but drop exact id dupes.
    seen: set[str] = set()
    unique_cards = []
    for c in cards:
        cid = c.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        unique_cards.append(c)

    path.write_text(json.dumps(unique_cards, indent=1))
    log(f"catalog: saved {len(unique_cards)} cards -> {path}")
    return unique_cards


# -- images -----------------------------------------------------------------


def image_url(card: dict, size: str = "normal") -> str | None:
    uris = (card.get("image_uris") or {}).get("digital") or {}
    order = [size] + [s for s in IMAGE_SIZES if s != size]
    for key in order:
        if uris.get(key):
            return uris[key]
    return None


def image_dir(cache_dir: Path, size: str) -> Path:
    return Path(cache_dir) / "images" / size


def download_images(
    cards: Iterable[dict],
    cache_dir: Path,
    size: str = "normal",
    workers: int = 8,
    log: Callable[[str], None] = print,
) -> dict[str, Path]:
    """Download card images to the cache. Returns card id -> local path.

    The ``?<timestamp>`` query parameter on each URI is the last-updated stamp,
    so it is stored alongside the file and used to detect re-spoiled art.
    """
    cards = list(cards)
    out_dir = image_dir(cache_dir, size)
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "index.json"
    index: dict[str, str] = json.loads(index_path.read_text()) if index_path.exists() else {}

    todo: list[tuple[str, str, Path]] = []
    paths: dict[str, Path] = {}

    for card in cards:
        cid = card.get("id")
        url = image_url(card, size)
        if not cid or not url:
            continue
        dest = out_dir / f"{cid}.avif"
        paths[cid] = dest
        if dest.exists() and index.get(cid) == url:
            continue
        todo.append((cid, url, dest))

    if not todo:
        log(f"images: {len(paths)} already cached in {out_dir}")
        return paths

    log(f"images: downloading {len(todo)} of {len(paths)} ({size}) -> {out_dir}")
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    done = 0
    failed = 0

    def grab(job: tuple[str, str, Path]) -> tuple[str, str, bool]:
        cid, url, dest = job
        try:
            resp = session.get(url, timeout=60)
            if not resp.ok or not resp.content:
                return cid, url, False
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(resp.content)
            tmp.replace(dest)
            return cid, url, True
        except requests.RequestException:
            return cid, url, False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(grab, job) for job in todo]
        for fut in as_completed(futures):
            cid, url, ok = fut.result()
            done += 1
            if ok:
                index[cid] = url
            else:
                failed += 1
                paths.pop(cid, None)
            if done % 100 == 0 or done == len(todo):
                log(f"  {done}/{len(todo)} downloaded ({failed} failed)")

    index_path.write_text(json.dumps(index, indent=0))
    return {cid: p for cid, p in paths.items() if p.exists()}
