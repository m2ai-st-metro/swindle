"""Tests for short-title generation and the retitle CLI command."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent))

from db import SwindleDB
from listing_generator import (
    SHORT_TITLE_MAX_CHARS,
    _fallback_short_title,
    _parse_short_title,
    _title_is_valid,
    generate_short_title,
)
from validator import TITLE_MAX_CHARS, TITLE_MAX_WORDS, validate_listing


class TestTitleParser:
    def test_strips_prefix_labels(self):
        assert _parse_short_title("Output: TokenSearch") == "TokenSearch"
        assert _parse_short_title("Title: ClipForge") == "ClipForge"
        assert _parse_short_title("Product Name: Argus") == "Argus"

    def test_strips_quotes_and_trailing_punct(self):
        assert _parse_short_title('"HalluHunter"') == "HalluHunter"
        assert _parse_short_title("HipaaMCP.") == "HipaaMCP"
        assert _parse_short_title("'convometrics'") == "convometrics"

    def test_skips_explanation_lines(self):
        output = "Here is the name:\nPlateauBench\nLet me know if you want alternatives."
        # Parser takes the first usable line. 'Here is' gets skipped; 'Plateau...' wins.
        assert _parse_short_title(output) == "PlateauBench"

    def test_returns_empty_on_blank(self):
        assert _parse_short_title("") == ""
        assert _parse_short_title("   \n\n  ") == ""


class TestTitleValidation:
    def test_accepts_punchy_names(self):
        assert _title_is_valid("AgentTrace")
        assert _title_is_valid("convometrics")
        assert _title_is_valid("Clip Forge")
        assert _title_is_valid("Power Rankings")

    def test_rejects_over_length(self):
        assert not _title_is_valid("a" * (SHORT_TITLE_MAX_CHARS + 1))

    def test_rejects_too_many_words(self):
        assert not _title_is_valid("One Two Three Four")

    def test_rejects_empty(self):
        assert not _title_is_valid("")

    def test_rejects_explanation_fragments(self):
        assert not _title_is_valid("Here is the name")
        assert not _title_is_valid("Example Output")


class TestFallback:
    def test_falls_back_to_current_if_valid(self):
        result = _fallback_short_title("ClipForge", "clip-forge")
        assert result == "ClipForge"

    def test_falls_back_to_repo_slug_if_current_too_long(self):
        long = "A really long current title that cannot be used"
        result = _fallback_short_title(long, "clip-forge")
        assert result == "Clip Forge"

    def test_truncates_when_both_unusable(self):
        long_current = "X" * 200
        long_repo = "some-super-duper-mega-repo-name-that-goes-on-forever"
        result = _fallback_short_title(long_current, long_repo)
        assert len(result) <= SHORT_TITLE_MAX_CHARS


class TestGenerateShortTitle:
    def test_happy_path_uses_llm_output(self):
        with patch("listing_generator._call_llm", return_value="TokenSearch\n"):
            result = generate_short_title(
                repo_name="token-efficient-search-api-for-ai",
                current_title="Token Efficient Search Api For Ai",
                summary="A search API.",
                spec_path=None,
            )
            assert result == "TokenSearch"

    def test_falls_back_when_llm_output_too_long(self):
        bad = "This Is Actually A Very Long Name That Exceeds The Limits"
        with patch("listing_generator._call_llm", return_value=bad):
            result = generate_short_title(
                repo_name="foo-bar",
                current_title="Foo Bar",
                summary="",
                spec_path=None,
            )
            # Should fall back to the current (valid) title
            assert result == "Foo Bar"

    def test_falls_back_when_llm_output_empty(self):
        with patch("listing_generator._call_llm", return_value=""):
            result = generate_short_title(
                repo_name="foo-bar",
                current_title="Foo Bar",
                summary="",
                spec_path=None,
            )
            assert result == "Foo Bar"


class TestValidatorTitleLength:
    def _stage(self, tmp: Path, title: str) -> Path:
        sd = tmp / "test-repo"
        sd.mkdir(parents=True)
        (sd / "listing.md").write_text("x" * 200, encoding="utf-8")
        (sd / "metadata.json").write_text(json.dumps({
            "title": title,
            "summary": "valid summary here",
            "tags": ["a", "b", "c"],
            "repo_url": "https://github.com/x/test-repo",
        }), encoding="utf-8")
        (sd / "features.txt").write_text("One\nTwo\nThree\n", encoding="utf-8")
        (sd / "button_text.txt").write_text("Grab it", encoding="utf-8")
        (sd / "receipt_message.md").write_text("Thanks.", encoding="utf-8")
        (sd / "cover.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (sd / "thumbnail.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        return sd

    def test_accepts_short_title(self):
        with tempfile.TemporaryDirectory() as td:
            sd = self._stage(Path(td), "AgentTrace")
            failures = validate_listing(sd)
            assert not any("title" in f.lower() for f in failures), failures

    def test_flags_over_length_title(self):
        with tempfile.TemporaryDirectory() as td:
            sd = self._stage(Path(td), "x" * (TITLE_MAX_CHARS + 1))
            failures = validate_listing(sd)
            assert any("metadata.title too long" in f for f in failures)

    def test_flags_over_word_count_title(self):
        with tempfile.TemporaryDirectory() as td:
            sd = self._stage(
                Path(td),
                " ".join(["word"] * (TITLE_MAX_WORDS + 1)),
            )
            failures = validate_listing(sd)
            assert any(f"{TITLE_MAX_WORDS + 1} words" in f for f in failures)


class TestRetitleCommand:
    def setup_method(self):
        self.runner = CliRunner()
        from swindle import cli
        self.cli = cli

    def _write_staging(self, tmp: Path, repo: str, title: str) -> Path:
        sd = tmp / repo
        sd.mkdir(parents=True)
        (sd / "metadata.json").write_text(json.dumps({
            "title": title,
            "summary": "A thing.",
            "tags": ["a", "b", "c"],
            "repo_url": f"https://github.com/x/{repo}",
        }, indent=2), encoding="utf-8")
        return sd

    def test_llm_path_rewrites_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sd = self._write_staging(tmp, "mouthful-repo", "Mouthful Name Here For AI Tooling")

            db = SwindleDB(":memory:")
            db.init_db()
            db.add_listing(
                "mouthful-repo",
                "https://github.com/x/mouthful-repo",
                "Mouthful Name Here For AI Tooling",
                None,
                str(sd),
            )

            with (
                patch("swindle.STAGING_DIR", tmp),
                patch("swindle._get_db", return_value=db),
                patch.object(db, "close"),
                patch("listing_generator._call_llm", return_value="MouthSnap"),
            ):
                result = self.runner.invoke(self.cli, ["retitle", "mouthful-repo"])

            assert result.exit_code == 0, result.output
            assert "MouthSnap" in result.output
            data = json.loads((sd / "metadata.json").read_text())
            assert data["title"] == "MouthSnap"
            listing = db.get_by_name("mouthful-repo")
            assert listing["title"] == "MouthSnap"
            db.close()

    def test_override_path_skips_llm(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sd = self._write_staging(tmp, "foo", "Some Long Existing Title")

            db = SwindleDB(":memory:")
            db.init_db()
            db.add_listing("foo", "https://github.com/x/foo", "Some Long Existing Title", None, str(sd))

            with (
                patch("swindle.STAGING_DIR", tmp),
                patch("swindle._get_db", return_value=db),
                patch.object(db, "close"),
                patch("listing_generator._call_llm") as llm,
            ):
                result = self.runner.invoke(
                    self.cli, ["retitle", "foo", "--new-title", "ShinyName"]
                )

            assert result.exit_code == 0, result.output
            llm.assert_not_called()
            data = json.loads((sd / "metadata.json").read_text())
            assert data["title"] == "ShinyName"
            db.close()

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sd = self._write_staging(tmp, "foo", "Old Title Here")
            before = (sd / "metadata.json").read_text()

            with (
                patch("swindle.STAGING_DIR", tmp),
                patch("listing_generator._call_llm", return_value="NewName"),
            ):
                result = self.runner.invoke(
                    self.cli, ["retitle", "foo", "--dry-run"]
                )

            assert result.exit_code == 0
            assert "dry-run" in result.output.lower()
            after = (sd / "metadata.json").read_text()
            assert before == after

    def test_unchanged_title_is_a_noop(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sd = self._write_staging(tmp, "foo", "Snappy")

            with (
                patch("swindle.STAGING_DIR", tmp),
                patch("listing_generator._call_llm", return_value="Snappy"),
            ):
                result = self.runner.invoke(self.cli, ["retitle", "foo"])

            assert result.exit_code == 0
            assert "unchanged" in result.output.lower()

    def test_missing_metadata_errors(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with patch("swindle.STAGING_DIR", tmp):
                result = self.runner.invoke(self.cli, ["retitle", "nonexistent"])
            assert result.exit_code != 0
            assert "metadata.json not found" in result.output
