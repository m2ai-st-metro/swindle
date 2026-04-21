#!/usr/bin/env python3
"""Swindle -- Storefront listing agent for ST Metro builds."""

import json
import shutil
import sys
from pathlib import Path
from typing import Optional

import click

from db import SwindleDB
from gumroad_publisher import GumroadPublisher, PlanError, build_plan
from image_generator import generate_cover, generate_thumbnail
from linkedin_post_generator import generate_linkedin_post
from listing_generator import (
    generate_button_text,
    generate_features,
    generate_listing,
    generate_receipt_message,
)
from validator import validate_listing

PLAYWRIGHT_PROFILE_DIR = Path(__file__).parent / "data" / "playwright-profile"

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


def _resolve_content_file(
    staging_dir: Path,
    project_dir: str | None,
) -> Optional[Path]:
    """Resolve the downloadable package to upload to Gumroad's Content tab.

    Resolution order:
      1. project_dir/gumroad.yaml has `content_file: <rel-path>` -> copy to staging/content.zip
      2. project_dir/dist/*.zip -> pick newest, copy to staging/content.zip
      3. staging_dir/content.zip already exists (manually dropped) -> use it
      4. Otherwise None (warn; Playwright step can fail loudly later).
    """
    staged_zip = staging_dir / "content.zip"

    if project_dir:
        proj = Path(project_dir).expanduser()
        yaml_path = proj / "gumroad.yaml"
        if yaml_path.exists():
            try:
                import yaml
                data = yaml.safe_load(yaml_path.read_text()) or {}
                rel = data.get("content_file")
                if rel:
                    src = proj / rel
                    if src.exists():
                        shutil.copy2(src, staged_zip)
                        return staged_zip
            except ImportError:
                pass

        dist = proj / "dist"
        if dist.is_dir():
            zips = sorted(dist.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
            if zips:
                shutil.copy2(zips[0], staged_zip)
                return staged_zip

    if staged_zip.exists():
        return staged_zip

    return None


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

    # Resolve downloadable content file (the .zip buyers get from Gumroad)
    content_path = _resolve_content_file(staging_dir, project_dir)
    if content_path:
        metadata["content_file"] = str(content_path)
        click.echo(f"  content.zip resolved: {content_path.name} ({content_path.stat().st_size} bytes)")
    else:
        metadata["content_file"] = None
        click.echo(
            "  content.zip NOT resolved (drop one into staging/<repo>/content.zip "
            "manually, or add gumroad.yaml/dist to project_dir)",
            err=True,
        )

    # Write listing files
    (staging_dir / "listing.md").write_text(listing_md, encoding="utf-8")
    (staging_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    click.echo(f"  listing.md written ({len(listing_md)} chars)")
    click.echo(f"  metadata.json written")

    # Generate Gumroad tab fields: features, button text, receipt message
    click.echo("\nGenerating Gumroad field copy...")
    try:
        features = generate_features(
            repo_url=repo_url, title=title, spec_path=spec_path,
            project_dir=project_dir,
        )
        (staging_dir / "features.txt").write_text(
            "\n".join(features) + "\n", encoding="utf-8"
        )
        click.echo(f"  features.txt written ({len(features)} bullets)")
    except Exception as e:
        click.echo(f"  features.txt FAILED: {e} (listing still usable)", err=True)

    try:
        button = generate_button_text(
            repo_url=repo_url, title=title, spec_path=spec_path,
        )
        (staging_dir / "button_text.txt").write_text(
            button + "\n", encoding="utf-8"
        )
        click.echo(f"  button_text.txt written ({button!r})")
    except Exception as e:
        click.echo(f"  button_text.txt FAILED: {e} (listing still usable)", err=True)

    try:
        receipt = generate_receipt_message(
            repo_url=repo_url, title=title, spec_path=spec_path,
        )
        (staging_dir / "receipt_message.md").write_text(
            receipt + "\n", encoding="utf-8"
        )
        click.echo(f"  receipt_message.md written ({len(receipt)} chars)")
    except Exception as e:
        click.echo(f"  receipt_message.md FAILED: {e} (listing still usable)", err=True)

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

    # Quality gate (warn-only; doesn't block staging)
    click.echo("\nRunning quality gate...")
    failures = validate_listing(staging_dir)
    if failures:
        click.echo(f"  {len(failures)} issue(s) found:", err=True)
        for msg in failures:
            click.echo(f"    - {msg}", err=True)
        click.echo(
            "  (listing still staged; fix issues before approving)",
            err=True,
        )
    else:
        click.echo("  passed")

    click.echo(f"\nListing staged at: {staging_dir}")
    click.echo("Review the files, then run: python swindle.py approve " + repo_name)


@cli.command("capture-selectors")
@click.option("--output-file", type=click.Path(), default=None,
              help="Where codegen writes the captured Python (default: /tmp/swindle_codegen_*.py)")
@click.option("--apply/--no-apply", default=True,
              help="After parsing, overwrite gumroad_selectors.py with captured selectors")
def capture_selectors_cmd(output_file, apply):
    """Wrap `playwright codegen` to capture real Gumroad selectors.

    Opens the Playwright Inspector. Walk through the new-product flow using
    the sentinel values printed to the terminal. When you close the
    Inspector, the captured Python is parsed and the matched selectors are
    written to gumroad_selectors.py (with a timestamped backup).
    """
    from pathlib import Path

    from capture_selectors import (
        WALKTHROUGH,
        apply_captures,
        parse_capture,
        run_codegen,
    )

    click.echo(WALKTHROUGH)
    click.confirm("Ready to launch Playwright Inspector?", default=True, abort=True)

    out_path = Path(output_file) if output_file else None
    out_path = run_codegen(output_path=out_path)
    click.echo(f"\nCodegen wrote capture to: {out_path}")

    if not out_path.exists():
        click.echo("No capture file produced. Aborting.", err=True)
        sys.exit(1)

    result = parse_capture(out_path)
    click.echo(f"\nMatched {len(result.captures)} selector(s):")
    for cap in result.captures:
        click.echo(f"  {cap.name:30} via {cap.matched_via:16} -> {cap.locator_expr}")
    if result.unmatched_sentinels:
        click.echo(f"\n{len(result.unmatched_sentinels)} sentinel(s) not found (field not filled or different value):", err=True)
        for s in result.unmatched_sentinels:
            click.echo(f"  {s}", err=True)
    if result.unmatched_clicks:
        click.echo(f"\n{len(result.unmatched_clicks)} click target(s) not found:", err=True)
        for c in result.unmatched_clicks:
            click.echo(f"  {c}", err=True)

    if not result.captures:
        click.echo("\nNothing to apply. Exiting.", err=True)
        sys.exit(1)

    if not apply:
        click.echo("\n--no-apply given; gumroad_selectors.py NOT modified.")
        return

    if not click.confirm("\nApply these captures to gumroad_selectors.py?", default=True):
        click.echo("Skipped. gumroad_selectors.py unchanged.")
        return

    sel_path = Path(__file__).parent / "gumroad_selectors.py"
    diffs = apply_captures(result.captures, sel_path, backup=True)
    click.echo(f"\nRewrote {len(diffs)} selector(s) in {sel_path}")
    for name, (old, new) in diffs.items():
        click.echo(f"  {name}")
        click.echo(f"    old: {old[:80]}")
        click.echo(f"    new: {new}")
    click.echo("\nBackup saved alongside gumroad_selectors.py (.bak-<timestamp>).")


@cli.command()
@click.argument("repo_name")
@click.option("--dry-run", is_flag=True, help="Print plan without opening browser")
@click.option("--headful/--headless", default=True, help="Show browser (recommended for first runs)")
@click.option("--first-login", is_flag=True, help="Open browser and pause for manual Gumroad login")
def push(repo_name, dry_run, headful, first_login):
    """Publish a staged listing as a Gumroad draft via Playwright."""
    staging_dir = STAGING_DIR / repo_name
    if not staging_dir.is_dir():
        click.echo(f"Staging directory not found: {staging_dir}", err=True)
        sys.exit(1)

    if first_login:
        click.echo("Opening browser for manual Gumroad login...")
        click.echo("Log in, dismiss any prompts, then close the browser tab.")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(PLAYWRIGHT_PROFILE_DIR),
                headless=False,
            )
            ctx.new_page().goto("https://app.gumroad.com/login")
            click.echo("\nPress Enter when login is complete to close...")
            input()
            ctx.close()
        click.echo("Session saved. Re-run `swindle push <repo>` without --first-login.")
        return

    try:
        plan = build_plan(staging_dir, repo_name)
    except PlanError as e:
        click.echo(f"Cannot build plan: {e}", err=True)
        sys.exit(1)

    if dry_run:
        click.echo(f"=== PublishPlan for {repo_name} ===")
        click.echo(f"  title:           {plan.title}")
        click.echo(f"  summary:         {plan.summary}")
        click.echo(f"  price_cents:     {plan.price_cents}")
        click.echo(f"  tags:            {plan.tags}")
        click.echo(f"  description:     {len(plan.description_md)} chars")
        click.echo(f"  features:        {len(plan.features)} bullets")
        click.echo(f"  button_text:     {plan.button_text!r}")
        click.echo(f"  receipt:         {len(plan.receipt_message)} chars")
        click.echo(f"  cover:           {plan.cover_path}")
        click.echo(f"  thumbnail:       {plan.thumbnail_path}")
        click.echo(f"  content_file:    {plan.content_file}")
        if not plan.content_file:
            click.echo("  [warn] no content_file — real push would fail here", err=True)
        if not plan.cover_path or not plan.thumbnail_path:
            click.echo("  [warn] missing image(s) — real push will leave tab empty", err=True)
        return

    click.echo(f"Publishing draft to Gumroad: {plan.title}")
    publisher = GumroadPublisher(
        profile_dir=PLAYWRIGHT_PROFILE_DIR,
        headless=not headful,
    )
    result = publisher.publish_draft(plan)

    if result.succeeded:
        click.echo(f"\n  draft saved: {result.gumroad_url}")
        click.echo(f"  product id:  {result.gumroad_product_id}")
        click.echo(f"  tabs:        {result.tabs_completed}")
        db = _get_db()
        try:
            db.update_gumroad_url(repo_name, result.gumroad_url)
        finally:
            db.close()
    else:
        click.echo(f"\n  FAILED at: {result.failed_at}", err=True)
        click.echo(f"  error:     {result.error}", err=True)
        click.echo(f"  completed: {result.tabs_completed}", err=True)
        if result.debug_dir:
            click.echo(f"  debug:     {result.debug_dir}", err=True)
        sys.exit(1)


def _ensure_fields_for_staging(
    staging_dir: Path,
    repo_name: str,
    repo_url: str,
    title: str,
    spec_path: Optional[str],
    project_dir: Optional[str],
) -> list[str]:
    """Regenerate any missing new-field files in a staging dir.

    Returns list of filenames that were (re)generated. Non-missing files are
    left alone — backfill does not overwrite existing work. If Matthew wants
    a full tone refresh, run `swindle prepare` directly.
    """
    generated = []
    if not (staging_dir / "features.txt").exists():
        features = generate_features(
            repo_url=repo_url, title=title, spec_path=spec_path, project_dir=project_dir,
        )
        (staging_dir / "features.txt").write_text("\n".join(features) + "\n", encoding="utf-8")
        generated.append("features.txt")
    if not (staging_dir / "button_text.txt").exists():
        button = generate_button_text(repo_url=repo_url, title=title, spec_path=spec_path)
        (staging_dir / "button_text.txt").write_text(button + "\n", encoding="utf-8")
        generated.append("button_text.txt")
    if not (staging_dir / "receipt_message.md").exists():
        receipt = generate_receipt_message(repo_url=repo_url, title=title, spec_path=spec_path)
        (staging_dir / "receipt_message.md").write_text(receipt + "\n", encoding="utf-8")
        generated.append("receipt_message.md")
    return generated


@cli.command()
@click.option("--limit", type=int, default=None, help="Max listings to process")
@click.option("--dry-run", is_flag=True, help="Show what would be pushed, no browser")
@click.option("--headful/--headless", default=True)
def backfill(limit, dry_run, headful):
    """Push every approved-but-unpublished listing to Gumroad as a draft.

    For each listing: regenerate any missing new-field files (features/
    button/receipt), build the plan, and push via Playwright. On success,
    records the gumroad_url in the DB.
    """
    db = _get_db()
    try:
        queue = db.get_approved_unpublished()
    finally:
        db.close()

    if not queue:
        click.echo("No approved-unpublished listings. Nothing to backfill.")
        return

    if limit:
        queue = queue[:limit]

    click.echo(f"Backfill queue: {len(queue)} listing(s)")
    for i, row in enumerate(queue, start=1):
        repo_name = row["repo_name"]
        staging_dir = STAGING_DIR / repo_name
        click.echo(f"\n[{i}/{len(queue)}] {repo_name}")

        if not staging_dir.is_dir():
            click.echo(f"  skip: staging dir missing ({staging_dir})", err=True)
            continue

        if dry_run:
            missing = [
                name for name in ("features.txt", "button_text.txt", "receipt_message.md")
                if not (staging_dir / name).exists()
            ]
            if missing:
                click.echo(f"  would regenerate: {missing}")
            else:
                click.echo("  new fields already present")
            for img in ("cover.png", "thumbnail.png"):
                exists = (staging_dir / img).exists() and (staging_dir / img).stat().st_size > 0
                click.echo(f"  {img}: {'present' if exists else 'MISSING'}")
            content = staging_dir / "content.zip"
            click.echo(f"  content.zip: {'present' if content.exists() else 'MISSING (real push fails)'}")
            continue

        click.echo("  ensuring new-field files...")
        try:
            generated = _ensure_fields_for_staging(
                staging_dir=staging_dir,
                repo_name=repo_name,
                repo_url=row["repo_url"],
                title=row["title"],
                spec_path=row["spec_path"],
                project_dir=None,
            )
            if generated:
                click.echo(f"  regenerated: {generated}")
            else:
                click.echo("  all new fields already present")
        except Exception as e:
            click.echo(f"  field regen FAILED: {e}", err=True)
            continue

        try:
            plan = build_plan(staging_dir, repo_name)
        except PlanError as e:
            click.echo(f"  plan error: {e}", err=True)
            continue

        publisher = GumroadPublisher(
            profile_dir=PLAYWRIGHT_PROFILE_DIR,
            headless=not headful,
        )
        result = publisher.publish_draft(plan)

        if result.succeeded:
            click.echo(f"  draft saved: {result.gumroad_url}")
            db = _get_db()
            try:
                db.update_gumroad_url(repo_name, result.gumroad_url)
            finally:
                db.close()
        else:
            click.echo(f"  FAILED at {result.failed_at}: {result.error}", err=True)
            click.echo(f"  debug: {result.debug_dir}", err=True)


@cli.command()
@click.argument("repo_name")
def validate(repo_name):
    """Re-run the quality gate against an existing staged listing."""
    staging_dir = STAGING_DIR / repo_name
    if not staging_dir.is_dir():
        click.echo(f"Staging directory not found: {staging_dir}", err=True)
        sys.exit(1)

    failures = validate_listing(staging_dir)
    if failures:
        click.echo(f"{repo_name}: {len(failures)} issue(s)", err=True)
        for msg in failures:
            click.echo(f"  - {msg}", err=True)
        sys.exit(1)

    click.echo(f"{repo_name}: passed")


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
