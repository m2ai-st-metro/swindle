"""Quality gate for Gumroad listing packages.

Reads a staging/<repo>/ directory and returns a list of human-readable
failures. Empty list means pass.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SUMMARY_MAX_CHARS = 140
TITLE_MAX_CHARS = 35
TITLE_MAX_WORDS = 4
TAG_MAX_CHARS = 20  # Gumroad rejects POST /products with any tag > 20 chars
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

# Regex patterns that catch outcome/benefit claims with numeric payloads.
# Each match is treated as a claim that MUST be grounded in the source spec.
# Patterns are deliberately scoped to "outcome verbs" or benefit framings so
# neutral references to version numbers, test counts, file counts, etc. don't
# trigger the guard. `re.IGNORECASE` is applied in the check function.
NUMERIC_CLAIM_PATTERNS = (
    # "save 2+ hours per week", "saves 30 minutes/day", "cut 5 days per month"
    r"\b(?:save[sd]?|saving|cut(?:s|ting)?|reduce[sd]?|reducing|shave[sd]?)\s+"
    r"\d+\+?\s*(?:hours?|hrs?|minutes?|mins?|days?|weeks?|months?)"
    r"(?:\s*(?:per|/|a)\s*(?:week|day|month|hour))?",

    # "reduce bugs by 80%", "cut costs by 40 percent", "save 30%"
    r"\b(?:reduce[sd]?|reducing|cut(?:s|ting)?|save[sd]?|saving)\s+"
    r"(?:\w+\s+){0,3}by\s+\d+\+?\s*(?:%|percent)",
    r"\b(?:reduce[sd]?|reducing|cut(?:s|ting)?|save[sd]?|saving)\s+\d+\+?\s*(?:%|percent)\b",

    # "boost/improve/increase X by 50%"
    r"\b(?:boost(?:s|ed|ing)?|improve[sd]?|improving|increase[sd]?|increasing|"
    r"accelerate[sd]?)\s+(?:\w+\s+){0,3}by\s+\d+\+?\s*(?:%|percent)",

    # "N% faster", "30% less", "50% more" (percent-qualified comparatives)
    r"\b\d+\+?\s*(?:%|percent)\s+(?:faster|slower|less|more|better|cheaper|"
    r"higher|lower|fewer|greater)\b",

    # "10x faster", "3× more", "5x less" (multiplier comparatives)
    r"\b\d+\+?\s*[x×]\s+(?:faster|slower|less|more|better|cheaper|higher|"
    r"lower|fewer|greater)\b",

    # "10 times faster", "3 times more" (verbal multiplier comparatives)
    r"\b\d+\+?\s+times\s+(?:faster|slower|less|more|better|cheaper|higher|"
    r"lower|fewer|greater)\b",
)


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
    _check_no_fabricated_claims(sd, failures)

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

    if title.strip():
        if len(title) > TITLE_MAX_CHARS:
            failures.append(
                f"metadata.title too long ({len(title)} chars, max {TITLE_MAX_CHARS}) — run `swindle retitle {data.get('repo_url', '<repo>').rstrip('/').rsplit('/', 1)[-1]}`"
            )
        if len(title.split()) > TITLE_MAX_WORDS:
            failures.append(
                f"metadata.title has {len(title.split())} words, max {TITLE_MAX_WORDS}"
            )

    tags = data.get("tags") or []
    if not isinstance(tags, list) or len(tags) < 3:
        failures.append(f"metadata.tags needs >=3 tags, got {tags!r}")
    elif {t.lower() for t in tags} == STUB_TAG_SET:
        failures.append("metadata.tags is the stub ['tag1','tag2','tag3']")
    else:
        for t in tags:
            if isinstance(t, str) and len(t) > TAG_MAX_CHARS:
                failures.append(
                    f"metadata.tags[{tags.index(t)}] {t!r} is {len(t)} chars, "
                    f"max {TAG_MAX_CHARS} (Gumroad rejects over-long tags)"
                )


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


def _normalize_claim(text: str) -> str:
    """Lowercase, collapse whitespace, strip surrounding punctuation.

    Used for provenance matching — we check whether a normalized claim
    appears as a substring of normalized spec text.
    """
    cleaned = re.sub(r"\s+", " ", text).strip().lower()
    # Strip wrapping punctuation but preserve internal chars like '+', '%', etc.
    cleaned = cleaned.strip(".,;:!?\"'()[]{}")
    return cleaned


def _check_no_fabricated_claims(sd: Path, failures: list[str]) -> None:
    """Flag unsourced numeric outcome claims in listing.md and metadata.summary.

    Strict-by-default: if metadata.json has no `spec_path` or the spec file is
    missing, ANY numeric claim fails validation. Auto-publish requires
    provenance — we will not let a claim go live without a source.
    """
    listing_path = sd / "listing.md"
    metadata_path = sd / "metadata.json"

    # Collect text surfaces that need screening. Missing files have already
    # been flagged elsewhere; skip silently here.
    surfaces: list[tuple[str, str]] = []
    if listing_path.exists():
        surfaces.append(("listing.md", listing_path.read_text(encoding="utf-8")))

    metadata: dict = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            metadata = {}
        summary_text = metadata.get("summary") or ""
        if summary_text.strip():
            surfaces.append(("metadata.summary", summary_text))

    # Find all claim matches across surfaces first. If nothing matches, we can
    # short-circuit without even touching the spec file.
    found: list[tuple[str, str]] = []  # (surface_name, matched_text)
    for surface_name, text in surfaces:
        for pattern in NUMERIC_CLAIM_PATTERNS:
            for m in re.finditer(pattern, text, flags=re.IGNORECASE):
                found.append((surface_name, m.group(0)))

    if not found:
        return

    # Provenance source: spec_path from metadata.json.
    spec_path_value = metadata.get("spec_path")
    spec_text_normalized: str | None = None

    if not spec_path_value:
        for surface_name, match_text in found:
            failures.append(
                f"numeric claim {match_text!r} in {surface_name} cannot be "
                f"verified: metadata.json missing spec_path"
            )
        return

    spec_file = Path(spec_path_value)
    if not spec_file.exists() or not spec_file.is_file():
        for surface_name, match_text in found:
            failures.append(
                f"numeric claim {match_text!r} in {surface_name} cannot be "
                f"verified: metadata.json missing spec_path"
            )
        return

    try:
        spec_raw = spec_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        for surface_name, match_text in found:
            failures.append(
                f"numeric claim {match_text!r} in {surface_name} cannot be "
                f"verified: metadata.json missing spec_path"
            )
        return

    spec_text_normalized = _normalize_claim(spec_raw)
    # Also keep a whitespace-collapsed lowercase version for substring match
    # (the stripping in _normalize_claim is for the outer edges only; we want
    # free substring matching through the body of the spec).
    spec_body = re.sub(r"\s+", " ", spec_raw).lower()

    for surface_name, match_text in found:
        needle = _normalize_claim(match_text)
        if needle and needle in spec_body:
            continue
        failures.append(
            f"unsourced numeric claim in {surface_name}: {match_text!r}"
        )
