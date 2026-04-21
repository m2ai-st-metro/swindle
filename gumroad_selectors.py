"""Gumroad UI selectors — single source of truth.

Every selector Gumroad's web app exposes to the publisher lives here. When
the Gumroad UI drifts (class renames, DOM restructure), there is exactly one
file to update.

All selectors are UNVERIFIED on first deploy. The current values are best
guesses from public-facing Gumroad docs and common web-app patterns. Before
the first real publish, verify each selector via:

    python -m playwright codegen https://app.gumroad.com

or by opening DevTools while manually walking the new-product flow. Update
this file with the selectors that actually match, then run the publisher
in --headful mode for the first few products to catch any missed drift.

Selectors prefer:
  1. Stable test-ids / aria-labels if Gumroad exposes them.
  2. Visible text via Playwright's text= engine (most resilient to CSS drift).
  3. Semantic role= queries (role=button, role=textbox).
  4. CSS classes only as last resort (most brittle).
"""

from typing import Final

# Top-level entry points
NEW_PRODUCT_URL: Final = "https://app.gumroad.com/products/new"
PRODUCT_DASHBOARD_URL: Final = "https://app.gumroad.com/products"
LOGIN_URL: Final = "https://app.gumroad.com/login"

# Login check — matches the "Logged in" landing page (dashboard)
LOGGED_IN_MARKER: Final = "nav[aria-label='Main navigation']"

# ---------------------------------------------------------------------------
# New product form (first screen after clicking "New product")
# ---------------------------------------------------------------------------

# Product-type picker. Gumroad groups products into types; we want Digital.
PRODUCT_TYPE_DIGITAL: Final = "text=Digital product"

# Name field on the new-product form
NEW_PRODUCT_NAME: Final = "input[name='name'], input[placeholder*='product name' i]"

# Price field on the new-product form. Gumroad uses dollars; 0 means free.
NEW_PRODUCT_PRICE: Final = "input[name='price'], input[placeholder*='price' i]"

# "Next" / "Create" button that moves from the new-product form to the editor
CREATE_PRODUCT_BUTTON: Final = "button:has-text('Next'), button:has-text('Create')"

# ---------------------------------------------------------------------------
# Product editor — tab navigation
# ---------------------------------------------------------------------------
# Gumroad's editor has a left-side tab list. Each tab is clicked to reveal
# its fields. Text-match is most resilient here.

TAB_PRODUCT: Final = "a:has-text('Product'), button:has-text('Product')"
TAB_CONTENT: Final = "a:has-text('Content'), button:has-text('Content')"
TAB_CHECKOUT: Final = "a:has-text('Checkout'), button:has-text('Checkout')"
TAB_RECEIPT: Final = "a:has-text('Receipt'), button:has-text('Receipt')"
TAB_SHARE: Final = "a:has-text('Share'), button:has-text('Share')"

# ---------------------------------------------------------------------------
# Product tab — fields
# ---------------------------------------------------------------------------

# Cover image upload. Gumroad uses a drag-drop zone; Playwright can fill an
# input[type=file] even if hidden behind a styled drop zone.
COVER_UPLOAD_INPUT: Final = (
    "input[type='file'][accept*='image'], "
    "div:has-text('Cover') >> input[type='file']"
)

# Thumbnail upload — Gumroad exposes this as a secondary upload near cover
THUMBNAIL_UPLOAD_INPUT: Final = (
    "div:has-text('Thumbnail') >> input[type='file']"
)

# Description editor. Gumroad uses a ProseMirror-based editor; the
# contenteditable div is the paste target.
DESCRIPTION_EDITOR: Final = (
    "[contenteditable='true'][role='textbox'], "
    "div.ProseMirror[contenteditable='true']"
)

# Summary (one-liner that shows in search)
SUMMARY_INPUT: Final = "input[name='summary'], textarea[name='summary']"

# Tags input — multi-value pill picker
TAGS_INPUT: Final = "input[placeholder*='tag' i]"

# ---------------------------------------------------------------------------
# Additional details → Features
# ---------------------------------------------------------------------------
# Gumroad's Features section is an expandable accordion under "Additional
# details". Clicking "Add feature" inserts a new row with a text input.

ADDITIONAL_DETAILS_ACCORDION: Final = "button:has-text('Additional details')"
ADD_FEATURE_BUTTON: Final = "button:has-text('Add feature'), button:has-text('Add a feature')"
FEATURE_INPUTS: Final = "input[name*='feature' i], input[placeholder*='feature' i]"

# ---------------------------------------------------------------------------
# Content tab — downloadable package upload
# ---------------------------------------------------------------------------

CONTENT_UPLOAD_INPUT: Final = (
    "input[type='file']:not([accept*='image'])"
)

# Sometimes the Content tab has an "Upload files" button that opens a picker
UPLOAD_FILES_BUTTON: Final = "button:has-text('Upload files'), button:has-text('Upload file')"

# ---------------------------------------------------------------------------
# Checkout tab — button text
# ---------------------------------------------------------------------------

BUTTON_TEXT_INPUT: Final = (
    "input[name='button_text'], input[placeholder*='button' i]"
)

# ---------------------------------------------------------------------------
# Receipt tab — custom message
# ---------------------------------------------------------------------------

RECEIPT_MESSAGE_EDITOR: Final = (
    "textarea[name*='receipt' i], "
    "[contenteditable='true'][aria-label*='receipt' i], "
    "div:has-text('Custom message') >> [contenteditable='true']"
)

# ---------------------------------------------------------------------------
# Save as draft
# ---------------------------------------------------------------------------

SAVE_DRAFT_BUTTON: Final = (
    "button:has-text('Save as draft'), "
    "button:has-text('Save draft'), "
    "button:has-text('Save')"
)

# After save, Gumroad redirects to the product page; the URL includes the
# product short-id. Regex to extract it.
PRODUCT_ID_URL_PATTERN: Final = r"/products/([A-Za-z0-9_-]+)/edit"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALL_SELECTOR_KEYS: Final = frozenset({
    "NEW_PRODUCT_URL", "PRODUCT_DASHBOARD_URL", "LOGIN_URL",
    "LOGGED_IN_MARKER",
    "PRODUCT_TYPE_DIGITAL", "NEW_PRODUCT_NAME", "NEW_PRODUCT_PRICE",
    "CREATE_PRODUCT_BUTTON",
    "TAB_PRODUCT", "TAB_CONTENT", "TAB_CHECKOUT", "TAB_RECEIPT", "TAB_SHARE",
    "COVER_UPLOAD_INPUT", "THUMBNAIL_UPLOAD_INPUT",
    "DESCRIPTION_EDITOR", "SUMMARY_INPUT", "TAGS_INPUT",
    "ADDITIONAL_DETAILS_ACCORDION", "ADD_FEATURE_BUTTON", "FEATURE_INPUTS",
    "CONTENT_UPLOAD_INPUT", "UPLOAD_FILES_BUTTON",
    "BUTTON_TEXT_INPUT",
    "RECEIPT_MESSAGE_EDITOR",
    "SAVE_DRAFT_BUTTON", "PRODUCT_ID_URL_PATTERN",
})
