# Swindle: Gumroad API client + auto-publish wiring

**Picking up from session 2026-04-21.** Read this file fully before writing any code. It summarizes four sessions of setbacks, pivots, and decisions so you do not repeat them.

## TL;DR

- Swindle's publisher is now unblocked by a real Gumroad API (not available on 2026-04-07, shipped 2026-04-06). Everything Playwright-related is dead.
- Fabrication mitigations are in place (hardened prompt + numeric-claim validator + Qwen 2.5-72B).
- **Goal this session: build `gumroad_api_client.py`, wire it into `swindle.py publish`, smoke-test on mythos-jr, then backfill 15 staged ST Metro listings on auto-publish.**
- Matthew's explicit policy: products appear without him touching them. Auto-publish is the intended end state.

## State at handoff (commits on `main`, Swindle repo)

| SHA | What |
|-----|------|
| `d9a591d` | Fabrication mitigations (prompt + validator + Qwen) |
| `a18c60c` | Capture CLI aborts cleanly on X-display / codegen failure |
| `23a848e` | CLI + upload sentinel coverage for capture_selectors |
| `ec1448a` | Quality gate + Playwright publisher scaffolding |

Tests: **90 pass, 0 fail** at `d9a591d`. `Qwen/Qwen2.5-72B-Instruct` is the model for both listing + LinkedIn copy.

## Setbacks we already paid for (do not repeat)

1. **Headless ProBook has no X server.** Running `playwright codegen` from an SSH session fails with `Missing X server or $DISPLAY`. We were about to install VcXsrv on the Surface tablet and use `ssh -Y` when we discovered the API path. **Do not spin up X forwarding.**
2. **Playwright branch consumed ~1,100 lines of code for nothing.** `gumroad_publisher.py`, `gumroad_selectors.py`, `capture_selectors.py` are all shelved. Selectors were unverified. Upload inputs could not be captured via codegen. X11 forwarding was a prerequisite. Sunk cost; do not resurrect. If anything, the walkthrough structure in `capture_selectors.py` SENTINELS dict is a useful reference for which Gumroad fields the API client needs to populate.
3. **Gumroad docs are stale.** `gumroad.com/api` and the Mintlify docs still say product creation is not implemented. **They are wrong.** Source of truth is the Rails code on `main` at `antiwork/gumroad`. Use GitHub code search, not the docs.
4. **Memory was stale about metroplex models.** Memory said Mistral Large was the metroplex spec-expander default. Actual default in code at `/home/apexaipc/projects/metroplex/gates/llm_expander.py:345` is `Qwen/Qwen2.5-72B-Instruct`. Code is authoritative. Always grep before trusting memory-derived model claims.
5. **SYSTEM_PROMPT fights the user template.** There is a system prompt constant in `listing_generator.py` that previously carried a worked example `"saves 2 hours/week"`. That example alone was the single biggest driver of Mistral Small fabrication. **We fixed it, but remember: system-prompt examples override user-prompt rules.** If future fabrication bugs appear, check `SYSTEM_PROMPT` first.
6. **Post-agent-verify hook false positives.** The hook sometimes lists dirty state in `m2ai-website-react` as if the subagent touched it. That repo has an unrelated ongoing rebrand WIP. Always verify file mtimes + git log before assuming a subagent went out of scope.
7. **Claude for Chrome (c4c) works but is not auto-publish-compatible.** It needs Chrome open + a human at the keyboard. Considered, rejected for auto-publish. Keep as manual fallback only.
8. **Metadata.json did not carry `spec_path` before `d9a591d`.** Pre-Qwen staged listings in `staging/` lack this field. Validator strict mode will fail them. **Any listing in `staging/` with no `spec_path` must be regenerated via `swindle.py prepare --spec-path` before publishing.**

## Decisions locked in (do not relitigate)

- **Playwright branch is dead.** Gumroad API is the path.
- **Model**: `Qwen/Qwen2.5-72B-Instruct` via DeepInfra. Already in the stack. Do not swap again.
- **HIL policy: auto-publish.** Validator is the gate. If validator passes, publish. No human review step.
- **Validator is strict by default.** No `spec_path` in metadata = fail. This is the right behavior for auto-publish.
- **Prompt hardening stays.** `templates/listing_prompt.txt` has STRICT SOURCING RULES at the top. Do not soften.
- **Auth**: `GUMROAD_ACCESS_TOKEN` from `~/.env.shared`. OAuth with `edit_products` scope. Confirmed working via live probe (`POST /v2/products` returned 200 and created a real product on 2026-04-21, which was then deleted cleanly via `DELETE /v2/products/:id`).

