# Swindle: Quality Gate + Playwright Automation

## Context

Session on 2026-04-02/03 reviewed all 21 staged Gumroad listings. Fixed 2 corrupted listings (march-2026-power-rankings, personalokr). 19 are ready for HIL review, 7 of those are missing cover images. Matthew will review and approve the batch, then we build automation.

## Task 1: Add Quality Gate to Listing Generator

The listing_generator.py has no quality checks -- corrupted listings (placeholder metadata, incomplete sections, LLM reasoning artifacts) slip through silently. Add a `validate_listing()` function that runs after generation and rejects bad output.

Checks to implement (based on real failures found):
- SUMMARY line present and under 140 chars
- TAGS line present with 3+ tags
- No placeholder text ("output here", "REPLACE THIS", "<text>", "lorem ipsum")
- No LLM reasoning artifacts ("let me think", "character count", "on second thought")
- Has all required sections (What's Inside, The Problem, How It Works, Get Started)
- No empty/stub sections (## with no content after it)
- metadata.json has non-empty summary and tags (not ["tag1","tag2","tag3"])

Reference: The model-audit benchmark checks at `~/projects/model-audit/benchmarks/listing_copy.json` have similar validation patterns.

Files:
- `~/projects/swindle/listing_generator.py` -- add validate_listing() and wire into generation pipeline
- `~/projects/swindle/tests/` -- add tests for the validator

## Task 2: Playwright Automation for Gumroad Publishing

Currently blocked on: Gumroad API POST returns 404 for product creation. Playwright browser automation is the workaround.

Build a `gumroad_publisher.py` that:
1. Logs into Gumroad via Playwright (credentials in ~/.env.shared if available, or prompt)
2. For each approved listing in staging/:
   - Creates a new product (free, digital)
   - Sets title, description (from listing.md), summary, tags (from metadata.json)
   - Uploads cover image if present
   - Saves as DRAFT (not published -- Matthew publishes manually)
3. Records which listings were pushed in a `staging/published.json` manifest
4. Supports `--dry-run` to show what would be created without browser actions

Important: This is Phase G (Scale) work from the L5 roadmap. The Swindle memory has more context: `~/.claude/projects/-home-apexaipc/memory/swindle-project.md`

## Start Command

```
cd ~/projects/swindle
# Read CLAUDE.md first, then listing_generator.py to understand existing code
# Task 1 first (quality gate), then Task 2 (Playwright)
```
