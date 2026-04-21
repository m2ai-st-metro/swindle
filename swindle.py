#!/usr/bin/env python3
"""Swindle -- Storefront listing agent for ST Metro builds."""

import json
import shutil
import sys
from pathlib import Path
from typing import Optional

import click

from db import SwindleDB
from gumroad_api_client import (
    GumroadAPIClient,
    GumroadAPIError,
    ProductPlan,
)
from publish_plan import PlanError, PublishPlan, build_plan
from image_generator import generate_cover, generate_thumbnail
from linkedin_post_generator import generate_linkedin_post
from listing_generator import (
    generate_button_text,
    generate_features,
    generate_listing,
    generate_receipt_message,
    generate_short_title,
)
from validator import validate_listing

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
@click.option("--title", help="Product title (skips auto-shortening)")
@click.option("--no-short-title", is_flag=True,
              help="Skip LLM short-title generation and use titlecased repo slug")
@click.option("--dry-run", is_flag=True, help="Generate copy only, skip images")
def prepare(repo_url, spec_path, project_dir, title, no_short_title, dry_run):
    """Prepare a Gumroad listing package for a published repo."""
    repo_name = _repo_name_from_url(repo_url)
    fallback_title = repo_name.replace("-", " ").replace("_", " ").title()

    if title:
        click.echo(f"Using supplied title: {title}")
    elif no_short_title:
        title = fallback_title
        click.echo(f"Using repo-slug title (--no-short-title): {title}")
    else:
        click.echo("Generating short title...")
        try:
            title = generate_short_title(
                repo_name=repo_name,
                current_title=fallback_title,
                summary="",
                spec_path=spec_path,
            )
            click.echo(f"  short title: {title!r}")
        except Exception as e:
            click.echo(f"  short title FAILED: {e} (falling back to slug)", err=True)
            title = fallback_title

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




@cli.command()
@click.argument("repo_name")
def plan(repo_name):
    """Show the PublishPlan for a staged listing (no network)."""
    staging_dir = STAGING_DIR / repo_name
    if not staging_dir.is_dir():
        click.echo(f"Staging directory not found: {staging_dir}", err=True)
        sys.exit(1)

    try:
        p = build_plan(staging_dir, repo_name)
    except PlanError as e:
        click.echo(f"Cannot build plan: {e}", err=True)
        sys.exit(1)

    click.echo(f"=== PublishPlan for {repo_name} ===")
    click.echo(f"  title:           {p.title}")
    click.echo(f"  summary:         {p.summary}")
    click.echo(f"  price_cents:     {p.price_cents}")
    click.echo(f"  tags:            {p.tags}")
    click.echo(f"  description:     {len(p.description_md)} chars")
    click.echo(f"  features:        {len(p.features)} bullets")
    click.echo(f"  button_text:     {p.button_text!r}")
    click.echo(f"  receipt:         {len(p.receipt_message)} chars")
    click.echo(f"  cover:           {p.cover_path}")
    click.echo(f"  thumbnail:       {p.thumbnail_path}")
    click.echo(f"  content_file:    {p.content_file}")
    if not p.content_file:
        click.echo("  [warn] no content_file — publish will fail", err=True)


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
@click.option("--dry-run", is_flag=True, help="Show what would be published, no API calls")
@click.option("--sleep-seconds", type=int, default=6,
              help="Seconds between publishes (rate limit: 10/min)")
@click.option("--no-publish", is_flag=True,
              help="Create drafts only, do not enable")
def backfill(limit, dry_run, sleep_seconds, no_publish):
    """Publish every approved-but-unpublished listing via the Gumroad API.

    Skips listings whose validator fails (strict) or that lack content.zip.
    Sleeps between publishes to stay under Gumroad's 10 creates/min limit.
    """
    import time

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
    client: Optional[GumroadAPIClient] = None if dry_run else GumroadAPIClient()
    succeeded = 0
    skipped = 0
    failed = 0

    for i, row in enumerate(queue, start=1):
        repo_name = row["repo_name"]
        staging_dir = STAGING_DIR / repo_name
        click.echo(f"\n[{i}/{len(queue)}] {repo_name}")

        if not staging_dir.is_dir():
            click.echo(f"  skip: staging dir missing ({staging_dir})", err=True)
            skipped += 1
            continue

        failures = validate_listing(staging_dir)
        if failures:
            click.echo(f"  skip: validator failed ({len(failures)} issue(s))", err=True)
            for msg in failures[:3]:
                click.echo(f"    - {msg}", err=True)
            skipped += 1
            continue

        try:
            plan = build_plan(staging_dir, repo_name)
        except PlanError as e:
            click.echo(f"  skip: plan error: {e}", err=True)
            skipped += 1
            continue

        if not plan.content_file:
            click.echo("  skip: no content.zip", err=True)
            skipped += 1
            continue

        if dry_run:
            click.echo(f"  would publish: {plan.title}")
            click.echo(f"    content:     {plan.content_file.name} ({plan.content_file.stat().st_size} bytes)")
            click.echo(f"    tags:        {plan.tags}")
            continue

        try:
            product_id, product_url = _publish_via_api(
                plan=plan,
                client=client,
                cover_url=None,
                skip_publish=no_publish,
            )
        except GumroadAPIError as e:
            click.echo(f"  FAILED: {e}", err=True)
            if e.status_code:
                click.echo(f"    status: {e.status_code}", err=True)
            failed += 1
            continue

        db = _get_db()
        try:
            db.update_status(repo_name, "published")
            db.update_gumroad_url(repo_name, product_url)
        finally:
            db.close()
        click.echo(f"  published: {product_url}")
        succeeded += 1

        if i < len(queue):
            time.sleep(sleep_seconds)

    click.echo(
        f"\nDone. {succeeded} published, {skipped} skipped, {failed} failed."
    )


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


