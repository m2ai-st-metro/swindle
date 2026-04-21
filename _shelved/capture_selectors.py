"""Selector capture helper.

Wraps `playwright codegen` so the user walks through the Gumroad new-product
flow once, filling each field with a sentinel value. The emitted Python is
parsed to pull out the locator chain that precedes each sentinel fill, and
those locator chains become the authoritative selectors for the publisher.

Flow
----
1. ensure_sentinel_files() lays down on-disk sentinel payloads (1x1 PNG,
   empty zip) so the user can point Gumroad's file pickers at real files
   during the capture walkthrough.
2. run_codegen() opens Gumroad in a Chromium window with the Playwright
   Inspector. Prints sentinel values the user must paste into each field.
3. User closes the Inspector when done. Codegen writes captured Python to
   the file we passed with -o.
4. parse_capture() reads that file, finds every .fill("SENTINEL") call and
   every .set_input_files("/tmp/SWINDLE_SENTINEL_*") call, and extracts
   the Playwright locator expression immediately before each. Labelled
   .click() calls are matched by visible text.
5. apply_captures() rewrites gumroad_selectors.py, preserving the module's
   structure but replacing matched selector string literals with the
   captured locator expressions (as string values — publisher evaluates
   them with page.locator(...)).

Sentinels are chosen to be unmistakable in the capture output: long, all-
caps, product-name-unlike. Upload-file sentinels use distinct paths under
/tmp/ so the set_input_files() parser can match them by filename.

CLI
---
    python capture_selectors.py run          # ensure files, run codegen, apply
    python capture_selectors.py walkthrough  # print the walkthrough and exit
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

SENTINELS = {
    "NEW_PRODUCT_NAME": "SWINDLE_SENTINEL_NAME",
    "NEW_PRODUCT_PRICE": "0.99",
    "DESCRIPTION_EDITOR": "SWINDLE_SENTINEL_DESCRIPTION_BODY_LONG_ENOUGH",
    "SUMMARY_INPUT": "SWINDLE_SENTINEL_SUMMARY_ONE_LINE",
    "TAGS_INPUT": "swindle-sentinel-tag-unique",
    "FEATURE_INPUTS": "SWINDLE_SENTINEL_FEATURE_A",
    "BUTTON_TEXT_INPUT": "SWINDLE_SENTINEL_BUTTON_TEXT",
    "RECEIPT_MESSAGE_EDITOR": "SWINDLE_SENTINEL_RECEIPT_BODY",
    # Upload sentinels: matched by the file path the user selects in the
    # native file picker. parse_capture() pulls the locator chain sitting
    # before each set_input_files("...") whose argument contains the sentinel.
    "COVER_UPLOAD_INPUT": "/tmp/SWINDLE_SENTINEL_COVER.png",
    "THUMBNAIL_UPLOAD_INPUT": "/tmp/SWINDLE_SENTINEL_THUMBNAIL.png",
    "CONTENT_UPLOAD_INPUT": "/tmp/SWINDLE_SENTINEL_CONTENT.zip",
}

# Which sentinels are file-picker uploads (matched via set_input_files, not
# fill). These must be real files on disk so the native picker accepts them.
UPLOAD_SENTINELS = {
    "COVER_UPLOAD_INPUT",
    "THUMBNAIL_UPLOAD_INPUT",
    "CONTENT_UPLOAD_INPUT",
}

# Minimum valid 1x1 PNG (67 bytes) — 8-byte PNG signature + IHDR + IDAT + IEND.
_MINIMAL_PNG_BYTES = bytes([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,  # signature
    0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,  # IHDR length + type
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,  # 1x1
    0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,  # 8-bit RGBA
    0x89,
    0x00, 0x00, 0x00, 0x0D, 0x49, 0x44, 0x41, 0x54,  # IDAT length + type
    0x78, 0x9C, 0x62, 0x00, 0x01, 0x00, 0x00, 0x05,
    0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4,
    0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44,  # IEND length + type
    0xAE, 0x42, 0x60, 0x82,
])

# Empty zip archive (22 bytes) — just the End of Central Directory record.
_EMPTY_ZIP_BYTES = bytes([
    0x50, 0x4B, 0x05, 0x06,  # EOCD signature
    0x00, 0x00,              # disk number
    0x00, 0x00,              # disk with central dir
    0x00, 0x00,              # entries on this disk
    0x00, 0x00,              # total entries
    0x00, 0x00, 0x00, 0x00,  # central dir size
    0x00, 0x00, 0x00, 0x00,  # central dir offset
    0x00, 0x00,              # comment length
])


def ensure_sentinel_files() -> dict[str, Path]:
    """Create the on-disk sentinel files used by upload pickers.

    Writes 1x1 PNG bytes to the cover/thumbnail sentinel paths and empty-zip
    bytes to the content sentinel path, if they don't already exist. Returns
    the mapping of selector-name -> Path so the caller can surface the list.
    """
    payloads: dict[str, bytes] = {
        "COVER_UPLOAD_INPUT": _MINIMAL_PNG_BYTES,
        "THUMBNAIL_UPLOAD_INPUT": _MINIMAL_PNG_BYTES,
        "CONTENT_UPLOAD_INPUT": _EMPTY_ZIP_BYTES,
    }
    created: dict[str, Path] = {}
    for name, payload in payloads.items():
        p = Path(SENTINELS[name])
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(payload)
        created[name] = p
    return created

# Click-only fields captured by matching the text the user clicks
CLICK_TARGETS = {
    "PRODUCT_TYPE_DIGITAL": "Digital product",
    "CREATE_PRODUCT_BUTTON": "Next",
    "TAB_PRODUCT": "Product",
    "TAB_CONTENT": "Content",
    "TAB_CHECKOUT": "Checkout",
    "TAB_RECEIPT": "Receipt",
    "ADDITIONAL_DETAILS_ACCORDION": "Additional details",
    "ADD_FEATURE_BUTTON": "Add feature",
    "SAVE_DRAFT_BUTTON": "Save as draft",
}

WALKTHROUGH = f"""
SELECTOR CAPTURE WALKTHROUGH
============================

