"""Tests for Swindle storefront listing agent."""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from db import SwindleDB
from capture_selectors import (
    SENTINELS,
    apply_captures,
    locator_expr_to_selector_string,
    parse_capture,
)
from gumroad_publisher import PlanError, PublishPlan, build_plan
from image_generator import build_cover_command, build_thumbnail_command
from listing_generator import (
    _clean_listing,
    _parse_button_text,
    _parse_features,
    _parse_metadata,
    _parse_receipt_message,
    _read_spec,
)
from validator import validate_listing


# --- CLI Tests ---

class TestCLI:
    """Test that CLI commands exist and have correct signatures."""

    def setup_method(self):
        self.runner = CliRunner()
        from swindle import cli
        self.cli = cli

    def test_help(self):
        result = self.runner.invoke(self.cli, ["--help"])
        assert result.exit_code == 0
        assert "Swindle storefront listing agent" in result.output

    def test_prepare_help(self):
        result = self.runner.invoke(self.cli, ["prepare", "--help"])
        assert result.exit_code == 0
        assert "--spec-path" in result.output
        assert "--project-dir" in result.output
        assert "--title" in result.output
        assert "--dry-run" in result.output

    def test_list_staged_help(self):
        result = self.runner.invoke(self.cli, ["list-staged", "--help"])
        assert result.exit_code == 0

    def test_approve_help(self):
        result = self.runner.invoke(self.cli, ["approve", "--help"])
        assert result.exit_code == 0

    def test_reject_help(self):
        result = self.runner.invoke(self.cli, ["reject", "--help"])
        assert result.exit_code == 0
        assert "--reason" in result.output

    def test_status_help(self):
        result = self.runner.invoke(self.cli, ["status", "--help"])
        assert result.exit_code == 0


# --- Database Tests ---

class TestDatabase:
    """Test SwindleDB operations."""

    def setup_method(self):
        self.db = SwindleDB(":memory:")
        self.db.init_db()

    def teardown_method(self):
        self.db.close()

    def test_init_creates_table(self):
        self.db.connect()
        cursor = self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='listings'"
        )
        assert cursor.fetchone() is not None

    def test_add_listing(self):
        row_id = self.db.add_listing(
            repo_name="test-repo",
            repo_url="https://github.com/m2ai-portfolio/test-repo",
            title="Test Repo",
            spec_path="/tmp/spec.txt",
            staging_dir="/tmp/staging/test-repo",
        )
        assert row_id > 0

    def test_get_staged(self):
        self.db.add_listing("repo-a", "https://url/a", "A", None, "/tmp/a")
        self.db.add_listing("repo-b", "https://url/b", "B", None, "/tmp/b")

        staged = self.db.get_staged()
        assert len(staged) == 2
        assert all(s["status"] == "staged" for s in staged)

    def test_approve(self):
        self.db.add_listing("repo-a", "https://url/a", "A", None, "/tmp/a")
        result = self.db.update_status("repo-a", "approved")
        assert result is True

        listing = self.db.get_by_name("repo-a")
        assert listing["status"] == "approved"
        assert listing["reviewed_at"] is not None

    def test_reject_with_reason(self):
        self.db.add_listing("repo-a", "https://url/a", "A", None, "/tmp/a")
        self.db.update_status("repo-a", "rejected", "bad quality")

        listing = self.db.get_by_name("repo-a")
        assert listing["status"] == "rejected"
        assert listing["reason"] == "bad quality"

    def test_get_by_name_missing(self):
        result = self.db.get_by_name("nonexistent")
        assert result is None

    def test_update_nonexistent(self):
        result = self.db.update_status("nonexistent", "approved")
        assert result is False

    def test_get_stats(self):
        self.db.add_listing("repo-a", "https://url/a", "A", None, "/tmp/a")
        self.db.add_listing("repo-b", "https://url/b", "B", None, "/tmp/b")
        self.db.update_status("repo-b", "approved")

        stats = self.db.get_stats()
        assert stats["staged"] == 1
        assert stats["approved"] == 1
        assert stats["total"] == 2

    def test_upsert_replaces(self):
        self.db.add_listing("repo-a", "https://url/a", "A v1", None, "/tmp/a")
        self.db.add_listing("repo-a", "https://url/a", "A v2", None, "/tmp/a2")

        listing = self.db.get_by_name("repo-a")
        assert listing["title"] == "A v2"
        assert listing["staging_dir"] == "/tmp/a2"


# --- Listing Generator Tests ---