@cli.command()
@click.argument("repo_name")
@click.option("--new-title", default=None,
              help="Override with a specific title instead of calling the LLM")
@click.option("--dry-run", is_flag=True,
              help="Show proposed title without writing")
def retitle(repo_name, new_title, dry_run):
    """Regenerate a punchier product name for a staged listing.

    Reads staging/<repo>/metadata.json, calls the short-title LLM with the
    existing summary + spec, and rewrites metadata.title in place. Does
    not re-run the main listing or image generators. For already-published
    products this only updates the local staging copy; Gumroad won't see
    the new name until you republish.
    """
    staging_dir = STAGING_DIR / repo_name
    meta_path = staging_dir / "metadata.json"
    if not meta_path.exists():
        click.echo(f"metadata.json not found: {meta_path}", err=True)
        sys.exit(1)

    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        click.echo(f"metadata.json invalid JSON: {e}", err=True)
        sys.exit(1)

    old_title = metadata.get("title") or repo_name
    summary = metadata.get("summary") or ""
    spec_path = metadata.get("spec_path")

    if new_title:
        proposed = new_title.strip()
        source = "override"
    else:
        click.echo(f"Generating short title for {repo_name}...")
        try:
            proposed = generate_short_title(
                repo_name=repo_name,
                current_title=old_title,
                summary=summary,
                spec_path=spec_path,
            )
            source = "LLM"
        except Exception as e:
            click.echo(f"Generation failed: {e}", err=True)
            sys.exit(1)

    click.echo(f"  old: {old_title!r}")
    click.echo(f"  new: {proposed!r}  (source: {source})")

    if proposed == old_title:
        click.echo("Title is unchanged. Nothing to write.")
        return

    if dry_run:
        click.echo("--dry-run: metadata.json not modified.")
        return

    metadata["title"] = proposed
    meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    click.echo(f"Updated {meta_path}")

    # Keep the DB title in sync for list-staged / status output
    db = _get_db()
    try:
        listing = db.get_by_name(repo_name)
        if listing:
            db.conn.execute(
                "UPDATE listings SET title = ? WHERE repo_name = ?",
                (proposed, repo_name),
            )
            db.conn.commit()
            click.echo("DB title updated.")
    finally:
        db.close()


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


def _plan_to_product_plan(
    plan: PublishPlan,
    file_url: Optional[str],
) -> ProductPlan:
    """Translate a staging PublishPlan into the API ProductPlan payload."""
    files: list[dict] = []
    if file_url and plan.content_file:
        files.append({
            "url": file_url,
            "display_name": plan.title,
            "extension": plan.content_file.suffix.lstrip(".") or "zip",
            "position": 0,
        })

    # Append the feature bullets under an "Includes" section; Gumroad's
    # dedicated features list is only writable via the web UI today.
    description = plan.description_md.rstrip()
    if plan.features:
        description += "\n\n## Includes\n\n"
        description += "\n".join(f"- {f}" for f in plan.features)

    return ProductPlan(
        name=plan.title,
        description=description,
        price_cents=plan.price_cents,
        tags=plan.tags,
        custom_summary=plan.summary,
        custom_receipt=plan.receipt_message,
        files=files,
    )


