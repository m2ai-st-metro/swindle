"""LinkedIn post generator using DeepInfra Nemotron-3."""

import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader

load_dotenv(os.path.expanduser("~/.env.shared"))

PROJECT_DIR = Path(__file__).parent
TEMPLATE_DIR = PROJECT_DIR / "templates"
STAGING_DIR = PROJECT_DIR / "staging"

DEEPINFRA_API_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
MODEL = "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"


def _call_llm(prompt: str) -> str:
    """Call DeepInfra Nemotron-3 for LinkedIn post generation."""
    api_key = os.environ.get("DEEPINFRA_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPINFRA_API_KEY not found in ~/.env.shared")

    resp = requests.post(
        DEEPINFRA_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a LinkedIn ghostwriter for a developer who ships "
                        "free tools. Write punchy, technical posts that follow the "
                        "exact structure given. No fluff. No emojis. No hashtags."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 1500,
            "temperature": 0.7,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def generate_linkedin_post(repo_name: str) -> str:
    """Generate a LinkedIn post draft from a staged listing package.

    Args:
        repo_name: Name of the staging directory (e.g. "my-tool")

    Returns:
        The generated post text.

    Raises:
        FileNotFoundError: If staging dir or required files are missing.
        RuntimeError: If the LLM call fails.
    """
    staging_dir = STAGING_DIR / repo_name

    listing_path = staging_dir / "listing.md"
    metadata_path = staging_dir / "metadata.json"

    if not staging_dir.exists():
        raise FileNotFoundError(f"Staging directory not found: {staging_dir}")
    if not listing_path.exists():
        raise FileNotFoundError(f"listing.md not found in {staging_dir}")
    if not metadata_path.exists():
        raise FileNotFoundError(f"metadata.json not found in {staging_dir}")

    listing_text = listing_path.read_text(encoding="utf-8")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("linkedin_prompt.txt")

    prompt = template.render(
        title=metadata.get("title", repo_name),
        repo_name=repo_name,
        listing_text=listing_text,
        metadata_json=json.dumps(metadata, indent=2),
    )

    draft = _call_llm(prompt)

    # Strip any wrapping markdown code fences the LLM might add
    draft = draft.strip()
    if draft.startswith("```"):
        lines = draft.split("\n")
        # Remove first and last lines if they are fences
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        draft = "\n".join(lines).strip()

    # Save draft
    output_path = staging_dir / "linkedin_draft.md"
    output_path.write_text(draft, encoding="utf-8")

    return draft
