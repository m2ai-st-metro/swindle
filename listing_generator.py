"""Listing copy generator using DeepInfra Mistral Small."""

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader

load_dotenv(os.path.expanduser("~/.env.shared"))

PROJECT_DIR = Path(__file__).parent
TEMPLATE_DIR = PROJECT_DIR / "templates"

DEEPINFRA_API_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
MODEL = "mistralai/Mistral-Small-3.2-24B-Instruct-2506"


def _get_file_tree(project_dir: str, max_depth: int = 3) -> str:
    """Get a file tree of the project directory."""
    if not project_dir or not Path(project_dir).exists():
        return "(no local source provided)"

    try:
        result = subprocess.run(
            ["find", project_dir, "-maxdepth", str(max_depth),
             "-not", "-path", "*/node_modules/*",
             "-not", "-path", "*/.git/*",
             "-not", "-path", "*/venv/*",
             "-not", "-path", "*/__pycache__/*"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() or "(empty)"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "(could not read file tree)"


def _read_spec(spec_path: str | None) -> str:
    """Read the app spec file."""
    if not spec_path:
        return "(no spec provided)"
    path = Path(spec_path)
    if not path.exists():
        return f"(spec not found: {spec_path})"
    return path.read_text(encoding="utf-8")[:8000]


SYSTEM_PROMPT = (
    "You are a copywriter for developer tools sold on Gumroad. Write in plain "
    "speak — how a dev would describe the tool to a friend, not how marketing "
    "would describe it. Lead with concrete pain or outcomes. Use specific "
    "claims ('saves 2 hours/week', 'Python 3.11+, no external services') not "
    "empty hype ('revolutionary', 'seamless', 'next-generation'). The reader "
    "is a developer — earn trust through specificity, not superlatives."
)


def _call_llm(prompt: str, max_tokens: int = 2000, temperature: float = 0.7) -> str:
    """Call DeepInfra Mistral Small for listing generation."""
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
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _parse_metadata(llm_output: str, title: str, repo_url: str) -> dict:
    """Extract summary and tags from LLM output."""
    summary = title
    tags = ["developer-tools"]

    # Extract SUMMARY line
    summary_match = re.search(r"SUMMARY:\s*(.+)", llm_output)
    if summary_match:
        summary = summary_match.group(1).strip()[:140]

    # Extract TAGS line
    tags_match = re.search(r"TAGS:\s*(.+)", llm_output)
    if tags_match:
        tags = [t.strip() for t in tags_match.group(1).split(",")]

    return {
        "title": title,
        "price": 0,
        "tags": tags,
        "summary": summary,
        "repo_url": repo_url,
        "generated_at": datetime.now().isoformat(),
    }


def _clean_listing(llm_output: str) -> str:
    """Remove SUMMARY/TAGS metadata lines from listing body."""
    lines = llm_output.split("\n")
    cleaned = [
        line for line in lines
        if not re.match(r"^(SUMMARY|TAGS):", line.strip())
    ]
    # Strip trailing separator
    text = "\n".join(cleaned).strip()
    if text.endswith("---"):
        text = text[:-3].strip()
    return text


def _render(template_name: str, **ctx: object) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    return env.get_template(template_name).render(**ctx)


def generate_listing(
    repo_url: str,
    title: str,
    spec_path: str | None = None,
    project_dir: str | None = None,
) -> tuple[str, dict]:
    """Generate listing copy and metadata.

    Returns:
        Tuple of (listing_markdown, metadata_dict)
    """
    spec_text = _read_spec(spec_path)
    file_tree = _get_file_tree(project_dir)

    prompt = _render(
        "listing_prompt.txt",
        title=title,
        repo_url=repo_url,
        spec_text=spec_text,
        file_tree=file_tree,
    )

    llm_output = _call_llm(prompt)
    listing_md = _clean_listing(llm_output)
    metadata = _parse_metadata(llm_output, title, repo_url)

    return listing_md, metadata


def generate_features(
    repo_url: str,
    title: str,
    spec_path: str | None = None,
    project_dir: str | None = None,
) -> list[str]:
    """Generate 3-5 concrete feature bullets for Gumroad's Features field.

    Returns a list of feature strings (no bullet markers, no blank lines).
    """
    prompt = _render(
        "features_prompt.txt",
        title=title,
        repo_url=repo_url,
        spec_text=_read_spec(spec_path),
        file_tree=_get_file_tree(project_dir),
    )
    output = _call_llm(prompt, max_tokens=400, temperature=0.5)
    return _parse_features(output)


def generate_button_text(
    repo_url: str,
    title: str,
    spec_path: str | None = None,
) -> str:
    """Generate 2-4 word verb-led button text (<=25 chars)."""
    prompt = _render(
        "button_text_prompt.txt",
        title=title,
        repo_url=repo_url,
        spec_text=_read_spec(spec_path)[:2000],
    )
    output = _call_llm(prompt, max_tokens=30, temperature=0.5)
    return _parse_button_text(output)


def generate_receipt_message(
    repo_url: str,
    title: str,
    spec_path: str | None = None,
) -> str:
    """Generate 1-2 sentence thank-you message for the Gumroad receipt."""
    prompt = _render(
        "receipt_message_prompt.txt",
        title=title,
        repo_url=repo_url,
        spec_text=_read_spec(spec_path)[:2000],
    )
    output = _call_llm(prompt, max_tokens=200, temperature=0.6)
    return _parse_receipt_message(output)


BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def _parse_features(llm_output: str) -> list[str]:
    """Parse feature bullets from LLM output. Strips bullet markers and blanks."""
    lines = []
    for raw in llm_output.strip().split("\n"):
        line = BULLET_RE.sub("", raw).strip()
        if not line:
            continue
        if line.lower().startswith(("output:", "features:", "example", "here are")):
            continue
        lines.append(line)
    return lines[:5]


PREFIX_RE = re.compile(r"^(?:output|button(?:\s+text)?|example)\s*:\s*", re.IGNORECASE)


def _parse_button_text(llm_output: str) -> str:
    """Parse button text from LLM output. Strips prefix labels, quotes, trailing punct."""
    for raw in llm_output.strip().split("\n"):
        line = PREFIX_RE.sub("", raw.strip()).strip()
        if not line:
            continue
        if line.lower().startswith(("example", "here are", "here is")):
            continue
        prev = ""
        while line != prev:
            prev = line
            line = line.strip('"\'').rstrip(".!,;:").strip()
        return line
    return ""


def _parse_receipt_message(llm_output: str) -> str:
    """Parse receipt message from LLM output. Strips prefix lines and surrounding whitespace."""
    lines = llm_output.strip().split("\n")
    filtered = []
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith(("output:", "message:", "example", "dear ")):
            continue
        filtered.append(line)
    return "\n".join(filtered).strip()
