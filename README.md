# nea-claudiu

**Local AI code reviewer for GitHub and BitBucket pull requests.** Watches your repos for new PRs, reviews them using Claude or Gemini, and posts structured comments — all from your machine.

No CI pipeline. No cloud service. No API keys beyond your existing AI CLI.

## Why nea-claudiu?

### Runs on your actual codebase, not a CI sandbox

Unlike cloud-based review bots, nea-claudiu works directly on your **pre-cloned local repos**. The AI reviewer has full access to your codebase — it can read any file, follow imports, check how functions are called, and understand the full context of a change. No shallow clones, no missing dependencies, no sandboxed environments.

### Git worktrees make it fast

Each review runs in a **git worktree** — a lightweight, isolated checkout that shares the `.git` directory with your main repo. No re-cloning, no downloading. Creating a worktree takes milliseconds, not minutes. Reviews start instantly.

### Run real commands, not just lint

You can configure nea-claudiu to run **arbitrary commands** during review — linters, type checkers, test suites, build scripts. These run in the worktree against the actual PR code on your real machine, with your real dependencies installed. If `ruff check` or `pytest` fails, the AI sees it and includes it in the review.

```yaml
# .nea-claudiu.yaml
test_commands:
  - uv run ruff check .
  - uv run mypy src/
  - uv run pytest tests/ -x -q
```

### The AI does the actual reviewing

nea-claudiu doesn't just run a linter and post the output. It invokes Claude or Gemini with **full tool access** — the AI autonomously diffs the PR, reads changed files, explores related code, runs your validation commands, and produces a structured review with severity-tagged findings and suggested fixes.

### Daemon or one-shot

Run it as a **background daemon** that polls for new PRs across all your repos, or use it for **one-shot reviews** of specific PRs. Dry-run mode lets you preview reviews before posting.

### Multi-repo, multi-AI

Configure multiple repositories with different settings, different AI backends (Claude or Gemini), and different review instructions per project. One daemon watches them all.

### Project-aware instructions

Each repo can include a `.nea-claudiu.yaml` with project-specific conventions, coding standards, and exploration instructions. The AI follows them during review — making reviews consistent with your team's practices, not generic advice.

### Smart re-reviews

When new commits are pushed to a PR, nea-claudiu automatically deletes its old comments and posts a fresh review. No stale feedback cluttering your PRs.

## Quick Start

### 1. Install

```bash
git clone https://github.com/simion/nea-claudiu.git
cd nea-claudiu
uv tool install -e .
```

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/). You also need `claude` or `gemini` CLI installed and authenticated.

### 2. Configure

```bash
mkdir -p ~/.config/nea-claudiu
```

Create `~/.config/nea-claudiu/config.yaml`:

<details>
<summary><b>GitHub setup</b></summary>

