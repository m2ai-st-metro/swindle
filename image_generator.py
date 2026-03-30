"""Image generator wrapping banana-maker for cover and thumbnail images."""

import subprocess
import sys
from pathlib import Path

BANANA_MAKER = "/home/apexaipc/.claude/skills/banana-maker/generate_image.py"
BANANA_MAKER_PYTHON = "/home/apexaipc/.claude/skills/banana-maker/venv/bin/python3"


def _run_banana_maker(prompt: str, output_path: str, aspect_ratio: str) -> bool:
    """Call banana-maker as a subprocess.

    Returns:
        True if image was generated successfully.
    """
    python = BANANA_MAKER_PYTHON if Path(BANANA_MAKER_PYTHON).exists() else sys.executable
    cmd = [
        python,
        BANANA_MAKER,
        prompt,
        "--model", "flash",
        "--output", output_path,
        "--aspect-ratio", aspect_ratio,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"banana-maker error: {result.stderr}", file=sys.stderr)
            return False
        # banana-maker may save as .jpg regardless of requested extension
        out = Path(output_path)
        if out.exists():
            return True
        jpg_alt = out.with_suffix(".jpg")
        if jpg_alt.exists():
            jpg_alt.rename(out)
            return True
        return False
    except subprocess.TimeoutExpired:
        print("banana-maker timed out after 120s", file=sys.stderr)
        return False


def generate_cover(title: str, staging_dir: str) -> bool:
    """Generate a 1280x720 cover image.

    Returns:
        True if image was generated successfully.
    """
    output_path = str(Path(staging_dir) / "cover.png")
    prompt = (
        f"Create a sleek product cover image for a developer tool called '{title}'. "
        "Dark background (#0d1117) with subtle gradient. "
        "Show a minimal code editor or terminal aesthetic with glowing accent lines "
        "in electric blue (#58a6ff) and purple (#bc8cff). "
        "Clean, modern, no clip art. Professional developer tool branding."
    )
    return _run_banana_maker(prompt, output_path, "16:9")


def generate_thumbnail(title: str, category: str, staging_dir: str) -> bool:
    """Generate a 600x600 thumbnail image.

    Returns:
        True if image was generated successfully.
    """
    output_path = str(Path(staging_dir) / "thumbnail.png")
    prompt = (
        f"Create a square thumbnail icon for '{title}'. "
        f"Dark background, single iconic symbol representing {category}. "
        "Glowing electric blue accent. Minimal, recognizable at small sizes."
    )
    return _run_banana_maker(prompt, output_path, "1:1")


def build_cover_command(title: str, staging_dir: str) -> list[str]:
    """Return the command that would generate a cover image (for testing/dry-run)."""
    output_path = str(Path(staging_dir) / "cover.png")
    prompt = (
        f"Create a sleek product cover image for a developer tool called '{title}'. "
        "Dark background (#0d1117) with subtle gradient. "
        "Show a minimal code editor or terminal aesthetic with glowing accent lines "
        "in electric blue (#58a6ff) and purple (#bc8cff). "
        "Clean, modern, no clip art. Professional developer tool branding."
    )
    return [
        sys.executable, BANANA_MAKER, prompt,
        "--model", "flash",
        "--output", output_path,
        "--aspect-ratio", "16:9",
    ]


def build_thumbnail_command(title: str, category: str, staging_dir: str) -> list[str]:
    """Return the command that would generate a thumbnail image (for testing/dry-run)."""
    output_path = str(Path(staging_dir) / "thumbnail.png")
    prompt = (
        f"Create a square thumbnail icon for '{title}'. "
        f"Dark background, single iconic symbol representing {category}. "
        "Glowing electric blue accent. Minimal, recognizable at small sizes."
    )
    return [
        sys.executable, BANANA_MAKER, prompt,
        "--model", "flash",
        "--output", output_path,
        "--aspect-ratio", "1:1",
    ]