## Actual tasks this session

### Task 1: Build `gumroad_api_client.py`

File: `/home/apexaipc/projects/swindle/gumroad_api_client.py`

Functions needed (roughly):

```python
def create_product(plan: ProductPlan) -> ProductRef           # POST /v2/products
def upload_file(path: Path) -> str                            # presign + multipart + complete -> returns file_url
def set_cover(product_id: str, image_path: Path) -> None      # cover endpoint (PR #4311)
def set_thumbnail(product_id: str, image_path: Path) -> None  # thumbnail endpoint (PR #4311)
def publish(product_id: str) -> None                          # existing "enable" endpoint
def delete_product(product_id: str) -> None                   # for test cleanup
```

Reference the Rails source for exact field shapes:
- `github.com/antiwork/gumroad/blob/main/app/controllers/api/v2/links_controller.rb` (create endpoint at line 58)
- `github.com/antiwork/gumroad/blob/main/config/routes.rb` (route definitions)
- PR #4267 (product create endpoint merge, 2026-04-06)
- PR #4069 (presigned S3 upload, 2026-03-30): `POST /v2/files/presign` returns `{upload_id, key, file_url, parts: [{part_number, url}]}`. Client PUTs each part to its presigned S3 URL, collects ETags. `POST /v2/files/complete` with `{upload_id, key, parts: [{part_number, etag}]}` finalizes. Use `file_url` in the product's `files[]`.
- PR #4311 (thumbnail + cover endpoints, 2026-04-07)
- PR #4315 (rate limits, 2026-04-03): creates capped at 10/min and 50/9hr. Seller-tier daily cap of 10 or 100 products.

Auth: `Authorization: Bearer $GUMROAD_ACCESS_TOKEN` header. The probe used `?access_token=<token>` as query param and that also worked, but header is standard.

Base URL: `https://api.gumroad.com/v2/`

Write tests with `responses` library (already in `requirements.txt` or add it): mock the HTTP endpoints, verify request shapes for presign → upload → complete → create → publish.

### Task 2: Wire `swindle.py publish`

Currently `swindle.py` imports `GumroadPublisher`, `PlanError`, `build_plan` from `gumroad_publisher.py` (the Playwright thing). Replace those imports + the `publish` command body to call `gumroad_api_client.py` instead.

Flow inside `publish <repo-name>`:
1. Read `staging/<repo>/metadata.json`
2. Run `validator.validate_listing(staging_dir)` — must return empty list (strict)
3. Build `ProductPlan` from staging files
4. `create_product(plan)` → get product_id
5. `upload_file` for content.zip → get file_url, attach to product via update
6. `set_cover`, `set_thumbnail`
7. `publish(product_id)`
8. Update Swindle DB: mark published, store gumroad_url
9. Generate LinkedIn draft (existing path, untouched)

Keep `gumroad_publisher.py`, `gumroad_selectors.py`, `capture_selectors.py` in the repo for now but move them to a `_shelved/` subdirectory or mark them deprecated in file headers. Do NOT delete yet — they may inform the API client if you hit ambiguous field mappings.

### Task 3: Smoke-test on mythos-jr

mythos-jr staging was regenerated on 2026-04-21 with the new prompt + Qwen + spec_path. Quality gate passed. Artifacts:
- `staging/mythos-jr/listing.md` ✓
- `staging/mythos-jr/metadata.json` ✓ (has spec_path)
- `staging/mythos-jr/features.txt` ✓
- `staging/mythos-jr/button_text.txt` ✓
- `staging/mythos-jr/receipt_message.md` ✓
- `staging/mythos-jr/cover.png` — from the pre-Qwen run. May be fine.
- `staging/mythos-jr/thumbnail.png` — ditto.
- `staging/mythos-jr/content.zip` — **probably missing.** Resolve via `--project-dir /home/apexaipc/projects/mjr` (it has a `dist/` that should be zipped). Or add `gumroad.yaml` there.