A Playwright Inspector window will open. In the browser pane:

 1. Log into Gumroad if not already.
 2. Navigate to https://app.gumroad.com/products/new
 3. Click "Digital product".
 4. Fill product name with exactly:  {SENTINELS['NEW_PRODUCT_NAME']!r}
 5. Fill price with:                 {SENTINELS['NEW_PRODUCT_PRICE']!r}
 6. Click "Next" (or "Create product").

Once the editor loads:

 7. In the Product tab:
     a. Click to open description editor, type:
        {SENTINELS['DESCRIPTION_EDITOR']!r}
     b. Fill Summary with:           {SENTINELS['SUMMARY_INPUT']!r}
     c. Type in the tags field:      {SENTINELS['TAGS_INPUT']!r}  (press Enter)
     d. Click the cover-image upload zone. In the native file picker, select:
        {SENTINELS['COVER_UPLOAD_INPUT']!r}
     e. Click the thumbnail upload zone. In the native file picker, select:
        {SENTINELS['THUMBNAIL_UPLOAD_INPUT']!r}
 8. Click "Additional details" if collapsed.
 9. Click "Add feature" and fill one feature with:
        {SENTINELS['FEATURE_INPUTS']!r}
10. Click the "Content" tab. Click the "Upload files" zone and select:
        {SENTINELS['CONTENT_UPLOAD_INPUT']!r}
11. Click the "Checkout" tab. Fill button text with:
        {SENTINELS['BUTTON_TEXT_INPUT']!r}
12. Click the "Receipt" tab. Fill the custom message with:
        {SENTINELS['RECEIPT_MESSAGE_EDITOR']!r}
13. Click "Save as draft" (do NOT publish).

When every field above has been filled with the matching sentinel, close the
Playwright Inspector window. Capture will be parsed automatically.