class TestListingGenerator:
    """Test listing generation helpers."""

    def test_read_spec_missing(self):
        result = _read_spec("/nonexistent/spec.txt")
        assert "not found" in result

    def test_read_spec_none(self):
        result = _read_spec(None)
        assert "no spec" in result

    def test_read_spec_valid(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Test spec content")
            f.flush()
            result = _read_spec(f.name)
            assert result == "Test spec content"
        os.unlink(f.name)

    def test_parse_metadata_basic(self):
        llm_output = (
            "## What's Inside\nStuff\n\n"
            "SUMMARY: A great developer tool for testing\n"
            "TAGS: claude-code, mcp, testing"
        )
        meta = _parse_metadata(llm_output, "Test Tool", "https://github.com/org/repo")

        assert meta["title"] == "Test Tool"
        assert meta["price"] == 0
        assert meta["repo_url"] == "https://github.com/org/repo"
        assert meta["summary"] == "A great developer tool for testing"
        assert "claude-code" in meta["tags"]
        assert "mcp" in meta["tags"]
        assert "testing" in meta["tags"]
        assert "generated_at" in meta

    def test_parse_metadata_no_markers(self):
        llm_output = "Just some content without markers"
        meta = _parse_metadata(llm_output, "Fallback", "https://url")

        assert meta["title"] == "Fallback"
        assert meta["summary"] == "Fallback"
        assert meta["tags"] == ["developer-tools"]

    def test_clean_listing_removes_metadata(self):
        raw = (
            "## What's Inside\nCode and configs\n\n"
            "## The Problem\nIt's hard\n\n"
            "---\n"
            "SUMMARY: A tool\n"
            "TAGS: a, b, c"
        )
        cleaned = _clean_listing(raw)
        assert "SUMMARY:" not in cleaned
        assert "TAGS:" not in cleaned
        assert "## What's Inside" in cleaned
        assert "## The Problem" in cleaned

    def test_clean_listing_strips_trailing_separator(self):
        raw = "## Content\nSome text\n\n---"
        cleaned = _clean_listing(raw)
        assert not cleaned.endswith("---")

    @patch("listing_generator.requests.post")
    def test_generate_listing_calls_llm(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": (
                        "## What's Inside\nSource code\n\n"
                        "SUMMARY: A test tool\n"
                        "TAGS: test, tool"
                    )
                }
            }]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with patch.dict(os.environ, {"DEEPINFRA_API_KEY": "test-key"}):
            from listing_generator import generate_listing
            listing_md, metadata = generate_listing(
                repo_url="https://github.com/org/repo",
                title="Test Tool",
            )

        assert "## What's Inside" in listing_md
        assert metadata["title"] == "Test Tool"
        assert metadata["price"] == 0
        mock_post.assert_called_once()

        # Verify correct model in request
        call_kwargs = mock_post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["model"] == "mistralai/Mistral-Small-3.2-24B-Instruct-2506"


# --- Image Generator Tests ---

class TestImageGenerator:
    """Test image command construction."""

    def test_cover_command_structure(self):
        cmd = build_cover_command("My Tool", "/tmp/staging/my-tool")
        assert "/home/apexaipc/.claude/skills/banana-maker/generate_image.py" in cmd
        assert "--model" in cmd
        assert "flash" in cmd
        assert "--aspect-ratio" in cmd
        assert "16:9" in cmd
        assert "--output" in cmd
        assert "/tmp/staging/my-tool/cover.png" in cmd

    def test_thumbnail_command_structure(self):
        cmd = build_thumbnail_command("My Tool", "mcp", "/tmp/staging/my-tool")
        assert "/home/apexaipc/.claude/skills/banana-maker/generate_image.py" in cmd
        assert "--model" in cmd
        assert "flash" in cmd
        assert "--aspect-ratio" in cmd
        assert "1:1" in cmd
        assert "--output" in cmd
        assert "/tmp/staging/my-tool/thumbnail.png" in cmd
        assert "mcp" in " ".join(cmd)

    def test_cover_prompt_has_dark_theme(self):
        cmd = build_cover_command("Test", "/tmp/test")
        prompt = cmd[2]  # prompt is the third arg
        assert "#0d1117" in prompt
        assert "#58a6ff" in prompt

    def test_thumbnail_prompt_has_title(self):
        cmd = build_thumbnail_command("AwesomeTool", "cli", "/tmp/test")
        prompt = cmd[2]
        assert "AwesomeTool" in prompt


# --- Integration-style Tests ---

class TestMetadataJSON:
    """Test metadata.json structure matches expected format."""

    def test_metadata_schema(self):
        from listing_generator import _parse_metadata

        meta = _parse_metadata(
            "SUMMARY: Test\nTAGS: a, b",
            "Title",
            "https://github.com/org/repo",
        )

        required_keys = {"title", "price", "tags", "summary", "repo_url", "generated_at"}
        assert required_keys.issubset(set(meta.keys()))
        assert isinstance(meta["price"], int)
        assert isinstance(meta["tags"], list)
        assert meta["price"] == 0

    def test_metadata_serializable(self):
        from listing_generator import _parse_metadata

        meta = _parse_metadata(
            "SUMMARY: Test\nTAGS: a, b",
            "Title",
            "https://url",
        )
        # Must be JSON-serializable
        serialized = json.dumps(meta)
        deserialized = json.loads(serialized)
        assert deserialized["title"] == "Title"


