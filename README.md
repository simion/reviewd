# nea-claudiu

AI code reviewer that watches your BitBucket PRs and posts review comments automatically. Runs locally on your machine.

## Setup (2 minutes)

### 1. Install

```bash
git clone git@github.com:simion/nea-claudiu.git
uv tool install -e ./nea-claudiu
```

### 2. Create global config

```bash
mkdir -p ~/.config/nea-claudiu
```

`~/.config/nea-claudiu/config.yaml`:

```yaml
bitbucket:
  workspace: your-workspace
  auth_token: YOUR_BB_APP_PASSWORD
  poll_interval_seconds: 60

repos:
  - name: my-project
    path: ~/Work/Repos/my-project
```

That's it. You can now review PRs.

### 3. (Optional) Add project-specific guidelines

Drop a `.nea-claudiu.yaml` in your repo root:

```yaml
guidelines: |
  - Python 3.12+, Django 5.x
  - Single quotes for strings
  - No broad except clauses

explore: |
  Read AGENTS.md at the repo root first.

test_commands:
  - uv run ruff check .

skip_title_patterns: ['[no-review]', '[wip]']
```

## Usage

```bash
# Review a specific PR
nea-claudiu review my-project --pr 1234

# Preview without posting
nea-claudiu review my-project --pr 1234 --dry-run

# Watch all repos — reviews open PRs, then polls for new ones
nea-claudiu watch -v

# Check review history
nea-claudiu status my-project
```

## How It Works

1. Fetches PR info from BitBucket API
2. Creates a git worktree for the PR branch
3. Runs `claude --print` (or `gemini`) in the worktree — the AI diffs, explores, and reviews autonomously
4. Parses the structured JSON output
5. Posts inline comments for critical findings + a summary comment for the rest
6. Cleans up the worktree

Re-reviews automatically when new commits are pushed (old bot comments are deleted first).

## Multiple Repos

Just add more entries to the config:

```yaml
repos:
  - name: project-a
    path: ~/repos/project-a
  - name: project-b
    path: ~/repos/project-b
    ai_cli: gemini  # use gemini instead of claude for this repo
```

## AI Backend

Defaults to `claude`. Set `ai_cli: gemini` globally or per-repo to use Gemini CLI instead.

## Requirements

- Python 3.12+
- `claude` or `gemini` CLI installed and authenticated
- BitBucket app password with `pullrequest:write` scope