Run:
```bash
(cd /home/apexaipc/projects/swindle && source venv/bin/activate && python swindle.py publish mythos-jr)
```

Expected: Gumroad draft appears at `https://snowdrift840.gumroad.com/l/<slug>`. Eyeball it. If bad, `delete_product` and iterate.

Known cost: 1 Gumroad product slot (free), ~$0.05 Qwen tokens if you regenerate copy. S3 upload to Gumroad is free for the account owner.

### Task 4: Metroplex Gate 4.8 auto-publish wiring

Gate 4.8 in Metroplex currently dispatches to Swindle's `prepare` command. Needs to additionally call `publish` if validator passes. Locate the dispatch code:

```bash
grep -rn "gate.*4\.8\|gate_swindle\|swindle" /home/apexaipc/projects/metroplex --include="*.py"
```

Decision point: auto-publish on a config flag, or unconditional? Matthew's stated goal is unconditional auto-publish. Flag the risk of a bad listing going live; validator is the gate.

### Task 5: Backfill 15 ST Metro staged listings

```bash
ls /home/apexaipc/projects/swindle/staging/
```

Exclude `mythos-jr` (pilot) and `smoketest-agentforge` (smoketest). The other ~15 are ST Metro builds staged pre-Qwen.

For each:
1. Check metadata.json for `spec_path`. If missing: find the source repo + spec file, run `swindle.py prepare` to regenerate with the new prompt + model. This will overwrite the staging dir.
2. If `spec_path` present and new-model-generated: validator should pass; go straight to publish.
3. `swindle.py publish <repo-name>`
4. Sleep 6 seconds between publishes to stay under 10/min rate limit.

If any listing's source repo cannot be found for `--spec-path`, skip and flag. Do not fabricate a spec.

## Risks / watch items

1. **content.zip resolution is not universal.** Swindle's `_resolve_content_file` looks at `gumroad.yaml` → `dist/*.zip` → manual drop. Many ST Metro builds may have no pre-built zip. Decide: auto-zip the repo dir, or skip listings without content. Flag to Matthew.
2. **Pre-Qwen staged listings have fabrications baked in.** The validator's strict mode will fail them (no `spec_path`). Regenerate, do not lift-and-shift.
3. **Gumroad API rate limits: 10 creates/min, 50/9hr.** 15 listings sequential: ~90 seconds with `sleep 6`. Fine.
4. **Fabrication can still slip through.** The validator catches numeric claims, not adjective hype. Qwen is better than Mistral Small but not perfect. If a bad listing ships, `delete_product` + regenerate.
5. **Gumroad account risk.** API creates are sanctioned — unlike scraping. Stay in-API. Do not auto-publish something that would violate Gumroad's content policy (nothing Swindle generates should, but know the escape hatch).
6. **Docs lag.** If a Gumroad endpoint seems to not work, check the Rails controller on `main` before assuming breakage. Mintlify / gumroad.com/api are behind by weeks.

## Files to reference

- `/home/apexaipc/projects/swindle/` — project root
- `/home/apexaipc/projects/swindle/CLAUDE.md` — project conventions
- `/home/apexaipc/.env.shared` — `GUMROAD_ACCESS_TOKEN`, `DEEPINFRA_API_KEY`
- `/home/apexaipc/.claude/projects/-home-apexaipc/memory/swindle-project.md` — memory (will need update after this session)
- `/home/apexaipc/.claude/projects/-home-apexaipc/memory/feedback_swindle_fabrication.md` — fabrication backstory
- GitHub sources of truth:
  - `antiwork/gumroad:app/controllers/api/v2/links_controller.rb`
  - `antiwork/gumroad:config/routes.rb`
  - PRs #4267, #4069, #4311, #4315 for the API additions

## Start command

```bash
(cd /home/apexaipc/projects/swindle && source venv/bin/activate && git log --oneline -5)
# Verify you're at d9a591d or later, then:
# 1. Read CLAUDE.md
# 2. Read validator.py and listing_generator.py to understand current shape
# 3. Write gumroad_api_client.py (Task 1) before touching anything else
# 4. Run tests after every major change: `pytest tests/ -v`
```

Work in small steps. Commit after each task. Do not batch-run the 15-listing backfill until mythos-jr has shipped cleanly end-to-end.
