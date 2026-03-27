#!/usr/bin/env python3
"""Swindle -- Storefront listing agent for ST Metro builds."""

import json
import sys
from pathlib import Path

import click

from db import SwindleDB
from image_generator import generate_cover, generate_thumbnail
from linkedin_post_generator import generate_linkedin_post
from listing_generator import generate_listing

PROJECT_DIR = Path(__file__).parent
STAGING_DIR = PROJECT_DIR / "staging"


def _repo_name_from_url(repo_url: str) -> str:
    """Extract repo name from GitHub URL."""
    # https://github.com/m2ai-portfolio/repo-name -> repo-name
    # https://github.com/m2ai-portfolio/repo-name.git -> repo-name
    name = repo_url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def _get_db() -> SwindleDB:
    """Get database instance, initialized."""
    db = SwindleDB(str(PROJECT_DIR / "data" / "swindle.db"))
    db.init_db()
    return db


@click.group()
def cli():
    """Swindle storefront listing agent."""
    pass


@cli.command()
@click.argument("repo_url")
@click.option("--spec-path", help="Path to app spec file")
@click.option("--project-dir", help="Path to local source code")
@click.option("--title", help="Product title")
@click.option("--dry-run", is_flag=True, help="Generate copy only, skip images")
def prepare(repo_url, spec_path, project_dir, title, dry_run):
    """Prepare a Gumroad listing package for a published repo."""
    repo_name = _repo_name_from_url(repo_url)

    if not title:
        # Derive title from repo name
        title = repo_name.replace("-", " ").replace("_", " ").title()

    staging_dir = STAGING_DIR / repo_name
    staging_dir.mkdir(parents=True, exist_ok=True)

    click.echo(f"Preparing listing for: {title}")
    click.echo(f"  Repo: {repo_url}")
    click.echo(f"  Staging: {staging_dir}")

    # Generate listing copy
    click.echo("\nGenerating listing copy...")
    try:
        listing_md, metadata = generate_listing(
            repo_url=repo_url,
            title=title,
            spec_path=spec_path,
            project_dir=project_dir,
        )
    except Exception as e:
        click.echo(f"Error generating listing: {e}", err=True)
        sys.exit(1)

    # Write listing files
    (staging_dir / "listing.md").write_text(listing_md, encoding="utf-8")
    (staging_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    click.echo(f"  listing.md written ({len(listing_md)} chars)")
    click.echo(f"  metadata.json written")

    # Generate images
    if dry_run:
        click.echo("\n[dry-run] Skipping image generation")
    else:
        click.echo("\nGenerating cover image (1280x720)...")
        if generate_cover(title, str(staging_dir)):
            click.echo("  cover.png generated")
        else:
            click.echo("  cover.png FAILED (listing still usable)", err=True)

        click.echo("Generating thumbnail (600x600)...")
        category = metadata.get("tags", ["developer-tools"])[0]
        if generate_thumbnail(title, category, str(staging_dir)):
            click.echo("  thumbnail.png generated")
        else:
            click.echo("  thumbnail.png FAILED (listing still usable)", err=True)

    # Track in DB
    db = _get_db()
    try:
        db.add_listing(
            repo_name=repo_name,
            repo_url=repo_url,
            title=title,
            spec_path=spec_path,
            staging_dir=str(staging_dir),
        )
    finally:
        db.close()

    click.echo(f"\nListing staged at: {staging_dir}")
    click.echo("Review the files, then run: python swindle.py approve " + repo_name)


@cli.command("list-staged")
def list_staged():
    """List all staged listing packages awaiting review."""
    db = _get_db()
    try:
        staged = db.get_staged()
    finally:
        db.close()

    if not staged:
        click.echo("No staged listings.")
        return

    click.echo(f"Staged listings ({len(staged)}):\n")
    for item in staged:
        click.echo(f"  {item['repo_name']}")
        click.echo(f"    Title: {item['title']}")
        click.echo(f"    Created: {item['created_at']}")
        click.echo(f"    Dir: {item['staging_dir']}")
        click.echo()


@cli.command()
@click.argument("repo_name")
def approve(repo_name):
    """Mark a staged listing as approved and generate LinkedIn draft."""
    db = _get_db()
    try:
        listing = db.get_by_name(repo_name)
        if not listing:
            click.echo(f"No listing found for: {repo_name}", err=True)
            sys.exit(1)
        if listing["status"] != "staged":
            click.echo(
                f"Listing is '{listing['status']}', not 'staged'", err=True
            )
            sys.exit(1)
        db.update_status(repo_name, "approved")

        click.echo(f"Approved: {repo_name}")

        # Auto-generate LinkedIn post draft if one doesn't exist
        draft_path = STAGING_DIR / repo_name / "linkedin_draft.md"
        if draft_path.exists():
            click.echo(f"\nLinkedIn draft already exists: {draft_path}")
        else:
            click.echo("\nGenerating LinkedIn post draft...")
            try:
                draft_content = generate_linkedin_post(repo_name)
                db.update_linkedin_draft_path(repo_name, str(draft_path))
                click.echo("\n--- LinkedIn Draft ---")
                click.echo(draft_content)
                click.echo("--- End Draft ---\n")
                click.echo(
                    f"LinkedIn draft saved to staging/{repo_name}/linkedin_draft.md"
                )
                click.echo(
                    "To post via Starscream, send the draft to "
                    "@m2ai_starscream_bot on Telegram"
                )
            except Exception as e:
                click.echo(
                    f"\nLinkedIn post generation failed: {e}", err=True
                )
    finally:
        db.close()


@cli.command()
@click.argument("repo_name")
@click.option("--gumroad-url", required=True, help="Gumroad product URL")
def publish(repo_name, gumroad_url):
    """Mark an approved listing as published with its Gumroad URL."""
    db = _get_db()
    try:
        listing = db.get_by_name(repo_name)
        if not listing:
            click.echo(f"No listing found for: {repo_name}", err=True)
            sys.exit(1)
        if listing["status"] != "approved":
            click.echo(
                f"Listing is '{listing['status']}', not 'approved'", err=True
            )
            sys.exit(1)

        db.update_status(repo_name, "published")
        db.update_gumroad_url(repo_name, gumroad_url)

        click.echo(f"Published: {repo_name}")
        click.echo(f"  Gumroad URL: {gumroad_url}")

        # Update LinkedIn draft to replace placeholder URL if draft exists
        draft_path = STAGING_DIR / repo_name / "linkedin_draft.md"
        if draft_path.exists():
            draft_content = draft_path.read_text(encoding="utf-8")
            updated = draft_content.replace("{{GUMROAD_URL}}", gumroad_url)
            if updated != draft_content:
                draft_path.write_text(updated, encoding="utf-8")
                click.echo("  LinkedIn draft updated with Gumroad URL")
            else:
                click.echo("  LinkedIn draft had no placeholder to update")
        else:
            click.echo("  No LinkedIn draft found to update")
    finally:
        db.close()


@cli.command("generate-post")
@click.argument("repo_name")
def generate_post(repo_name):
    """Generate a LinkedIn post draft from a staged listing package."""
    staging_dir = STAGING_DIR / repo_name
    if not staging_dir.exists():
        click.echo(f"Staging directory not found: {staging_dir}", err=True)
        sys.exit(1)

    click.echo(f"Generating LinkedIn post for: {repo_name}")
    try:
        draft = generate_linkedin_post(repo_name)
    except FileNotFoundError as e:
        click.echo(f"Missing file: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error generating post: {e}", err=True)
        sys.exit(1)

    click.echo(f"\n--- LinkedIn Draft ---")
    click.echo(draft)
    click.echo(f"--- End Draft ---\n")
    click.echo(f"Saved to: staging/{repo_name}/linkedin_draft.md")


@cli.command()
@click.argument("repo_name")
@click.option("--reason", default="", help="Rejection reason")
def reject(repo_name, reason):
    """Reject a staged listing (won't be re-generated)."""
    db = _get_db()
    try:
        listing = db.get_by_name(repo_name)
        if not listing:
            click.echo(f"No listing found for: {repo_name}", err=True)
            sys.exit(1)
        if listing["status"] != "staged":
            click.echo(
                f"Listing is '{listing['status']}', not 'staged'", err=True
            )
            sys.exit(1)
        db.update_status(repo_name, "rejected", reason)
    finally:
        db.close()

    click.echo(f"Rejected: {repo_name}" + (f" ({reason})" if reason else ""))


@cli.command()
def status():
    """Show Swindle status and stats."""
    db = _get_db()
    try:
        stats = db.get_stats()
        all_listings = db.get_all()
    finally:
        db.close()

    click.echo("Swindle Status")
    click.echo("=" * 40)
    click.echo(f"  Total listings: {stats.get('total', 0)}")
    click.echo(f"  Staged:         {stats.get('staged', 0)}")
    click.echo(f"  Approved:       {stats.get('approved', 0)}")
    click.echo(f"  Rejected:       {stats.get('rejected', 0)}")
    click.echo(f"  Published:      {stats.get('published', 0)}")

    if all_listings:
        click.echo(f"\nRecent listings:")
        for item in all_listings[:5]:
            click.echo(
                f"  [{item['status']:>9}] {item['repo_name']} - {item['title']}"
            )


if __name__ == "__main__":
    cli()