1. Create a [Personal Access Token](https://github.com/settings/tokens) with the **`repo`** scope (or a [fine-grained token](https://github.com/settings/tokens?type=beta) with **Pull requests: Read and write**).
2. Export it: `export GITHUB_TOKEN=ghp_...`
3. Config:

```yaml
github:
  token: ${GITHUB_TOKEN}

repos:
  - name: owner/my-repo        # must be owner/repo format
    path: ~/repos/my-repo
    provider: github
```

</details>

<details>
<summary><b>BitBucket setup</b></summary>

1. Create an [App Password](https://bitbucket.org/account/settings/app-passwords/) with **Pull requests: Read** and **Pull requests: Write** permissions.
2. Export it: `export BB_AUTH_TOKEN=ATCTT3x...`
3. Config:

```yaml
bitbucket:
  workspace: your-workspace
  auth_token: ${BB_AUTH_TOKEN}

repos:
  - name: my-project            # repo slug (not owner/repo)
    path: ~/repos/my-project
    provider: bitbucket
```

</details>

You can configure both providers in the same config file to watch GitHub and BitBucket repos together.

### 3. Review a PR

```bash
# One-shot review
nea-claudiu review my-project --pr 42

# Preview without posting
nea-claudiu review my-project --pr 42 --dry-run

# Watch all repos for new PRs
nea-claudiu watch -v
```

## How It Works

```
Poll Provider (GitHub/BitBucket) → Check State (SQLite) → Create Worktree → AI Review (Claude/Gemini CLI) → Parse JSON → Post Comments
```

1. Fetches open PRs from the GitHub or BitBucket API
2. Skips PRs that have already been reviewed at the current commit
3. Creates a git worktree for the PR branch (fast — shares `.git` with your clone)
4. Builds a review prompt with PR metadata, project instructions, and a JSON output schema
5. Runs the AI CLI (`claude --print` or `gemini`) in the worktree with full tool access
6. The AI autonomously diffs, explores, validates, and reviews the code
7. Extracts the structured JSON review from the AI's output
8. Posts inline comments on specific lines + a summary comment on the PR
9. Cleans up the worktree

## Configuration

### Global config (`~/.config/nea-claudiu/config.yaml`)

```yaml
poll_interval_seconds: 60

# Provider credentials (configure one or both)
github:
  token: ${GITHUB_TOKEN}

bitbucket:
  workspace: your-workspace
  auth_token: ${BB_AUTH_TOKEN}

ai_cli: claude  # or "gemini"

# Customize the review header and footer (supports markdown)
# reviewer_name: "Nea' ~Caisă~ Claudiu"
# footer: "Automated review by ..."

# Global review instructions (applied to all repos)
instructions: |
  Be concise and constructive.
  Every issue must include a concrete suggested fix.

repos:
  - name: owner/gh-backend
    path: ~/repos/gh-backend
    provider: github

  - name: bb-frontend
    path: ~/repos/bb-frontend
    provider: bitbucket
    ai_cli: gemini  # override per repo

  - name: other-project
    path: ~/repos/other-project
    provider: bitbucket
    bitbucket:
      workspace: other-workspace      # different BB workspace
      auth_token: ${OTHER_BB_TOKEN}   # different credentials

state_db: ~/.local/share/nea-claudiu/state.db
```

### Per-project config (`.nea-claudiu.yaml` in repo root)

```yaml
# Review instructions specific to this project
# (merged with global instructions — global first, then project)
instructions: |
  Python 3.12+, Django 5.x.
  Single quotes for strings, double quotes for messages.
  Check for missing select_related/prefetch_related.
  No broad except clauses.
  Read CONTRIBUTING.md at the repo root before reviewing.

# Commands to run during review (executed in the worktree)
test_commands:
  - uv run ruff check .
  - uv run mypy src/

# PRs matching these title patterns are skipped
skip_title_patterns:
  - '[no-review]'
  - '[wip]'

# Authors to skip (e.g., bots)
skip_authors: []

# Which severity levels get inline comments on specific lines
# (rest goes into the summary comment)
inline_comments_for:
  - critical
  - suggestion

# Auto-approve PRs with no critical findings
approve_if_no_critical: false
```

## CLI Reference

```bash
# Daemon mode — polls all repos for new PRs
nea-claudiu watch -v
nea-claudiu watch -v --dry-run              # preview mode, no comments posted
nea-claudiu watch -v --review-existing      # also review already-open PRs on startup

# One-shot review
nea-claudiu review my-project --pr 42
nea-claudiu review my-project --branch feature/xyz
nea-claudiu review my-project --pr 42 --dry-run
nea-claudiu review my-project --pr 42 --force   # re-review even if already done

# Review history
nea-claudiu status my-project
nea-claudiu status my-project --limit 50
```

## Requirements

- Python 3.12+
- [`claude`](https://docs.anthropic.com/en/docs/claude-code) or [`gemini`](https://ai.google.dev/gemini-api/docs/gemini-cli) CLI installed and authenticated
- GitHub personal access token with `repo` scope, or BitBucket app password with `pullrequest:write` scope
- Git
- [`uv`](https://docs.astral.sh/uv/) (for installation)

## Architecture

- **Polling, not webhooks** — runs locally, no tunnel or public endpoint needed
- **Git worktrees** — isolated checkouts that share `.git`, creating them is near-instant
- **AI has full tool access** — reads files, runs commands, explores the codebase in the worktree
- **JSON output schema** — the AI outputs structured findings, the tool just parses and posts
- **SQLite state** — tracks reviews by `(repo, pr_id, source_commit)` to avoid duplicates
- **Provider abstraction** — GitHub and BitBucket supported, extensible to others

## Roadmap

- **Incremental re-reviews** — diff only new commits since last review
- **Slack/webhook notifications** — get notified when a review is posted
- **Multi-model support** — configure different AI models per repo

## Disclaimer

> This project is **100% vibe-coded** — written entirely through AI-assisted development. Every line was generated, reviewed, and iterated with Claude Code.
>
> **Why is that fine?** nea-claudiu is a read-only tool that posts comments on pull requests. It doesn't modify code, deploy anything, or touch your database. The worst it can do is post a bad review — and you're already reviewing those anyway.

## License

MIT