class TestFieldParsers:
    """Parsers for the new Gumroad field outputs."""

    def test_features_strips_mixed_bullets(self):
        raw = "Here are the features:\n- A\n* B\n1. C\n  • D\nE\nOutput: ignore"
        assert _parse_features(raw) == ["A", "B", "C", "D", "E"]

    def test_features_caps_at_five(self):
        raw = "\n".join(f"Line {i}" for i in range(10))
        assert len(_parse_features(raw)) == 5

    def test_button_text_handles_prefix_and_quotes(self):
        cases = [
            ('Output: "Grab the pack".', "Grab the pack"),
            ('\n\n  Get the code\n', "Get the code"),
            ('Button text: Try it free', "Try it free"),
            ('"Download it"', "Download it"),
            ("'Take the template'.", "Take the template"),
        ]
        for raw, expected in cases:
            assert _parse_button_text(raw) == expected, f"failed on {raw!r}"

    def test_receipt_strips_dear_and_output_noise(self):
        raw = "Dear customer,\nThanks. Run `make setup`.\n"
        assert _parse_receipt_message(raw) == "Thanks. Run `make setup`."

    def test_receipt_preserves_markdown(self):
        raw = "Run `npm init`, then read **README.md**."
        assert _parse_receipt_message(raw) == "Run `npm init`, then read **README.md**."


