"""Tests for gumroad_api_client.

Mocks the Gumroad v2 API with the `responses` library so we exercise the
real request shapes without live HTTP. The live smoke test is separate and
not run by pytest by default.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
import responses

sys.path.insert(0, str(Path(__file__).parent.parent))

from gumroad_api_client import (  # noqa: E402
    GUMROAD_API_BASE,
    GumroadAPIClient,
    GumroadAPIError,
    ProductPlan,
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GUMROAD_ACCESS_TOKEN", "test-token")
    return GumroadAPIClient()


@pytest.fixture
def sample_plan():
    return ProductPlan(
        name="Test Product",
        description="# Test\n\nA test product.",
        price_cents=0,
        tags=["a", "b", "c"],
        custom_summary="A short summary.",
        custom_receipt="Thank you.",
        files=[],
    )


def _url(path: str) -> str:
    return f"{GUMROAD_API_BASE}{path}"


class TestAuth:
    def test_missing_token_raises(self, monkeypatch):
        monkeypatch.delenv("GUMROAD_ACCESS_TOKEN", raising=False)
        with pytest.raises(GumroadAPIError, match="GUMROAD_ACCESS_TOKEN"):
            GumroadAPIClient()

    def test_explicit_token_overrides_env(self, monkeypatch):
        monkeypatch.delenv("GUMROAD_ACCESS_TOKEN", raising=False)
        c = GumroadAPIClient(access_token="explicit")
        assert c._token == "explicit"

    @responses.activate
    def test_sends_bearer_token(self, client, sample_plan):
        responses.add(
            responses.POST,
            _url("/products"),
            json={"success": True, "product": {"id": "p1", "short_url": "https://x/l/p1"}},
        )
        client.create_product(sample_plan)
        call = responses.calls[0]
        assert call.request.headers["Authorization"] == "Bearer test-token"


class TestCreateProduct:
    @responses.activate
    def test_sends_correct_payload(self, client, sample_plan):
        captured = {}

        def _handler(request):
            import json as _j
            captured.update(_j.loads(request.body))
            return (
                200,
                {},
                '{"success": true, "product": {"id": "prod_123", "short_url": "https://x/l/p"}}',
            )

        responses.add_callback(responses.POST, _url("/products"), callback=_handler)

        plan = sample_plan
        plan.files = [{"url": "https://s3/file.zip", "display_name": "pkg"}]
        ref = client.create_product(plan)

        assert captured["name"] == "Test Product"
        assert captured["price"] == 0
        assert captured["tags"] == ["a", "b", "c"]
        assert captured["custom_summary"] == "A short summary."
        assert captured["custom_receipt"] == "Thank you."
        assert captured["files"][0]["url"] == "https://s3/file.zip"
        assert ref.id == "prod_123"
        assert ref.url == "https://x/l/p"

    @responses.activate
    def test_omits_files_when_empty(self, client, sample_plan):
        captured = {}

        def _handler(request):
            import json as _j
            captured.update(_j.loads(request.body))
            return 200, {}, '{"success": true, "product": {"id": "p", "short_url": ""}}'

        responses.add_callback(responses.POST, _url("/products"), callback=_handler)
        client.create_product(sample_plan)
        assert "files" not in captured

    @responses.activate
    def test_raises_on_missing_id(self, client, sample_plan):
        responses.add(
            responses.POST,
            _url("/products"),
            json={"success": True, "product": {}},
        )
        with pytest.raises(GumroadAPIError, match="missing product.id"):
            client.create_product(sample_plan)

    @responses.activate
    def test_raises_on_api_failure(self, client, sample_plan):
        responses.add(
            responses.POST,
            _url("/products"),
            json={"success": False, "message": "invalid price"},
            status=422,
        )
        with pytest.raises(GumroadAPIError, match="invalid price"):
            client.create_product(sample_plan)


class TestPublish:
    @responses.activate
    def test_enable_endpoint(self, client):
        responses.add(
            responses.PUT,
            _url("/products/abc/enable"),
            json={"success": True, "product": {"id": "abc", "published": True}},
        )
        result = client.publish("abc")
        assert result["id"] == "abc"
        assert responses.calls[0].request.url.endswith("/products/abc/enable")


class TestDeleteProduct:
    @responses.activate
    def test_calls_delete(self, client):
        responses.add(
            responses.DELETE,
            _url("/products/abc"),
            json={"success": True, "product": {"id": "abc"}},
        )
        client.delete_product("abc")
        assert responses.calls[0].request.method == "DELETE"


class TestAddCoverUrl:
    @responses.activate
    def test_posts_url(self, client):
        captured = {}

        def _handler(request):
            import json as _j
            captured.update(_j.loads(request.body))
            return 200, {}, '{"success": true, "covers": [], "main_cover_id": null}'

        responses.add_callback(
            responses.POST, _url("/products/abc/covers"), callback=_handler
        )
        client.add_cover_url("abc", "https://example.com/cover.png")
        assert captured["url"] == "https://example.com/cover.png"


class TestSetThumbnail:
    def test_raises_not_implemented(self, client, tmp_path):
        img = tmp_path / "thumb.png"
        img.write_bytes(b"fake")
        with pytest.raises(NotImplementedError, match="signed_blob_id"):
            client.set_thumbnail("abc", img)


class TestUploadFile:
    @responses.activate
    def test_end_to_end_single_part(self, client, tmp_path):
        content = b"hello gumroad"
        src = tmp_path / "content.zip"
        src.write_bytes(content)

        # Presign
        responses.add(
            responses.POST,
            _url("/files/presign"),
            json={
                "success": True,
                "upload_id": "upl_1",
                "key": "abc/def/content.zip",
                "file_url": "https://files.gumroad.com/abc/def/content.zip",
                "parts": [
                    {"part_number": 1, "presigned_url": "https://s3.put/part1"}
                ],
            },
        )
        # S3 PUT
        responses.add(
            responses.PUT,
            "https://s3.put/part1",
            body="",
            status=200,
            headers={"ETag": '"etag-aaa"'},
        )
        # Complete
        complete_captured = {}

        def _complete(request):
            import json as _j
            complete_captured.update(_j.loads(request.body))
            return 200, {}, '{"success": true, "file_url": "https://final/url"}'

        responses.add_callback(
            responses.POST, _url("/files/complete"), callback=_complete
        )

        file_url = client.upload_file(src)

        # Gumroad's presign response returned the canonical file_url; that's
        # what we return (not the "final" url from complete — which is the
        # same value in practice).
        assert file_url == "https://files.gumroad.com/abc/def/content.zip"
        assert complete_captured["upload_id"] == "upl_1"
        assert complete_captured["parts"] == [{"part_number": 1, "etag": "etag-aaa"}]

    @responses.activate
    def test_aborts_on_s3_put_failure(self, client, tmp_path):
        src = tmp_path / "content.zip"
        src.write_bytes(b"hello")

        responses.add(
            responses.POST,
            _url("/files/presign"),
            json={
                "success": True,
                "upload_id": "upl_2",
                "key": "k",
                "file_url": "https://files/final",
                "parts": [{"part_number": 1, "presigned_url": "https://s3.fail/1"}],
            },
        )
        responses.add(
            responses.PUT,
            "https://s3.fail/1",
            status=500,
            body="s3 exploded",
        )
        abort_called = {"hit": False}

        def _abort(request):
            abort_called["hit"] = True
            return 200, {}, '{"success": true, "status": "accepted"}'

        responses.add_callback(responses.POST, _url("/files/abort"), callback=_abort)

        with pytest.raises(GumroadAPIError, match="multipart PUT failed"):
            client.upload_file(src)
        assert abort_called["hit"] is True

    @responses.activate
    def test_rejects_empty_file(self, client, tmp_path):
        src = tmp_path / "empty.zip"
        src.touch()
        with pytest.raises(GumroadAPIError, match="empty file"):
            client.upload_file(src)

    @responses.activate
    def test_rejects_missing_file(self, client, tmp_path):
        missing = tmp_path / "no.zip"
        with pytest.raises(GumroadAPIError, match="not a file"):
            client.upload_file(missing)


class TestUpdateProduct:
    @responses.activate
    def test_sends_fields_through(self, client):
        captured = {}

        def _handler(request):
            import json as _j
            captured.update(_j.loads(request.body))
            return 200, {}, '{"success": true, "product": {"id": "abc"}}'

        responses.add_callback(
            responses.PUT, _url("/products/abc"), callback=_handler
        )
        client.update_product("abc", files=[{"url": "https://u"}], tags=["x"])
        assert captured["files"][0]["url"] == "https://u"
        assert captured["tags"] == ["x"]
