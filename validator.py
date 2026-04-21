"""Quality gate for Gumroad listing packages.

Reads a staging/<repo>/ directory and returns a list of human-readable
failures. Empty list means pass.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SUMMARY_MAX_CHARS = 140
BUTTON_MAX_CHARS = 25
BUTTON_MAX_WORDS = 4
RECEIPT_MAX_CHARS = 300
FEATURES_MIN = 3
FEATURES_MAX = 5
FEATURE_MAX_CHARS = 120
DESCRIPTION_MIN_CHARS = 120
DESCRIPTION_MAX_CHARS = 4000

PLACEHOLDER_PATTERNS = (
    "output here",
    "replace this",
    "<text>",
    "<your text>",
    "lorem ipsum",
    "placeholder",
    "tbd",
    "todo:",
)

REASONING_ARTIFACTS = (
    "let me think",
    "on second thought",
    "character count:",
    "word count:",
    "as an ai",
    "i apologize",
    "i'm sorry",
    "here is the",
    "here's the",
    "hope this helps",
    "note:",
)

STUB_TAG_SET = frozenset({"tag1", "tag2", "tag3"})


def validate_listing(staging_dir: Path) -> list[str]:
    """Validate a staging directory. Returns a list of failure messages.

    Args:
        staging_dir: Path to staging/<repo>/ directory.

    Returns:
        List of failure strings. Empty list means the listing passes.
    """
    failures: list[str] = []
    sd = Path(staging_dir)

    if not sd.is_dir():
        return [f"staging dir does not exist: {sd}"]

    _check_description(sd, failures)
    _check_metadata(sd, failures)
    _check_features(sd, failures)
    _check_button_text(sd, failures)
    _check_receipt_message(sd, failures)
    _check_images(sd, failures)

    return failures


def _check_description(sd: Path, failures: list[str]) -> None:
    path = sd / "listing.md"
    if not path.exists():
        failures.append("listing.md missing")
        return

    text = path.read_text(encoding="utf-8")
    if len(text) < DESCRIPTION_MIN_CHARS:
        failures.append(
            f"listing.md too short ({len(text)} chars, need >={DESCRIPTION_MIN_CHARS})"
        )
    if len(text) > DESCRIPTION_MAX_CHARS:
        failures.append(
            f"listing.md too long ({len(text)} chars, max {DESCRIPTION_MAX_CHARS})"
        )

    lower = text.lower()
    for pat in PLACEHOLDER_PATTERNS:
        if pat in lower:
            failures.append(f"listing.md contains placeholder: {pat!r}")
    for pat in REASONING_ARTIFACTS:
        if pat in lower:
            failures.append(f"listing.md contains reasoning artifact: {pat!r}")

    # Empty section check: any "## Header" with no non-blank content before next header or EOF
    sections = re.split(r"^##\s+.+$", text, flags=re.MULTILINE)
    # sections[0] is preamble before any header; rest are content-between-headers
    for i, body in enumerate(sections[1:], start=1):
        if not body.strip():
            failures.append(f"listing.md has empty section (section {i})")


def _check_metadata(sd: Path, failures: list[str]) -> None:
    path = sd / "metadata.json"
    if not path.exists():
        failures.append("metadata.json missing")
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        failures.append(f"metadata.json invalid JSON: {e}")
        return

    summary = data.get("summary") or ""
    title = data.get("title") or ""
    if not summary.strip():
        failures.append("metadata.summary is empty")
    elif len(summary) > SUMMARY_MAX_CHARS:
        failures.append(
            f"metadata.summary too long ({len(summary)} chars, max {SUMMARY_MAX_CHARS})"
        )
    elif summary.strip() == title.strip():
        failures.append("metadata.summary is just the title (LLM fallback — regenerate)")

    tags = data.get("tags") or []
    if not isinstance(tags, list) or len(tags) < 3:
        failures.append(f"metadata.tags needs >=3 tags, got {tags!r}")
    elif {t.lower() for t in tags} == STUB_TAG_SET:
        failures.append("metadata.tags is the stub ['tag1','tag2','tag3']")


def _check_features(sd: Path, failures: list[str]) -> None:
    path = sd / "features.txt"
    if not path.exists():
        failures.append("features.txt missing")
        return

    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) < FEATURES_MIN:
        failures.append(f"features.txt has {len(lines)} bullets, need >={FEATURES_MIN}")
    if len(lines) > FEATURES_MAX:
        failures.append(f"features.txt has {len(lines)} bullets, max {FEATURES_MAX}")

    for i, line in enumerate(lines, start=1):
        if len(line) > FEATURE_MAX_CHARS:
            failures.append(
                f"features.txt line {i} too long ({len(line)} chars, max {FEATURE_MAX_CHARS})"
            )
        if re.match(r"^[-*•]|^\d+[.)]\s", line):
            failures.append(f"features.txt line {i} still has bullet marker: {line!r}")


def _check_button_text(sd: Path, failures: list[str]) -> None:
    path = sd / "button_text.txt"
    if not path.exists():
        failures.append("button_text.txt missing")
        return

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        failures.append("button_text.txt is empty")
        return
    if len(text) > BUTTON_MAX_CHARS:
        failures.append(f"button_text too long ({len(text)} chars, max {BUTTON_MAX_CHARS})")
    words = text.split()
    if len(words) > BUTTON_MAX_WORDS:
        failures.append(f"button_text has {len(words)} words, max {BUTTON_MAX_WORDS}")
    if text.startswith(('"', "'")) or text.endswith(('"', "'")):
        failures.append(f"button_text has stray quotes: {text!r}")


def _check_receipt_message(sd: Path, failures: list[str]) -> None:
    path = sd / "receipt_message.md"
    if not path.exists():
        failures.append("receipt_message.md missing")
        return

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        failures.append("receipt_message.md is empty")
        return
    if len(text) > RECEIPT_MAX_CHARS:
        failures.append(
            f"receipt_message too long ({len(text)} chars, max {RECEIPT_MAX_CHARS})"
        )
    lower = text.lower()
    if lower.startswith("dear "):
        failures.append("receipt_message starts with 'Dear ' (strip salutation)")
    for pat in REASONING_ARTIFACTS:
        if pat in lower:
            failures.append(f"receipt_message contains reasoning artifact: {pat!r}")


def _check_images(sd: Path, failures: list[str]) -> None:
    for name in ("cover.png", "thumbnail.png"):
        path = sd / name
        if not path.exists():
            failures.append(f"{name} missing")
        elif path.stat().st_size == 0:
            failures.append(f"{name} is 0 bytes")
