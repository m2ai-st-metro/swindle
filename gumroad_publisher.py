"""Gumroad publisher — reads a staging/<repo>/ dir and drives the Gumroad
web UI via Playwright to create a draft product with all 8 tabs populated.

Design
------
Two-phase publish:
  1. build_plan(repo_name) -> PublishPlan
     Reads staging files only. No browser. Fails fast on missing content.
  2. publish_draft(plan) -> PublishResult
     Opens persistent Chromium context, walks tab-by-tab, saves as draft.
     On any step failure, dumps screenshot + DOM to staging/<repo>/_debug/.

Persistent session lives at data/playwright-profile/. First-run login is
manual (see README). Subsequent runs reuse the cookie jar.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import gumroad_selectors as S


# ---------------------------------------------------------------------------
# Data: what a ready-to-publish plan looks like
# ---------------------------------------------------------------------------


@dataclass
class PublishPlan:
    """Everything Playwright needs to populate a Gumroad draft product."""

    repo_name: str
    title: str
    summary: str
    price_cents: int
    tags: list[str]
    description_md: str
    features: list[str]
    button_text: str
    receipt_message: str
    cover_path: Optional[Path]
    thumbnail_path: Optional[Path]
    content_file: Optional[Path]
    staging_dir: Path

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("cover_path", "thumbnail_path", "content_file", "staging_dir"):
            d[k] = str(d[k]) if d[k] else None
        return d


@dataclass
class PublishResult:
    """Outcome of a publish_draft() call."""

    repo_name: str
    gumroad_product_id: Optional[str] = None
    gumroad_url: Optional[str] = None
    tabs_completed: list[str] = field(default_factory=list)
    failed_at: Optional[str] = None
    error: Optional[str] = None
    debug_dir: Optional[Path] = None

    @property
    def succeeded(self) -> bool:
        return self.failed_at is None and self.gumroad_product_id is not None


# ---------------------------------------------------------------------------
# Phase 1: plan (no browser)
# ---------------------------------------------------------------------------


class PlanError(Exception):
    """Raised when staging files are missing or unreadable."""


def build_plan(staging_dir: Path, repo_name: str) -> PublishPlan:
    """Assemble a PublishPlan from a staging/<repo>/ directory.

    Raises PlanError if any required file is missing or malformed. Does not
    touch the network or browser.
    """
    sd = Path(staging_dir)
    if not sd.is_dir():
        raise PlanError(f"staging dir not found: {sd}")

    def _read_required(name: str) -> str:
        path = sd / name
        if not path.exists():
            raise PlanError(f"required file missing: {path}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise PlanError(f"required file empty: {path}")
        return text

    description_md = _read_required("listing.md")

    meta_path = sd / "metadata.json"
    if not meta_path.exists():
        raise PlanError(f"metadata.json missing: {meta_path}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PlanError(f"metadata.json invalid JSON: {e}") from e

    summary = (meta.get("summary") or "").strip()
    if not summary:
        raise PlanError("metadata.summary is empty")
    tags = meta.get("tags") or []
    if not isinstance(tags, list) or len(tags) < 3:
        raise PlanError(f"metadata.tags needs >=3 tags, got {tags!r}")
    title = (meta.get("title") or "").strip() or repo_name
    price_cents = int(meta.get("price", 0) or 0) * 100  # schema is dollars

    features = [
        ln.strip() for ln in _read_required("features.txt").splitlines() if ln.strip()
    ]
    if len(features) < 3:
        raise PlanError(f"features.txt needs >=3 bullets, got {len(features)}")

    button_text = _read_required("button_text.txt")
    receipt_message = _read_required("receipt_message.md")

    cover = sd / "cover.png"
    thumbnail = sd / "thumbnail.png"
    content = sd / "content.zip"
    # content.zip is optional here (publisher will fail loudly if missing),
    # but metadata.content_file can also point outside the staging dir.
    content_file = None
    if content.exists():
        content_file = content
    elif meta.get("content_file"):
        candidate = Path(meta["content_file"])
        if candidate.exists():
            content_file = candidate

    return PublishPlan(
        repo_name=repo_name,
        title=title,
        summary=summary,
        price_cents=price_cents,
        tags=tags,
        description_md=description_md,
        features=features[:5],
        button_text=button_text,
        receipt_message=receipt_message,
        cover_path=cover if cover.exists() and cover.stat().st_size else None,
        thumbnail_path=thumbnail if thumbnail.exists() and thumbnail.stat().st_size else None,
        content_file=content_file,
        staging_dir=sd,
    )


# ---------------------------------------------------------------------------
# Phase 2: publish (browser)
# ---------------------------------------------------------------------------


class GumroadPublisher:
    """Drives the Gumroad web UI to create a draft product from a PublishPlan.

    Single-use: construct, call publish_draft(plan), context is closed.
    Persistent Chromium profile is reused across instances.
    """

    def __init__(
        self,
        profile_dir: Path,
        headless: bool = False,
        slow_mo_ms: int = 0,
        timeout_ms: int = 15000,
    ):
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.timeout_ms = timeout_ms

    def publish_draft(self, plan: PublishPlan) -> PublishResult:
        """Open Gumroad, create a draft product, fill all tabs, save.

        Never publishes (stays as draft). Screenshot + DOM dumped to
        staging/<repo>/_debug/ on any failure.
        """
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright

        result = PublishResult(repo_name=plan.repo_name)
        debug_dir = plan.staging_dir / "_debug"

        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=self.headless,
                slow_mo=self.slow_mo_ms,
            )
            ctx.set_default_timeout(self.timeout_ms)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            try:
                self._verify_logged_in(page)
                result.tabs_completed.append("login")

                self._create_product(page, plan)
                result.tabs_completed.append("create")

                self._fill_product_tab(page, plan)
                result.tabs_completed.append("product")

                self._fill_content_tab(page, plan)
                result.tabs_completed.append("content")

                self._fill_checkout_tab(page, plan)
                result.tabs_completed.append("checkout")

                self._fill_receipt_tab(page, plan)
                result.tabs_completed.append("receipt")

                product_id, product_url = self._save_as_draft(page)
                result.gumroad_product_id = product_id
                result.gumroad_url = product_url
                result.tabs_completed.append("saved")

            except (PWTimeout, AssertionError, Exception) as e:  # noqa: BLE001
                result.failed_at = (
                    result.tabs_completed[-1] if result.tabs_completed else "login"
                )
                result.error = f"{type(e).__name__}: {e}"
                debug_dir.mkdir(exist_ok=True)
                try:
                    page.screenshot(path=str(debug_dir / f"fail_{result.failed_at}.png"), full_page=True)
                    (debug_dir / f"fail_{result.failed_at}.html").write_text(page.content(), encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
                result.debug_dir = debug_dir
            finally:
                ctx.close()

        return result

    # ---- tab walkers ------------------------------------------------------

    def _verify_logged_in(self, page) -> None:
        """Ensure the persistent profile has an active Gumroad session."""
        page.goto(S.PRODUCT_DASHBOARD_URL, wait_until="domcontentloaded")
        # If redirected to /login, the session expired — bail with a clear msg.
        if "/login" in page.url:
            raise RuntimeError(
                "Gumroad session expired. Run once with --first-login to log "
                "in manually via the visible browser, then retry."
            )

    def _create_product(self, page, plan: PublishPlan) -> None:
        page.goto(S.NEW_PRODUCT_URL, wait_until="domcontentloaded")
        page.locator(S.PRODUCT_TYPE_DIGITAL).first.click()
        page.locator(S.NEW_PRODUCT_NAME).first.fill(plan.title)
        price_dollars = plan.price_cents / 100
        page.locator(S.NEW_PRODUCT_PRICE).first.fill(f"{price_dollars:g}")
        page.locator(S.CREATE_PRODUCT_BUTTON).first.click()
        page.wait_for_url(re.compile(S.PRODUCT_ID_URL_PATTERN))

    def _fill_product_tab(self, page, plan: PublishPlan) -> None:
        page.locator(S.TAB_PRODUCT).first.click()

        if plan.cover_path:
            page.locator(S.COVER_UPLOAD_INPUT).first.set_input_files(str(plan.cover_path))
        if plan.thumbnail_path:
            page.locator(S.THUMBNAIL_UPLOAD_INPUT).first.set_input_files(str(plan.thumbnail_path))

        editor = page.locator(S.DESCRIPTION_EDITOR).first
        editor.click()
        editor.fill("")  # clear default
        editor.type(plan.description_md, delay=5)

        summary_input = page.locator(S.SUMMARY_INPUT).first
        summary_input.fill(plan.summary)

        tags_input = page.locator(S.TAGS_INPUT).first
        for tag in plan.tags:
            tags_input.fill(tag)
            tags_input.press("Enter")

        # Expand Additional Details, add features
        try:
            page.locator(S.ADDITIONAL_DETAILS_ACCORDION).first.click()
        except Exception:  # noqa: BLE001
            pass  # already expanded, or no accordion

        for feature in plan.features:
            page.locator(S.ADD_FEATURE_BUTTON).first.click()
            # Fill the most recently added feature input
            inputs = page.locator(S.FEATURE_INPUTS)
            inputs.nth(inputs.count() - 1).fill(feature)

    def _fill_content_tab(self, page, plan: PublishPlan) -> None:
        if not plan.content_file:
            raise RuntimeError(
                f"no content file for {plan.repo_name}. Drop content.zip into "
                f"{plan.staging_dir} or set metadata.content_file."
            )
        page.locator(S.TAB_CONTENT).first.click()
        page.locator(S.CONTENT_UPLOAD_INPUT).first.set_input_files(str(plan.content_file))
        # Wait for upload to settle; Gumroad shows a progress bar.
        time.sleep(2)

    def _fill_checkout_tab(self, page, plan: PublishPlan) -> None:
        page.locator(S.TAB_CHECKOUT).first.click()
        page.locator(S.BUTTON_TEXT_INPUT).first.fill(plan.button_text)

    def _fill_receipt_tab(self, page, plan: PublishPlan) -> None:
        page.locator(S.TAB_RECEIPT).first.click()
        editor = page.locator(S.RECEIPT_MESSAGE_EDITOR).first
        editor.click()
        editor.fill("")
        editor.type(plan.receipt_message, delay=5)

    def _save_as_draft(self, page) -> tuple[str, str]:
        page.locator(S.SAVE_DRAFT_BUTTON).first.click()
        # Gumroad redirects back to the edit URL after save; extract product_id
        page.wait_for_load_state("networkidle")
        m = re.search(S.PRODUCT_ID_URL_PATTERN, page.url)
        if not m:
            raise RuntimeError(f"could not parse product id from url: {page.url}")
        return m.group(1), page.url
