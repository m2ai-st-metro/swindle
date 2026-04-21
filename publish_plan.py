"""Publish plan: read a staging/<repo>/ dir into a structured object.

Extracted from the now-shelved gumroad_publisher.py (Playwright era). These
types and helpers are independent of any publisher implementation -- they
just describe "what's in a staging dir, ready to ship."
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class PublishPlan:
    """Everything a publisher needs from a staging dir."""

    repo_name: str
    title: str
    summary: str
    price_cents: int
    tags: list[str]
    description_md: str
    features: list[str]
    button_text: str
    receipt_message: str
    cover_path: Optional[Path]
    thumbnail_path: Optional[Path]
    content_file: Optional[Path]
    staging_dir: Path

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("cover_path", "thumbnail_path", "content_file", "staging_dir"):
            d[k] = str(d[k]) if d[k] else None
        return d


class PlanError(Exception):
    """Raised when staging files are missing or unreadable."""


def build_plan(staging_dir: Path, repo_name: str) -> PublishPlan:
    """Assemble a PublishPlan from a staging/<repo>/ directory.

    Raises PlanError if any required file is missing or malformed.
    """
    sd = Path(staging_dir)
    if not sd.is_dir():
        raise PlanError(f"staging dir not found: {sd}")

    def _read_required(name: str) -> str:
        path = sd / name
        if not path.exists():
            raise PlanError(f"required file missing: {path}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise PlanError(f"required file empty: {path}")
        return text

    description_md = _read_required("listing.md")

    meta_path = sd / "metadata.json"
    if not meta_path.exists():
        raise PlanError(f"metadata.json missing: {meta_path}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PlanError(f"metadata.json invalid JSON: {e}") from e

    summary = (meta.get("summary") or "").strip()
    if not summary:
        raise PlanError("metadata.summary is empty")
    tags = meta.get("tags") or []
    if not isinstance(tags, list) or len(tags) < 3:
        raise PlanError(f"metadata.tags needs >=3 tags, got {tags!r}")
    title = (meta.get("title") or "").strip() or repo_name
    price_cents = int(meta.get("price", 0) or 0) * 100

    features = [
        ln.strip() for ln in _read_required("features.txt").splitlines() if ln.strip()
    ]
    if len(features) < 3:
        raise PlanError(f"features.txt needs >=3 bullets, got {len(features)}")

    button_text = _read_required("button_text.txt")
    receipt_message = _read_required("receipt_message.md")

    cover = sd / "cover.png"
    thumbnail = sd / "thumbnail.png"
    content = sd / "content.zip"
    content_file: Optional[Path] = None
    if content.exists():
        content_file = content
    elif meta.get("content_file"):
        candidate = Path(meta["content_file"])
        if candidate.exists():
            content_file = candidate

    return PublishPlan(
        repo_name=repo_name,
        title=title,
        summary=summary,
        price_cents=price_cents,
        tags=tags,
        description_md=description_md,
        features=features[:5],
        button_text=button_text,
        receipt_message=receipt_message,
        cover_path=cover if cover.exists() and cover.stat().st_size else None,
        thumbnail_path=thumbnail if thumbnail.exists() and thumbnail.stat().st_size else None,
        content_file=content_file,
        staging_dir=sd,
    )