You can skip any step (e.g., you don't care about receipt message right now)
-- missing selectors stay as the best-guess defaults currently in
gumroad_selectors.py.
"""


@dataclass
class Capture:
    """A single selector extracted from the codegen Python."""

    name: str  # e.g. NEW_PRODUCT_NAME
    locator_expr: str  # e.g. 'get_by_label("Name")' or 'locator("#name")'
    matched_via: str  # 'fill-sentinel' | 'click-text' | 'set_input_files'


@dataclass
class CaptureResult:
    raw_path: Path
    raw_code: str
    captures: list[Capture] = field(default_factory=list)
    unmatched_sentinels: list[str] = field(default_factory=list)
    unmatched_clicks: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Running codegen
# --------------------------------------------------------------------------


def run_codegen(
    start_url: str = "https://app.gumroad.com/login",
    output_path: Optional[Path] = None,
    target: str = "python",
) -> Path:
    """Launch playwright codegen, block until user closes the Inspector.

    Returns the path of the captured Python file.
    """
    if output_path is None:
        output_path = Path(tempfile.gettempdir()) / f"swindle_codegen_{datetime.now():%Y%m%d_%H%M%S}.py"
    cmd = [
        sys.executable, "-m", "playwright", "codegen",
        "--target", target,
        "-o", str(output_path),
        start_url,
    ]
    subprocess.run(cmd, check=False)
    return output_path


# --------------------------------------------------------------------------
# Parsing codegen output
# --------------------------------------------------------------------------

# A "locator chain" is the expression immediately before a .fill(...) /
# .click() / .set_input_files(...) call. Examples:
#   page.get_by_role("textbox", name="Name")
#   page.get_by_label("Price")
#   page.locator("#product-name")
#   page.get_by_placeholder("Describe")
#   frame_locator("iframe").get_by_role("button")
#
# We capture everything after "page." (or "frame_locator(...).") up to the
# terminal method call.

LOCATOR_CHAIN = r"(?:page\.|frame_locator\([^)]*\)\.)((?:[a-z_]+\([^)]*\)\.?)+?)"
FILL_RE = re.compile(LOCATOR_CHAIN + r"fill\((?P<value>(?:f?r?\"[^\"\\]*(?:\\.[^\"\\]*)*\"|'[^']*'))\)")
CLICK_RE = re.compile(LOCATOR_CHAIN + r"click\(\)")
FILES_RE = re.compile(LOCATOR_CHAIN + r"set_input_files\((?P<value>[^)]*)\)")


def _strip_quotes(s: str) -> str:
    s = s.strip()
    for p in ('f"', 'r"', 'fr"', 'rf"'):
        if s.startswith(p) and s.endswith('"'):
            return s[len(p):-1]
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    return s


def _dot_trim(expr: str) -> str:
    """Strip trailing dot (artifact of the regex capture)."""
    return expr.rstrip(".")


def parse_capture(path: Path) -> CaptureResult:
    """Parse a codegen-emitted Python file; extract selectors by sentinel.

    Matches three kinds of codegen output:
      * .fill("SENTINEL_VALUE") — text-field selectors
      * .set_input_files("/tmp/SWINDLE_SENTINEL_*.{png,zip}") — upload inputs
      * .click() with a visible-text locator matching a CLICK_TARGETS entry
    """
    raw = path.read_text(encoding="utf-8")
    result = CaptureResult(raw_path=path, raw_code=raw)

    # Sentinel-fill captures
    fill_by_value: dict[str, str] = {}
    for m in FILL_RE.finditer(raw):
        value = _strip_quotes(m.group("value"))
        if not value:
            continue
        fill_by_value.setdefault(value, _dot_trim(m.group(1)))

    # set_input_files captures — the argument can be a single quoted string, a
    # list, or a variable. We accept any occurrence of the sentinel path as a
    # substring of the raw argument expression.
    files_args: list[tuple[str, str]] = []  # (locator_chain, raw_arg_text)
    for m in FILES_RE.finditer(raw):
        files_args.append((_dot_trim(m.group(1)), m.group("value")))

    for sel_name, sentinel in SENTINELS.items():
        if sel_name in UPLOAD_SENTINELS:
            matched = None
            for loc, arg_text in files_args:
                # The sentinel path appears verbatim inside the arg expression
                # (e.g. "/tmp/SWINDLE_SENTINEL_COVER.png").
                if sentinel in arg_text:
                    matched = loc
                    break
            if matched:
                result.captures.append(
                    Capture(name=sel_name, locator_expr=matched,
                            matched_via="set_input_files")
                )
            else:
                result.unmatched_sentinels.append(sel_name)
            continue

        if sentinel in fill_by_value:
            result.captures.append(
                Capture(name=sel_name, locator_expr=fill_by_value[sentinel],
                        matched_via="fill-sentinel")
            )
        else:
            result.unmatched_sentinels.append(sel_name)

    # Click-text captures
    click_locators: list[str] = []
    for m in CLICK_RE.finditer(raw):
        click_locators.append(_dot_trim(m.group(1)))

    for sel_name, text in CLICK_TARGETS.items():
        matched = None
        for loc in click_locators:
            if f"'{text}'" in loc or f'"{text}"' in loc:
                matched = loc
                break
        if matched:
            result.captures.append(
                Capture(name=sel_name, locator_expr=matched, matched_via="click-text")
            )
        else:
            result.unmatched_clicks.append(sel_name)

    return result


# --------------------------------------------------------------------------
# Writing captures into gumroad_selectors.py
# --------------------------------------------------------------------------


def locator_expr_to_selector_string(expr: str) -> str:
    """Convert a Playwright chain to a string the publisher can pass to page.locator().

    The publisher currently calls page.locator(S.SELECTOR).fill(...). To stay
    compatible, we emit selectors as strings. For chains like:
      get_by_role("textbox", name="Name") -> 'role=textbox[name="Name"]'
      get_by_label("Price")               -> 'label=Price'
      get_by_placeholder("Describe")      -> 'placeholder=Describe'
      locator("#foo")                     -> '#foo'
      get_by_test_id("x")                 -> 'data-testid=x'
      get_by_text("Save")                 -> 'text=Save'
    For anything we don't recognize, fall back to the raw expression wrapped
    as a Playwright engine string: 'xpath=...' style.
    """
    expr = expr.strip()

    m = re.match(r'get_by_role\("([^"]+)",\s*name="([^"]+)"\)$', expr)
    if m:
        return f'role={m.group(1)}[name="{m.group(2)}"]'

    m = re.match(r'get_by_role\("([^"]+)"\)$', expr)
    if m:
        return f"role={m.group(1)}"

    m = re.match(r'get_by_label\("([^"]+)"\)$', expr)
    if m:
        return f"label={m.group(1)}"

    m = re.match(r'get_by_placeholder\("([^"]+)"\)$', expr)
    if m:
        return f"placeholder={m.group(1)}"

    m = re.match(r'get_by_test_id\("([^"]+)"\)$', expr)
    if m:
        return f"data-testid={m.group(1)}"

    m = re.match(r'get_by_text\("([^"]+)"(?:,\s*exact=(?:True|False))?\)$', expr)
    if m:
        return f"text={m.group(1)}"

    m = re.match(r'locator\("([^"]+)"\)$', expr)
    if m:
        return m.group(1)

    # Fallback: preserve the raw expression as a Playwright "internal" string.
    # The publisher could eval() this if needed, but safer: emit as comment +
    # leave the original selector in place.
    return f"__RAW__:{expr}"


def apply_captures(
    captures: list[Capture],
    module_path: Path,
    *,
    backup: bool = True,
) -> dict[str, tuple[str, str]]:
    """Rewrite gumroad_selectors.py to use the captured selectors.

    For each Capture: find `NAME: Final = "..."` and swap the right-hand
    string for the captured selector. Leaves lines that didn't match the
    pattern alone (so comments / helpers are preserved).

    Returns a dict of {selector_name: (old_value, new_value)} for the UI.
    Skips entries where the new value contains __RAW__ — those require manual
    review.
    """
    raw = module_path.read_text(encoding="utf-8")
    if backup:
        bak = module_path.with_suffix(
            f".py.bak-{datetime.now():%Y%m%d-%H%M%S}"
        )
        shutil.copy2(module_path, bak)

    diffs: dict[str, tuple[str, str]] = {}

    for cap in captures:
        new_str = locator_expr_to_selector_string(cap.locator_expr)
        if new_str.startswith("__RAW__"):
            continue

        span = _find_assignment_span(raw, cap.name)
        if span is None:
            continue
        start, end = span
        old_block = raw[start:end]
        replacement = f"{cap.name}: Final = {repr(new_str)}"
        if old_block == replacement:
            continue
        raw = raw[:start] + replacement + raw[end:]
        diffs[cap.name] = (old_block.split("=", 1)[1].strip(), new_str)

    module_path.write_text(raw, encoding="utf-8")
    return diffs


def _find_assignment_span(source: str, name: str) -> Optional[tuple[int, int]]:
    """Return (start, end) byte offsets of the assignment block `NAME: Final = ...`.

    Handles both single-line and multi-line parenthesised RHS by using the
    tokenize module, which is string-aware (treats `)` inside quoted strings
    as ordinary characters).
    """
    import io
    import tokenize

    try:
        lines = source.splitlines(keepends=True)
        reader = io.StringIO(source).readline
        tokens = list(tokenize.generate_tokens(reader))
    except tokenize.TokenizeError:
        return None

    def offset(row: int, col: int) -> int:
        return sum(len(lines[i]) for i in range(row - 1)) + col

    for i, tok in enumerate(tokens):
        if tok.type == tokenize.NAME and tok.string == name:
            # Expect pattern: NAME : Final = <value> NEWLINE
            j = i + 1
            # Skip whitespace tokens between NAME and the end of the statement
            depth = 0
            end_tok = None
            while j < len(tokens):
                t = tokens[j]
                if t.string in ("(", "[", "{"):
                    depth += 1
                elif t.string in (")", "]", "}"):
                    depth -= 1
                if t.type == tokenize.NEWLINE and depth == 0:
                    end_tok = t
                    break
                j += 1
            if end_tok is None:
                continue
            start = offset(tok.start[0], tok.start[1])
            end = offset(end_tok.start[0], end_tok.start[1])
            return (start, end)
    return None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

import click  # noqa: E402  (intentional: keep CLI deps out of library import path discussion)


@click.group()
def cli() -> None:
    """Capture Gumroad selectors via playwright codegen."""


@cli.command()
@click.option(
    "--selectors-path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path(__file__).parent / "gumroad_selectors.py",
    show_default=True,
    help="Path to gumroad_selectors.py to rewrite.",
)
@click.option(
    "--start-url",
    default="https://app.gumroad.com/login",
    show_default=True,
    help="Codegen launches here. Log in first if needed.",
)
def run(selectors_path: Path, start_url: str) -> None:
    """Run the full capture loop: ensure files, codegen, parse, apply."""
    if sys.platform != "win32" and not os.environ.get("DISPLAY"):
        raise click.ClickException(
            "No X display available ($DISPLAY is empty). Playwright codegen "
            "needs a visible browser. Options:\n"
            "  - Sit at the ProBook's GUI desktop and run this from a "
            "terminal opened there.\n"
            "  - SSH with X11 forwarding from a machine running an X server: "
            "`ssh -Y apexaipc@10.0.0.46` (e.g. VcXsrv on Windows, XQuartz "
            "on macOS).\n"
            "  - Port capture_selectors.py to a machine with a real display."
        )
    click.echo(WALKTHROUGH)
    ensure_sentinel_files()
    click.echo("Launching playwright codegen — close the Inspector when done.\n")
    capture_path = run_codegen(start_url=start_url)
    if not capture_path.exists() or capture_path.stat().st_size == 0:
        raise click.ClickException(
            f"Codegen produced no capture at {capture_path}. Check the "
            f"codegen output above for the real error (common causes: "
            f"missing X server, browser launch failure, user killed the "
            f"Inspector before recording anything)."
        )
    click.echo(f"\nParsing {capture_path}")
    result = parse_capture(capture_path)
    diffs = apply_captures(result.captures, selectors_path, backup=True)

    click.echo("\n=== Capture summary ===")
    click.echo(f"Matched captures:     {len(result.captures)}")
    click.echo(f"Unmatched sentinels:  {len(result.unmatched_sentinels)}"
               + (f"  ({', '.join(result.unmatched_sentinels)})"
                  if result.unmatched_sentinels else ""))
    click.echo(f"Unmatched clicks:     {len(result.unmatched_clicks)}"
               + (f"  ({', '.join(result.unmatched_clicks)})"
                  if result.unmatched_clicks else ""))
    click.echo(f"\nRewrote {len(diffs)} selector(s) in {selectors_path}:")
    for name, (old, new) in diffs.items():
        click.echo(f"  {name}: {old} -> {new!r}")


@cli.command()
def walkthrough() -> None:
    """Print the capture walkthrough and exit."""
    click.echo(WALKTHROUGH)


if __name__ == "__main__":
    cli()
