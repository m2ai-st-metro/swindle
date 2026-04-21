"""Image generator wrapping banana-maker for cover and thumbnail images."""

import subprocess
import sys
from pathlib import Path

BANANA_MAKER = "/home/apexaipc/.claude/skills/banana-maker/generate_image.py"
BANANA_MAKER_PYTHON = "/home/apexaipc/.claude/skills/banana-maker/venv/bin/python3"


STYLE_DEVELOPER = "developer"
STYLE_PHOTOGRAPHIC = "photographic"
DEFAULT_STYLE = STYLE_DEVELOPER
KNOWN_STYLES = (STYLE_DEVELOPER, STYLE_PHOTOGRAPHIC)


def _developer_cover_prompt(title: str, summary: str) -> str:  # noqa: ARG001
    return (
        f"Create a sleek product cover image for a developer tool called '{title}'. "
        "Dark background (#0d1117) with subtle gradient. "
        "Show a minimal code editor or terminal aesthetic with glowing accent lines "
        "in electric blue (#58a6ff) and purple (#bc8cff). "
        "Clean, modern, no clip art. Professional developer tool branding."
    )


def _developer_thumbnail_prompt(title: str, category: str) -> str:
    return (
        f"Create a square thumbnail icon for '{title}'. "
        f"Dark background, single iconic symbol representing {category}. "
        "Glowing electric blue accent. Minimal, recognizable at small sizes."
    )


def _photographic_cover_prompt(title: str, summary: str) -> str:
    scene = summary.strip() or f"the essence of the product called '{title}'"
    return (
        "Photorealistic hero image, editorial photography quality, "
        "natural light, shallow depth of field, cinematic composition. "
        f"Subject: a scene that visually represents {scene}. "
        "Think real hands, real objects, real workspaces — "
        "a candid moment, not a posed advertisement. "
        "Warm, muted palette. Real textures (wood, paper, fabric, skin). "
        "No screens showing fake UI, no code overlays, no text in image, "
        "no robotic humans, no AI cliches (circuit boards, glowing blue lines, "
        "holograms). Leave space in upper third for a title overlay — "
        "the composition should flow toward the lower two-thirds."
    )


def _photographic_thumbnail_prompt(title: str, category: str) -> str:
    return (
        "Photorealistic square image, editorial photography, natural light, "
        f"close-up of a single tangible object or small scene that evokes "
        f"'{category}' — a real prop or hand gesture, not a logo. "
        "Warm tones, shallow depth of field, macro-style intimacy. "
        "No UI, no text, no screens, no clip-art, no AI cliches."
    )


COVER_PROMPTS = {
    STYLE_DEVELOPER: _developer_cover_prompt,
    STYLE_PHOTOGRAPHIC: _photographic_cover_prompt,
}

THUMBNAIL_PROMPTS = {
    STYLE_DEVELOPER: _developer_thumbnail_prompt,
    STYLE_PHOTOGRAPHIC: _photographic_thumbnail_prompt,
}


def _run_banana_maker(prompt: str, output_path: str, aspect_ratio: str) -> bool:
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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f"banana-maker error: {result.stderr}", file=sys.stderr)
            return False
        # banana-maker saves as .jpg regardless of requested extension
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


def generate_cover(title: str, staging_dir: str, summary: str = "",
                   style: str = DEFAULT_STYLE) -> bool:
    """Generate a 1280x720 cover image."""
    output_path = str(Path(staging_dir) / "cover.png")
    builder = COVER_PROMPTS.get(style, COVER_PROMPTS[DEFAULT_STYLE])
    prompt = builder(title, summary)
    return _run_banana_maker(prompt, output_path, "16:9")


def generate_thumbnail(title: str, category: str, staging_dir: str,
                       style: str = DEFAULT_STYLE) -> bool:
    """Generate a 600x600 thumbnail image."""
    output_path = str(Path(staging_dir) / "thumbnail.png")
    builder = THUMBNAIL_PROMPTS.get(style, THUMBNAIL_PROMPTS[DEFAULT_STYLE])
    prompt = builder(title, category)
    return _run_banana_maker(prompt, output_path, "1:1")


def build_cover_command(title: str, staging_dir: str, summary: str = "",
                        style: str = DEFAULT_STYLE) -> list[str]:
    """Return the command that would generate a cover image (for testing/dry-run)."""
    output_path = str(Path(staging_dir) / "cover.png")
    builder = COVER_PROMPTS.get(style, COVER_PROMPTS[DEFAULT_STYLE])
    prompt = builder(title, summary)
    return [
        sys.executable, BANANA_MAKER, prompt,
        "--model", "flash",
        "--output", output_path,
        "--aspect-ratio", "16:9",
    ]


def build_thumbnail_command(title: str, category: str, staging_dir: str,
                            style: str = DEFAULT_STYLE) -> list[str]:
    """Return the command that would generate a thumbnail image (for testing/dry-run)."""
    output_path = str(Path(staging_dir) / "thumbnail.png")
    builder = THUMBNAIL_PROMPTS.get(style, THUMBNAIL_PROMPTS[DEFAULT_STYLE])
    prompt = builder(title, category)
    return [
        sys.executable, BANANA_MAKER, prompt,
        "--model", "flash",
        "--output", output_path,
        "--aspect-ratio", "1:1",
    ]