def _publish_via_api(
    plan: PublishPlan,
    client: GumroadAPIClient,
    cover_url: Optional[str],
    skip_publish: bool,
) -> tuple[str, str]:
    """Run the API publish flow. Returns (product_id, product_url).

    Raises GumroadAPIError on any step failure. The caller is responsible
    for DB bookkeeping and for optionally calling delete_product on rollback.
    """
    file_url = None
    if plan.content_file:
        click.echo(f"  uploading {plan.content_file.name} ({plan.content_file.stat().st_size} bytes)...")
        file_url = client.upload_file(plan.content_file)
        click.echo(f"    file_url: {file_url}")

    api_plan = _plan_to_product_plan(plan, file_url)
    click.echo("  creating product...")
    ref = client.create_product(api_plan)
    click.echo(f"    product_id: {ref.id}")

    if api_plan.custom_receipt:
        click.echo("  updating product (custom_receipt)...")
        client.update_product(ref.id, custom_receipt=api_plan.custom_receipt)

    if cover_url:
        click.echo(f"  attaching cover: {cover_url}")
        try:
            client.add_cover_url(ref.id, cover_url)
        except GumroadAPIError as e:
            click.echo(f"    cover attach FAILED: {e} (continuing)", err=True)

    if skip_publish:
        click.echo("  --no-publish: leaving product as draft")
        return ref.id, ref.url

    click.echo("  publishing...")
    client.publish(ref.id)
    return ref.id, ref.url


@cli.command()
@click.argument("repo_name")
@click.option("--cover-url", default=None,
              help="Public URL for cover image (thumbnail is API-blocked)")
@click.option("--no-publish", is_flag=True,
              help="Create product and upload file but leave as draft")
@click.option("--skip-validate", is_flag=True,
              help="Skip validator (only for debugging)")
def publish(repo_name, cover_url, no_publish, skip_validate):
    """Auto-publish a staged listing via the Gumroad API.

    Runs the validator (strict), uploads content.zip, creates the product,
    attaches the cover if a URL is supplied, and enables the product. On
    success, records the Gumroad URL in the DB and refreshes the LinkedIn
    draft.
    """
    staging_dir = STAGING_DIR / repo_name
    if not staging_dir.is_dir():
        click.echo(f"Staging directory not found: {staging_dir}", err=True)
        sys.exit(1)

    if not skip_validate:
        click.echo("Running validator...")
        failures = validate_listing(staging_dir)
        if failures:
            click.echo(f"  validator found {len(failures)} issue(s):", err=True)
            for msg in failures:
                click.echo(f"    - {msg}", err=True)
            click.echo(
                "Aborting publish. Fix issues or pass --skip-validate.", err=True
            )
            sys.exit(1)
        click.echo("  passed")

    try:
        plan = build_plan(staging_dir, repo_name)
    except PlanError as e:
        click.echo(f"Cannot build plan: {e}", err=True)
        sys.exit(1)

    if not plan.content_file:
        click.echo(
            "No content.zip resolved. Add one to the staging dir or run "
            "`swindle prepare` with --project-dir pointing at a repo that "
            "has gumroad.yaml or dist/*.zip.",
            err=True,
        )
        sys.exit(1)

    click.echo(f"\nPublishing {repo_name} to Gumroad via API")
    client = GumroadAPIClient()
    try:
        product_id, product_url = _publish_via_api(
            plan=plan,
            client=client,
            cover_url=cover_url,
            skip_publish=no_publish,
        )
    except GumroadAPIError as e:
        click.echo(f"\n  FAILED: {e}", err=True)
        if e.status_code:
            click.echo(f"    status: {e.status_code}", err=True)
        if e.body:
            click.echo(f"    body: {e.body}", err=True)
        sys.exit(1)

    click.echo(f"\n  product_id:  {product_id}")
    click.echo(f"  product_url: {product_url}")

    db = _get_db()
    try:
        listing = db.get_by_name(repo_name)
        if listing and listing["status"] != "published":
            db.update_status(repo_name, "published")
        if product_url:
            db.update_gumroad_url(repo_name, product_url)
    finally:
        db.close()

    # Refresh LinkedIn draft placeholder if present
    draft_path = STAGING_DIR / repo_name / "linkedin_draft.md"
    if draft_path.exists() and product_url:
        draft_content = draft_path.read_text(encoding="utf-8")
        updated = draft_content.replace("{{GUMROAD_URL}}", product_url)
        if updated != draft_content:
            draft_path.write_text(updated, encoding="utf-8")
            click.echo("  LinkedIn draft updated with Gumroad URL")


@cli.command("mark-published")
@click.argument("repo_name")
@click.option("--gumroad-url", required=True, help="Gumroad product URL")
def mark_published(repo_name, gumroad_url):
    """Manually mark an approved listing as published (no API call).

    Kept as an escape hatch for products published via the web UI.
    """
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

        click.echo(f"Marked published: {repo_name}")
        click.echo(f"  Gumroad URL: {gumroad_url}")

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