class TestValidator:
    """Quality gate for staging/<repo>/ directories."""

    @staticmethod
    def _good_staging(sd: Path) -> None:
        (sd / "listing.md").write_text(
            "This tool solves X for developers.\n\n"
            + "It does Y fast and Z reliably. " * 5,
            encoding="utf-8",
        )
        (sd / "metadata.json").write_text(
            json.dumps({
                "title": "Test Tool",
                "price": 0,
                "tags": ["cli", "python", "mcp"],
                "summary": "Tool for devs who want X, saves 2 hours/week.",
                "repo_url": "https://github.com/x/y",
                "generated_at": "now",
            }),
            encoding="utf-8",
        )
        (sd / "features.txt").write_text("Feature A\nFeature B\nFeature C\n", encoding="utf-8")
        (sd / "button_text.txt").write_text("Grab the pack\n", encoding="utf-8")
        (sd / "receipt_message.md").write_text("Thanks. Run README setup.\n", encoding="utf-8")
        (sd / "cover.png").write_bytes(b"x")
        (sd / "thumbnail.png").write_bytes(b"y")

    def test_passes_clean_listing(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._good_staging(sd)
            assert validate_listing(sd) == []

    def test_flags_missing_files(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            failures = validate_listing(sd)
            msgs = "\n".join(failures)
            for name in ("listing.md", "metadata.json", "features.txt",
                         "button_text.txt", "receipt_message.md",
                         "cover.png", "thumbnail.png"):
                assert name in msgs, f"expected {name} in failures"

    def test_flags_stub_tags(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._good_staging(sd)
            (sd / "metadata.json").write_text(
                json.dumps({"title": "T", "summary": "fine", "tags": ["tag1", "tag2", "tag3"]}),
                encoding="utf-8",
            )
            failures = validate_listing(sd)
            assert any("stub" in f for f in failures)

    def test_flags_summary_equals_title(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._good_staging(sd)
            (sd / "metadata.json").write_text(
                json.dumps({"title": "Same", "summary": "Same",
                            "tags": ["a", "b", "c"]}),
                encoding="utf-8",
            )
            failures = validate_listing(sd)
            assert any("just the title" in f for f in failures)

    def test_flags_placeholder_text(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._good_staging(sd)
            (sd / "listing.md").write_text(
                "A" * 200 + "\n\nLorem ipsum dolor sit amet.", encoding="utf-8",
            )
            failures = validate_listing(sd)
            assert any("lorem ipsum" in f for f in failures)

    def test_flags_oversize_button_and_words(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._good_staging(sd)
            (sd / "button_text.txt").write_text(
                "Grab this very long button text here\n", encoding="utf-8",
            )
            failures = validate_listing(sd)
            assert any("too long" in f for f in failures)
            assert any("max 4" in f for f in failures)

    def test_flags_feature_bullet_markers_leaked(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._good_staging(sd)
            (sd / "features.txt").write_text(
                "- marker leaked\nOK line\nOK line\n", encoding="utf-8",
            )
            failures = validate_listing(sd)
            assert any("bullet marker" in f for f in failures)

    def test_flags_receipt_dear_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._good_staging(sd)
            (sd / "receipt_message.md").write_text(
                "Dear customer, thanks.\n", encoding="utf-8",
            )
            failures = validate_listing(sd)
            assert any("Dear" in f for f in failures)


class TestPublisherPlan:
    """build_plan() reads staging dirs and produces PublishPlan without a browser."""

    @staticmethod
    def _populate(sd: Path, *, include_images=True, include_content=False,
                  bad_tags=False, empty_summary=False, short_features=False):
        (sd / "listing.md").write_text("A" * 200, encoding="utf-8")
        meta = {
            "title": "Test Tool", "price": 0,
            "tags": ["tag1", "tag2", "tag3"] if bad_tags else ["a", "b", "c"],
            "summary": "" if empty_summary else "Saves 2 hours a week for devs.",
            "repo_url": "https://github.com/x/y", "generated_at": "now",
        }
        (sd / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        (sd / "features.txt").write_text(
            "A\nB\n" if short_features else "A\nB\nC\nD\nE\n",
            encoding="utf-8",
        )
        (sd / "button_text.txt").write_text("Grab it\n", encoding="utf-8")
        (sd / "receipt_message.md").write_text("Thanks.\n", encoding="utf-8")
        if include_images:
            (sd / "cover.png").write_bytes(b"x")
            (sd / "thumbnail.png").write_bytes(b"y")
        if include_content:
            (sd / "content.zip").write_bytes(b"z")

    def test_build_plan_reads_all_fields(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._populate(sd, include_content=True)
            plan = build_plan(sd, "my-repo")
            assert plan.repo_name == "my-repo"
            assert plan.title == "Test Tool"
            assert plan.summary == "Saves 2 hours a week for devs."
            assert plan.price_cents == 0
            assert plan.tags == ["a", "b", "c"]
            assert plan.description_md == "A" * 200
            assert plan.features == ["A", "B", "C", "D", "E"]
            assert plan.button_text == "Grab it"
            assert plan.receipt_message == "Thanks."
            assert plan.cover_path == sd / "cover.png"
            assert plan.thumbnail_path == sd / "thumbnail.png"
            assert plan.content_file == sd / "content.zip"

    def test_build_plan_caps_features_at_five(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._populate(sd)
            (sd / "features.txt").write_text("\n".join(f"L{i}" for i in range(10)), encoding="utf-8")
            plan = build_plan(sd, "r")
            assert len(plan.features) == 5

    def test_build_plan_fails_on_missing_listing(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            with pytest.raises(PlanError, match="listing.md"):
                build_plan(sd, "r")

    def test_build_plan_fails_on_missing_features(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._populate(sd)
            (sd / "features.txt").unlink()
            with pytest.raises(PlanError, match="features.txt"):
                build_plan(sd, "r")

    def test_build_plan_fails_on_empty_summary(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._populate(sd, empty_summary=True)
            with pytest.raises(PlanError, match="summary"):
                build_plan(sd, "r")

    def test_build_plan_fails_on_under_three_tags(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._populate(sd)
            meta = json.loads((sd / "metadata.json").read_text())
            meta["tags"] = ["just-one"]
            (sd / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
            with pytest.raises(PlanError, match="tags"):
                build_plan(sd, "r")

    def test_build_plan_fails_on_too_few_features(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._populate(sd, short_features=True)
            with pytest.raises(PlanError, match="features.*3"):
                build_plan(sd, "r")

    def test_build_plan_content_file_none_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._populate(sd, include_content=False)
            plan = build_plan(sd, "r")
            assert plan.content_file is None

    def test_build_plan_resolves_external_content_file(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._populate(sd)
            external_zip = Path(td) / "external.zip"
            external_zip.write_bytes(b"z")
            meta = json.loads((sd / "metadata.json").read_text())
            meta["content_file"] = str(external_zip)
            (sd / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
            plan = build_plan(sd, "r")
            assert plan.content_file == external_zip

    def test_build_plan_ignores_zero_byte_images(self):
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            self._populate(sd, include_images=False)
            (sd / "cover.png").write_bytes(b"")
            (sd / "thumbnail.png").write_bytes(b"")
            plan = build_plan(sd, "r")
            assert plan.cover_path is None
            assert plan.thumbnail_path is None


class TestGumroadSelectors:
    """Structural checks on the gumroad_selectors module."""

    def test_all_required_selectors_exported(self):
        import gumroad_selectors as gs
        required = [
            "NEW_PRODUCT_URL", "LOGIN_URL",
            "TAB_PRODUCT", "TAB_CONTENT", "TAB_CHECKOUT", "TAB_RECEIPT",
            "DESCRIPTION_EDITOR", "SUMMARY_INPUT", "TAGS_INPUT",
            "ADD_FEATURE_BUTTON", "FEATURE_INPUTS",
            "BUTTON_TEXT_INPUT", "RECEIPT_MESSAGE_EDITOR",
            "SAVE_DRAFT_BUTTON", "PRODUCT_ID_URL_PATTERN",
        ]
        for name in required:
            assert hasattr(gs, name), f"missing selector: {name}"
            assert getattr(gs, name), f"empty selector: {name}"

    def test_product_id_pattern_matches_typical_url(self):
        import re

        import gumroad_selectors as gs
        m = re.search(gs.PRODUCT_ID_URL_PATTERN,
                      "https://app.gumroad.com/products/abc123XYZ_-/edit")
        assert m is not None
        assert m.group(1) == "abc123XYZ_-"


class TestCaptureSelectors:
    """capture_selectors parses codegen output and rewrites gumroad_selectors.py."""

    def test_locator_expr_to_selector_string_mappings(self):
        cases = [
            ('get_by_role("textbox", name="Name")', 'role=textbox[name="Name"]'),
            ('get_by_role("button")', 'role=button'),
            ('get_by_label("Price")', 'label=Price'),
            ('get_by_placeholder("Describe")', 'placeholder=Describe'),
            ('get_by_test_id("submit")', 'data-testid=submit'),
            ('get_by_text("Save")', 'text=Save'),
            ('get_by_text("Save", exact=True)', 'text=Save'),
            ('locator("#product-name")', '#product-name'),
        ]
        for expr, expected in cases:
            assert locator_expr_to_selector_string(expr) == expected, f"failed on {expr}"

    def test_parse_capture_matches_sentinels_and_clicks(self):
        code = f'''
from playwright.sync_api import Page

def run(page: Page):
    page.get_by_text("Digital product").click()
    page.get_by_label("Name").fill("{SENTINELS['NEW_PRODUCT_NAME']}")
    page.get_by_label("Summary").fill("{SENTINELS['SUMMARY_INPUT']}")
    page.get_by_role("button", name="Save as draft").click()
'''
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(code)
            path = Path(f.name)
        try:
            result = parse_capture(path)
            names = {c.name for c in result.captures}
            assert "NEW_PRODUCT_NAME" in names
            assert "SUMMARY_INPUT" in names
            assert "PRODUCT_TYPE_DIGITAL" in names
            assert "SAVE_DRAFT_BUTTON" in names
            # Unmatched reported too
            assert "DESCRIPTION_EDITOR" in result.unmatched_sentinels
        finally:
            path.unlink()

    def test_apply_captures_rewrites_single_line(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mod = td / "gumroad_selectors.py"
            mod.write_text(
                'from typing import Final\n'
                'FOO: Final = "old-css"\n'
                'BAR: Final = "keep-me"\n',
                encoding="utf-8",
            )
            code = f'''
def r(page):
    page.get_by_label("Foo").fill("{SENTINELS['NEW_PRODUCT_NAME']}")
'''
            # Temp: map NEW_PRODUCT_NAME capture to FOO by renaming in the copy
            cap_file = td / "cap.py"
            cap_file.write_text(code, encoding="utf-8")
            result = parse_capture(cap_file)
            # Remap to match our test module
            result.captures[0].name = "FOO"
            diffs = apply_captures(result.captures, mod, backup=False)
            assert "FOO" in diffs
            content = mod.read_text()
            assert "FOO: Final = 'label=Foo'" in content
            assert 'BAR: Final = "keep-me"' in content  # untouched

    def test_apply_captures_rewrites_multiline_parenthesised(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mod = td / "gumroad_selectors.py"
            mod.write_text(
                'from typing import Final\n'
                'SAVE: Final = (\n'
                '    "button:has-text(\'Save\'), "\n'
                '    "button:has-text(\'Submit\')"\n'
                ')\n'
                'OTHER: Final = "untouched"\n',
                encoding="utf-8",
            )
            code = f'''
def r(page):
    page.get_by_role("button", name="Save as draft").click()
'''
            cap_file = td / "cap.py"
            cap_file.write_text(code, encoding="utf-8")
            result = parse_capture(cap_file)
            assert any(c.name == "SAVE_DRAFT_BUTTON" for c in result.captures)
            # Remap for test module key
            for c in result.captures:
                if c.name == "SAVE_DRAFT_BUTTON":
                    c.name = "SAVE"
            diffs = apply_captures(result.captures, mod, backup=False)
            content = mod.read_text()
            # Must be syntactically valid Python
            import ast
            ast.parse(content)
            assert "SAVE: Final = 'role=button[name=\"Save as draft\"]'" in content
            assert 'OTHER: Final = "untouched"' in content

    def test_apply_captures_skips_raw_fallbacks(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mod = td / "gumroad_selectors.py"
            original = 'from typing import Final\nFOO: Final = "keep-me"\n'
            mod.write_text(original, encoding="utf-8")
            # A capture whose expression we cannot map
            from capture_selectors import Capture
            cap = Capture(name="FOO", locator_expr="something_weird(xyz)",
                          matched_via="fill-sentinel")
            diffs = apply_captures([cap], mod, backup=False)
            assert diffs == {}
            assert mod.read_text() == original


class TestRepoNameExtraction:
    """Test repo name extraction from URLs."""

    def test_basic_url(self):
        from swindle import _repo_name_from_url
        assert _repo_name_from_url("https://github.com/m2ai-portfolio/my-tool") == "my-tool"

    def test_url_with_git_suffix(self):
        from swindle import _repo_name_from_url
        assert _repo_name_from_url("https://github.com/org/repo.git") == "repo"

    def test_url_with_trailing_slash(self):
        from swindle import _repo_name_from_url
        assert _repo_name_from_url("https://github.com/org/repo/") == "repo"


# --- LinkedIn Post Generator Tests ---

class TestLinkedInPostGenerator:
    """Test LinkedIn post generation."""

    SAMPLE_LISTING = "## What's Inside\nSource code and configs\n\n## The Problem\nIt's hard"
    SAMPLE_METADATA = {
        "title": "My Tool",
        "price": 0,
        "tags": ["developer-tools", "mcp"],
        "summary": "A great developer tool",
        "repo_url": "https://github.com/m2ai-portfolio/my-tool",
        "generated_at": "2026-03-26T12:00:00",
    }
    SAMPLE_DRAFT = (
        "I built a thing.\n\n"
        "My Tool solves a real problem for developers.\n\n"
        "---\n\n"
        "Grab it free: {{GUMROAD_URL}}"
    )

    def _setup_staging(self, tmpdir: Path, repo_name: str = "my-tool"):
        """Create a staging dir with listing.md and metadata.json."""
        staging = tmpdir / repo_name
        staging.mkdir(parents=True)
        (staging / "listing.md").write_text(self.SAMPLE_LISTING, encoding="utf-8")
        (staging / "metadata.json").write_text(
            json.dumps(self.SAMPLE_METADATA, indent=2), encoding="utf-8"
        )
        return staging

    @patch("linkedin_post_generator.requests.post")
    def test_reads_listing_and_metadata(self, mock_post):
        """generate_linkedin_post reads listing.md and metadata.json from staging dir."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": self.SAMPLE_DRAFT}}]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            self._setup_staging(tmpdir)

            with (
                patch("linkedin_post_generator.STAGING_DIR", tmpdir),
                patch.dict(os.environ, {"DEEPINFRA_API_KEY": "test-key"}),
            ):
                from linkedin_post_generator import generate_linkedin_post
                generate_linkedin_post("my-tool")

            # Verify the LLM was called (meaning files were read)
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            prompt_content = body["messages"][1]["content"]
            # The prompt should contain content from the listing
            assert "What's Inside" in prompt_content or "My Tool" in prompt_content

    @patch("linkedin_post_generator.requests.post")
    def test_saves_draft_to_staging(self, mock_post):
        """generate_linkedin_post saves linkedin_draft.md to the staging dir."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": self.SAMPLE_DRAFT}}]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            self._setup_staging(tmpdir)

            with (
                patch("linkedin_post_generator.STAGING_DIR", tmpdir),
                patch.dict(os.environ, {"DEEPINFRA_API_KEY": "test-key"}),
            ):
                from linkedin_post_generator import generate_linkedin_post
                generate_linkedin_post("my-tool")

            draft_path = tmpdir / "my-tool" / "linkedin_draft.md"
            assert draft_path.exists()
            content = draft_path.read_text(encoding="utf-8")
            assert len(content) > 0

    @patch("linkedin_post_generator.requests.post")
    def test_draft_contains_cta_separator(self, mock_post):
        """Draft should contain the CTA format separated by ---."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": self.SAMPLE_DRAFT}}]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            self._setup_staging(tmpdir)

            with (
                patch("linkedin_post_generator.STAGING_DIR", tmpdir),
                patch.dict(os.environ, {"DEEPINFRA_API_KEY": "test-key"}),
            ):
                from linkedin_post_generator import generate_linkedin_post
                result = generate_linkedin_post("my-tool")

            assert "---" in result

    @patch("linkedin_post_generator.requests.post")
    def test_returns_draft_text(self, mock_post):
        """generate_linkedin_post returns the draft content string."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": self.SAMPLE_DRAFT}}]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            self._setup_staging(tmpdir)

            with (
                patch("linkedin_post_generator.STAGING_DIR", tmpdir),
                patch.dict(os.environ, {"DEEPINFRA_API_KEY": "test-key"}),
            ):
                from linkedin_post_generator import generate_linkedin_post
                result = generate_linkedin_post("my-tool")

            assert isinstance(result, str)
            assert "I built a thing" in result

    def test_raises_on_missing_staging_dir(self):
        """generate_linkedin_post raises FileNotFoundError for missing staging dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            with patch("linkedin_post_generator.STAGING_DIR", tmpdir):
                from linkedin_post_generator import generate_linkedin_post
                with pytest.raises(FileNotFoundError, match="Staging directory not found"):
                    generate_linkedin_post("nonexistent")

    def test_raises_on_missing_listing(self):
        """generate_linkedin_post raises FileNotFoundError if listing.md is missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            staging = tmpdir / "my-tool"
            staging.mkdir()
            # No listing.md
            (staging / "metadata.json").write_text("{}", encoding="utf-8")

            with patch("linkedin_post_generator.STAGING_DIR", tmpdir):
                from linkedin_post_generator import generate_linkedin_post
                with pytest.raises(FileNotFoundError, match="listing.md not found"):
                    generate_linkedin_post("my-tool")

    @patch("linkedin_post_generator.requests.post")
    def test_strips_code_fences(self, mock_post):
        """Draft should have markdown code fences stripped."""
        fenced_draft = "```markdown\n" + self.SAMPLE_DRAFT + "\n```"
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": fenced_draft}}]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            self._setup_staging(tmpdir)

            with (
                patch("linkedin_post_generator.STAGING_DIR", tmpdir),
                patch.dict(os.environ, {"DEEPINFRA_API_KEY": "test-key"}),
            ):
                from linkedin_post_generator import generate_linkedin_post
                result = generate_linkedin_post("my-tool")

            assert not result.startswith("```")
            assert not result.endswith("```")


# --- Approve Flow Tests ---

class TestApproveFlow:
    """Test the approve command's LinkedIn draft integration."""

    def setup_method(self):
        self.runner = CliRunner()
        from swindle import cli
        self.cli = cli

    @patch("swindle.generate_linkedin_post", create=True)
    def test_approve_generates_linkedin_draft(self, mock_gen):
        """Approve generates a LinkedIn draft when one doesn't exist."""
        mock_gen.return_value = "Draft content here"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            staging = tmpdir / "test-repo"
            staging.mkdir(parents=True)

            db = SwindleDB(":memory:")
            db.init_db()
            db.add_listing("test-repo", "https://url/test", "Test", None, str(staging))

            with (
                patch("swindle._get_db", return_value=db),
                patch("swindle.STAGING_DIR", tmpdir),
                patch.object(db, "close"),  # Keep in-memory DB alive
            ):
                result = self.runner.invoke(self.cli, ["approve", "test-repo"])

            assert result.exit_code == 0
            assert "Approved: test-repo" in result.output

            listing = db.get_by_name("test-repo")
            assert listing["status"] == "approved"
            db.close()

    def test_approve_skips_existing_draft(self):
        """Approve skips draft generation when linkedin_draft.md already exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            staging = tmpdir / "test-repo"
            staging.mkdir(parents=True)
            # Pre-create the draft file
            (staging / "linkedin_draft.md").write_text(
                "Existing draft", encoding="utf-8"
            )

            db = SwindleDB(":memory:")
            db.init_db()
            db.add_listing("test-repo", "https://url/test", "Test", None, str(staging))

            with (
                patch("swindle._get_db", return_value=db),
                patch("swindle.STAGING_DIR", tmpdir),
                patch.object(db, "close"),  # Keep in-memory DB alive
            ):
                result = self.runner.invoke(self.cli, ["approve", "test-repo"])

            assert result.exit_code == 0
            assert "LinkedIn draft already exists" in result.output

            # Verify existing draft was not overwritten
            content = (staging / "linkedin_draft.md").read_text(encoding="utf-8")
            assert content == "Existing draft"
            db.close()


# --- Publish Command Tests ---

class TestPublishCommand:
    """Test the publish command."""

    def setup_method(self):
        self.runner = CliRunner()
        from swindle import cli
        self.cli = cli

    def test_publish_updates_status(self):
        """Publish updates status from 'approved' to 'published'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            staging = tmpdir / "test-repo"
            staging.mkdir(parents=True)

            db = SwindleDB(":memory:")
            db.init_db()
            db.add_listing("test-repo", "https://url/test", "Test", None, str(staging))
            db.update_status("test-repo", "approved")

            with (
                patch("swindle._get_db", return_value=db),
                patch("swindle.STAGING_DIR", tmpdir),
                patch.object(db, "close"),  # Keep in-memory DB alive
            ):
                result = self.runner.invoke(
                    self.cli, ["publish", "test-repo", "--gumroad-url", "https://gumroad.com/l/test"]
                )

            assert result.exit_code == 0
            assert "Published: test-repo" in result.output

            listing = db.get_by_name("test-repo")
            assert listing["status"] == "published"
            db.close()

    def test_publish_stores_gumroad_url(self):
        """Publish stores the gumroad_url in the database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            staging = tmpdir / "test-repo"
            staging.mkdir(parents=True)

            db = SwindleDB(":memory:")
            db.init_db()
            db.add_listing("test-repo", "https://url/test", "Test", None, str(staging))
            db.update_status("test-repo", "approved")

            with (
                patch("swindle._get_db", return_value=db),
                patch("swindle.STAGING_DIR", tmpdir),
                patch.object(db, "close"),  # Keep in-memory DB alive
            ):
                result = self.runner.invoke(
                    self.cli, ["publish", "test-repo", "--gumroad-url", "https://gumroad.com/l/test"]
                )

            assert result.exit_code == 0
            listing = db.get_by_name("test-repo")
            assert listing["gumroad_url"] == "https://gumroad.com/l/test"
            db.close()

    def test_publish_fails_if_not_approved(self):
        """Publish fails if listing is not in 'approved' status."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            staging = tmpdir / "test-repo"
            staging.mkdir(parents=True)

            db = SwindleDB(":memory:")
            db.init_db()
            db.add_listing("test-repo", "https://url/test", "Test", None, str(staging))
            # Status is 'staged', not 'approved'

            with (
                patch("swindle._get_db", return_value=db),
                patch("swindle.STAGING_DIR", tmpdir),
            ):
                result = self.runner.invoke(
                    self.cli, ["publish", "test-repo", "--gumroad-url", "https://gumroad.com/l/test"]
                )

            assert result.exit_code != 0
            assert "not 'approved'" in result.output
            db.close()

    def test_publish_updates_placeholder_in_draft(self):
        """Publish replaces {{GUMROAD_URL}} placeholder in linkedin_draft.md."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            staging = tmpdir / "test-repo"
            staging.mkdir(parents=True)

            draft_content = "Check it out: {{GUMROAD_URL}}\nGreat tool."
            (staging / "linkedin_draft.md").write_text(draft_content, encoding="utf-8")

            db = SwindleDB(":memory:")
            db.init_db()
            db.add_listing("test-repo", "https://url/test", "Test", None, str(staging))
            db.update_status("test-repo", "approved")

            with (
                patch("swindle._get_db", return_value=db),
                patch("swindle.STAGING_DIR", tmpdir),
            ):
                result = self.runner.invoke(
                    self.cli, ["publish", "test-repo", "--gumroad-url", "https://gumroad.com/l/test"]
                )

            assert result.exit_code == 0
            assert "LinkedIn draft updated with Gumroad URL" in result.output

            updated = (staging / "linkedin_draft.md").read_text(encoding="utf-8")
            assert "{{GUMROAD_URL}}" not in updated
            assert "https://gumroad.com/l/test" in updated
            db.close()

    def test_publish_no_draft_no_error(self):
        """Publish succeeds even when no LinkedIn draft exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            staging = tmpdir / "test-repo"
            staging.mkdir(parents=True)
            # No linkedin_draft.md

            db = SwindleDB(":memory:")
            db.init_db()
            db.add_listing("test-repo", "https://url/test", "Test", None, str(staging))
            db.update_status("test-repo", "approved")

            with (
                patch("swindle._get_db", return_value=db),
                patch("swindle.STAGING_DIR", tmpdir),
            ):
                result = self.runner.invoke(
                    self.cli, ["publish", "test-repo", "--gumroad-url", "https://gumroad.com/l/test"]
                )

            assert result.exit_code == 0
            assert "No LinkedIn draft found" in result.output
            db.close()

    def test_publish_nonexistent_listing(self):
        """Publish fails for a listing that doesn't exist."""
        db = SwindleDB(":memory:")
        db.init_db()

        with patch("swindle._get_db", return_value=db):
            result = self.runner.invoke(
                self.cli, ["publish", "nonexistent", "--gumroad-url", "https://gumroad.com/l/x"]
            )

        assert result.exit_code != 0
        assert "No listing found" in result.output
        db.close()


# --- DB Migration Tests ---

class TestDBMigration:
    """Test that new columns exist after init_db."""

    def test_linkedin_draft_path_column_exists(self):
        """linkedin_draft_path column exists after init_db."""
        db = SwindleDB(":memory:")
        db.init_db()

        db.connect()
        cursor = db.conn.execute("PRAGMA table_info(listings)")
        columns = {row["name"] for row in cursor.fetchall()}
        assert "linkedin_draft_path" in columns
        db.close()

    def test_gumroad_url_column_exists(self):
        """gumroad_url column exists after init_db."""
        db = SwindleDB(":memory:")
        db.init_db()

        db.connect()
        cursor = db.conn.execute("PRAGMA table_info(listings)")
        columns = {row["name"] for row in cursor.fetchall()}
        assert "gumroad_url" in columns
        db.close()

    def test_migration_adds_columns_to_existing_db(self):
        """Migration adds new columns to a DB created without them."""
        db = SwindleDB(":memory:")
        db.connect()
        # Create the old schema (without linkedin_draft_path and gumroad_url)
        db.conn.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_name TEXT UNIQUE,
                repo_url TEXT,
                title TEXT,
                status TEXT DEFAULT 'staged',
                spec_path TEXT,
                staging_dir TEXT,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TIMESTAMP
            )
        """)
        db.conn.commit()

        # Verify columns don't exist yet
        cursor = db.conn.execute("PRAGMA table_info(listings)")
        old_columns = {row["name"] for row in cursor.fetchall()}
        assert "linkedin_draft_path" not in old_columns
        assert "gumroad_url" not in old_columns

        # Run init_db which triggers migration
        db.init_db()

        cursor = db.conn.execute("PRAGMA table_info(listings)")
        new_columns = {row["name"] for row in cursor.fetchall()}
        assert "linkedin_draft_path" in new_columns
        assert "gumroad_url" in new_columns
        db.close()

    def test_update_linkedin_draft_path(self):
        """update_linkedin_draft_path stores the path in the DB."""
        db = SwindleDB(":memory:")
        db.init_db()
        db.add_listing("repo-a", "https://url/a", "A", None, "/tmp/a")

        result = db.update_linkedin_draft_path("repo-a", "/tmp/a/linkedin_draft.md")
        assert result is True

        listing = db.get_by_name("repo-a")
        assert listing["linkedin_draft_path"] == "/tmp/a/linkedin_draft.md"
        db.close()

    def test_update_gumroad_url(self):
        """update_gumroad_url stores the URL in the DB."""
        db = SwindleDB(":memory:")
        db.init_db()
        db.add_listing("repo-a", "https://url/a", "A", None, "/tmp/a")

        result = db.update_gumroad_url("repo-a", "https://gumroad.com/l/test")
        assert result is True

        listing = db.get_by_name("repo-a")
        assert listing["gumroad_url"] == "https://gumroad.com/l/test"
        db.close()
