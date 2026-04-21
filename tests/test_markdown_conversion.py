"""Tests for markdown-to-HTML conversion in the Gumroad description payload."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from swindle import _markdown_to_html, _plan_to_product_plan
from publish_plan import PublishPlan


def _plan(description_md: str, features: list[str] | None = None,
          receipt: str = "Thanks.") -> PublishPlan:
    return PublishPlan(
        repo_name="test",
        title="Test Product",
        summary="A test product.",
        price_cents=0,
        tags=["a", "b", "c"],
        description_md=description_md,
        features=features or [],
        button_text="Grab it",
        receipt_message=receipt,
        cover_path=None,
        thumbnail_path=None,
        content_file=None,
        staging_dir=Path("/tmp/nope"),
    )


class TestMarkdownToHTML:
    def test_headers_become_h_tags(self):
        html = _markdown_to_html("# H1\n\n## H2\n\n### H3\n")
        assert "<h1>H1</h1>" in html
        assert "<h2>H2</h2>" in html
        assert "<h3>H3</h3>" in html

    def test_bullets_become_ul_li(self):
        html = _markdown_to_html("- foo\n- bar\n- baz")
        assert "<ul>" in html
        assert "<li>foo</li>" in html
        assert "<li>bar</li>" in html

    def test_bold_and_italic(self):
        html = _markdown_to_html("**bold** and *italic*")
        assert "<strong>bold</strong>" in html
        assert "<em>italic</em>" in html

    def test_inline_code(self):
        html = _markdown_to_html("use `npx foo` to run")
        assert "<code>npx foo</code>" in html

    def test_fenced_code_block(self):
        md_input = "```\nsome code\n```"
        html = _markdown_to_html(md_input)
        assert "<pre>" in html or "<code>" in html

    def test_links_are_anchors(self):
        html = _markdown_to_html("[repo](https://example.com)")
        assert '<a href="https://example.com">repo</a>' in html

    def test_paragraph_wrapping(self):
        html = _markdown_to_html("First paragraph.\n\nSecond paragraph.")
        assert "<p>First paragraph.</p>" in html
        assert "<p>Second paragraph.</p>" in html

    def test_handles_empty_string(self):
        assert _markdown_to_html("") == ""


class TestPlanConversion:
    def test_description_is_html(self):
        plan = _plan(
            description_md="# Title\n\nSome text with **bold**.\n\n## Why\n\n- reason one\n- reason two",
            features=["Fast", "Free", "Fun"],
        )
        api_plan = _plan_to_product_plan(plan, file_url=None)
        assert "<h1>Title</h1>" in api_plan.description
        assert "<strong>bold</strong>" in api_plan.description
        assert "<h2>Why</h2>" in api_plan.description
        assert "<li>reason one</li>" in api_plan.description

    def test_includes_section_rendered_as_html_list(self):
        plan = _plan(
            description_md="Body text.",
            features=["Alpha feature", "Beta feature", "Gamma feature"],
        )
        api_plan = _plan_to_product_plan(plan, file_url=None)
        assert "<h2>Includes</h2>" in api_plan.description
        assert "<li>Alpha feature</li>" in api_plan.description
        assert "<li>Beta feature</li>" in api_plan.description
        assert "<li>Gamma feature</li>" in api_plan.description

    def test_no_features_no_includes_section(self):
        plan = _plan(description_md="Body.", features=[])
        api_plan = _plan_to_product_plan(plan, file_url=None)
        assert "Includes" not in api_plan.description

    def test_receipt_also_converted_to_html(self):
        plan = _plan(
            description_md="Body.",
            receipt="Thanks. Run `npx foo`. See [docs](https://x).",
        )
        api_plan = _plan_to_product_plan(plan, file_url=None)
        assert "<code>npx foo</code>" in api_plan.custom_receipt
        assert '<a href="https://x">docs</a>' in api_plan.custom_receipt

    def test_empty_receipt_stays_empty(self):
        plan = _plan(description_md="Body.", receipt="")
        api_plan = _plan_to_product_plan(plan, file_url=None)
        assert api_plan.custom_receipt == ""

    def test_summary_is_not_converted(self):
        plan = _plan(description_md="Body.")
        api_plan = _plan_to_product_plan(plan, file_url=None)
        # Summary stays plain text — Gumroad's summary field is short/plain
        assert "<" not in api_plan.custom_summary
        assert api_plan.custom_summary == "A test product."
