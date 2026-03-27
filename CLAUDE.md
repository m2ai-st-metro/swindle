# CLAUDE.md -- Swindle

Storefront listing agent for ST Metro builds. Prepares Gumroad listing packages (copy + images) for human review.

## Setup

```bash
cd /home/apexaipc/projects/swindle
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

All API keys sourced from `~/.env.shared` -- no separate `.env` needed.

## Commands

```bash
source venv/bin/activate
python swindle.py prepare <repo_url> --spec-path <path> --title "Title"
python swindle.py prepare <repo_url> --spec-path <path> --title "Title" --dry-run
python swindle.py list-staged
python swindle.py approve <repo-name>
python swindle.py reject <repo-name> --reason "reason"
python swindle.py status
```

## Testing

```bash
pytest tests/ -v
```

## Architecture

- **CLI**: `swindle.py` (Click) -- all commands
- **Copy gen**: `listing_generator.py` -- DeepInfra Nemotron-3 via HTTP
- **Image gen**: `image_generator.py` -- wraps banana-maker subprocess
- **DB**: `data/swindle.db` -- SQLite tracking (staged/approved/rejected)
- **Staging**: `staging/{repo-name}/` -- draft listing packages

## Design Decisions

1. **HIL only** -- Swindle prepares, Matthew publishes manually to Gumroad
2. **No Gumroad API** -- POST endpoint returns 404; output is file-based staging packages
3. **Subprocess isolation** -- banana-maker called as subprocess, not imported
4. **DeepInfra Nemotron-3** -- all LLM calls use `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B`
5. **Price always $0** -- free developer tools
6. **Visual style** -- clean, modern, dark-theme developer aesthetic (not hand-drawn/pixel art)
