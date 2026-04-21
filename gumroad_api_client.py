"""Gumroad API client for auto-publishing staged listings.

Replaces the Playwright `gumroad_publisher` path. Uses the real Gumroad v2
API (merged in antiwork/gumroad PR #4267 on 2026-04-06; thumbnail + cover
endpoints in PR #4311 on 2026-04-07).

Flow:
    client = GumroadAPIClient()
    file_url = client.upload_file(content_zip)
    product = client.create_product(plan, file_url=file_url)
    client.add_cover_url(product.id, cover_public_url)   # optional
    client.publish(product.id)

Thumbnail limitation
--------------------
The thumbnail endpoint only accepts `signed_blob_id` (ActiveStorage), and
ActiveStorage direct-uploads are not mounted on the Gumroad public API.
There is no pure-API path to set a thumbnail today. `set_thumbnail` raises
`NotImplementedError` until Gumroad exposes a direct-upload endpoint or
accepts a `url` parameter here. Covers support `url` and do work.

Rate limits
-----------
Gumroad caps product creates at 10/min and 50/9hr (PR #4315). Seller tiers
have a daily cap of 10 or 100 products. The client does not enforce these;
callers should `time.sleep(6)` between creates when batching.

Source of truth
---------------
- app/controllers/api/v2/links_controller.rb (create, update, destroy, enable)
- app/controllers/api/v2/files_controller.rb (presign, complete, abort)
- app/controllers/api/v2/covers_controller.rb (create, destroy)
- app/controllers/api/v2/thumbnails_controller.rb (create, destroy)

Docs at gumroad.com/api are stale — controllers are authoritative.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.env.shared"))

GUMROAD_API_BASE = "https://api.gumroad.com/v2"
PART_SIZE_BYTES = 100 * 1024 * 1024  # Gumroad splits multipart at 100 MB
REQUEST_TIMEOUT_SECONDS = 60


class GumroadAPIError(RuntimeError):
    """Raised when the Gumroad API returns an error or unexpected response."""

    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass
class ProductPlan:
    """Shape we send to POST /v2/products."""

    name: str
    description: str
    price_cents: int
    tags: list[str]
    custom_summary: str
    custom_receipt: str = ""
    files: list[dict] = field(default_factory=list)


@dataclass
class ProductRef:
    """Identifier + URL returned after a successful create."""

    id: str
    url: str
    raw: dict = field(default_factory=dict)


class GumroadAPIClient:
    """Thin wrapper around the Gumroad v2 API.

    Auth: Bearer token from `GUMROAD_ACCESS_TOKEN` unless overridden.
    """

    def __init__(
        self,
        access_token: str | None = None,
        base_url: str = GUMROAD_API_BASE,
        session: requests.Session | None = None,
    ):
        token = access_token or os.environ.get("GUMROAD_ACCESS_TOKEN")
        if not token:
            raise GumroadAPIError(
                "GUMROAD_ACCESS_TOKEN not set (expected in ~/.env.shared)"
            )
        self._token = token
        self._base = base_url.rstrip("/")
        self._session = session or requests.Session()

    # ---- HTTP helpers -----------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        expect_success: bool = True,
    ) -> dict:
        url = f"{self._base}{path}"
        resp = self._session.request(
            method=method,
            url=url,
            headers=self._headers(),
            json=json_body,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        try:
            data = resp.json()
        except ValueError:
            raise GumroadAPIError(
                f"{method} {path}: non-JSON response",
                status_code=resp.status_code,
                body=resp.text[:500],
            )

        if expect_success and not data.get("success"):
            msg = data.get("message") or data.get("error") or "unknown Gumroad error"
            raise GumroadAPIError(
                f"{method} {path}: {msg}",
                status_code=resp.status_code,
                body=str(data)[:500],
            )
        return data

    # ---- File upload (multipart presigned) --------------------------------

    def upload_file(self, path: Path | str) -> str:
        """Upload a local file via Gumroad's presign -> parts -> complete flow.

        Returns the `file_url` suitable for use in `files[].url` on a product.
        Aborts the upload on any PUT failure to avoid orphaned blobs.
        """
        p = Path(path)
        if not p.exists() or not p.is_file():
            raise GumroadAPIError(f"upload_file: not a file: {p}")
        size = p.stat().st_size
        if size == 0:
            raise GumroadAPIError(f"upload_file: empty file: {p}")

        presign = self._request(
            "POST",
            "/files/presign",
            {"filename": p.name, "file_size": size},
        )
        upload_id = presign["upload_id"]
        key = presign["key"]
        file_url = presign["file_url"]
        parts_spec = presign["parts"]

        try:
            etags = self._upload_parts(p, parts_spec)
        except Exception as e:
            self._abort_upload(upload_id, key)
            raise GumroadAPIError(f"upload_file: multipart PUT failed: {e}") from e

        self._request(
            "POST",
            "/files/complete",
            {"upload_id": upload_id, "key": key, "parts": etags},
        )
        return file_url

    def _upload_parts(self, path: Path, parts_spec: list[dict]) -> list[dict]:
        """PUT each part to its presigned S3 URL, returning [{part_number, etag}]."""
        etags: list[dict] = []
        with path.open("rb") as f:
            for part in parts_spec:
                chunk = f.read(PART_SIZE_BYTES)
                if not chunk:
                    break
                put_url = part.get("presigned_url") or part.get("url")
                if not put_url:
                    raise GumroadAPIError(
                        f"presign response missing presigned_url for part {part}"
                    )
                resp = self._session.put(put_url, data=chunk, timeout=REQUEST_TIMEOUT_SECONDS)
                if resp.status_code not in (200, 204):
                    raise GumroadAPIError(
                        f"S3 PUT part {part['part_number']} failed: "
                        f"{resp.status_code} {resp.text[:200]}"
                    )
                etag = resp.headers.get("ETag", "").strip('"')
                if not etag:
                    raise GumroadAPIError(
                        f"S3 PUT part {part['part_number']} returned no ETag"
                    )
                etags.append({"part_number": part["part_number"], "etag": etag})
        return etags

    def _abort_upload(self, upload_id: str, key: str) -> None:
        """Best-effort abort of a multipart upload."""
        try:
            self._request(
                "POST",
                "/files/abort",
                {"upload_id": upload_id, "key": key},
                expect_success=False,
            )
        except Exception:
            pass

    # ---- Product CRUD -----------------------------------------------------

    def create_product(self, plan: ProductPlan) -> ProductRef:
        """POST /v2/products — create a draft product.

        Returns a ProductRef. The product is NOT published until `publish()`
        is called. Pricing is in cents.
        """
        payload: dict = {
            "name": plan.name,
            "description": plan.description,
            "price": plan.price_cents,
            "custom_summary": plan.custom_summary,
            "tags": plan.tags,
        }
        if plan.custom_receipt:
            payload["custom_receipt"] = plan.custom_receipt
        if plan.files:
            payload["files"] = plan.files

        data = self._request("POST", "/products", payload)
        product = data.get("product") or {}
        pid = product.get("id") or product.get("external_id")
        if not pid:
            raise GumroadAPIError(
                f"create_product: response missing product.id: {str(product)[:300]}"
            )
        return ProductRef(
            id=pid,
            url=product.get("short_url") or product.get("url") or "",
            raw=product,
        )

    def update_product(self, product_id: str, **fields) -> dict:
        """PUT /v2/products/:id — update whitelisted fields (see controller)."""
        data = self._request("PUT", f"/products/{product_id}", fields)
        return data.get("product") or {}

    def publish(self, product_id: str) -> dict:
        """POST /v2/products/:id/enable — take the draft live.

        The Rails route is `put :enable` (member action). Both PUT and POST
        reach the enable controller action in Rails; we use PUT because
        that's what `config/routes.rb` declares.
        """
        data = self._request("PUT", f"/products/{product_id}/enable", {})
        return data.get("product") or {}

    def delete_product(self, product_id: str) -> None:
        """DELETE /v2/products/:id — hard delete (used for test cleanup)."""
        self._request("DELETE", f"/products/{product_id}", None)

    # ---- Cover + thumbnail ------------------------------------------------

    def add_cover_url(self, product_id: str, image_url: str) -> dict:
        """POST /v2/products/:id/covers — attach a cover by URL.

        The endpoint also supports `signed_blob_id` for ActiveStorage blobs,
        but direct-upload routes are not mounted on the public API, so URL
        is the only practical path.
        """
        data = self._request(
            "POST",
            f"/products/{product_id}/covers",
            {"url": image_url},
        )
        return data

    def set_thumbnail(self, product_id: str, image_path: Path | str) -> None:  # noqa: ARG002
        """POST /v2/products/:id/thumbnail — NOT IMPLEMENTED via pure API.

        The endpoint requires a `signed_blob_id` from ActiveStorage direct
        upload, which Gumroad does not expose on the v2 API. Until that
        changes, thumbnails must be set via the web UI or by discovering an
        undocumented direct-upload endpoint.
        """
        raise NotImplementedError(
            "Gumroad thumbnail API requires signed_blob_id from ActiveStorage "
            "direct upload, which is not exposed on the v2 public API. "
            "Set thumbnails via the web UI or skip them."
        )
