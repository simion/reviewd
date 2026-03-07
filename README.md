# reviewd

[![PyPI](https://img.shields.io/pypi/v/reviewd)](https://pypi.org/project/reviewd/)
[![Python 3.12+](https://img.shields.io/pypi/pyversions/reviewd)](https://pypi.org/project/reviewd/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/simion/reviewd/actions/workflows/ci.yml/badge.svg)](https://github.com/simion/reviewd/actions/workflows/ci.yml)

**The review daemon** — local AI code reviewer for GitHub and BitBucket pull requests, powered by Claude Code / Gemini CLI subscriptions.

- Watches your repos for new PRs, reviews them using Claude or Gemini CLI, and posts structured comments
- All from your machine — no CI pipeline, no cloud service, no new accounts
- Secure by default — can only access repos you already have locally, as secure as your machine

> If you already have `claude` or `gemini` CLI and local git clones, you're 5 minutes away from automated code reviews.

## Features

- **Reuses what you already have** — your local git repos, your Claude/Gemini CLI subscription, your existing credentials. Nothing new to install or pay for.
- **Full codebase context** — reviews run on your actual local repos, not shallow CI clones. The AI can read any file, follow imports, and understand the full picture.
- **Fast via git worktrees** — isolated checkouts that share `.git`. No re-cloning. Reviews start in milliseconds.
- **Parallel reviews** — concurrent PR processing with configurable concurrency. Per-repo git locks, thread-safe SQLite, graceful shutdown.
- **Runs real commands** — configure linters, type checkers, and test suites to run during review. Failures are included in the AI's analysis.
- **Structured output** — severity-tagged findings with inline comments on specific lines and a summary comment.
- **Daemon or one-shot** — background polling across all repos, or single PR reviews on demand. Dry-run mode to preview.
- **Multi-repo, multi-AI** — different repos can use different AI backends, models, and review instructions.
- **Smart re-reviews** — new commits on a PR trigger a fresh review; old comments are deleted automatically.
- **Draft-aware** — skips draft PRs by default. Add `[review]`, `[claudiu]`, `[ask]`, or `[bot review]` to the title to request a review anyway.
- **Auto-approve** — automatically approves PRs that pass configurable gates (diff size, severity, finding count) and AI-evaluated rules. Shows approval rationale in the summary comment.
- **Critical tasks** — optionally creates a BitBucket PR task on critical findings to block merge.
- **Spam protection** — configurable diff size thresholds, cooldowns, and title/author skip patterns.
- **Auto-sync config** — automatically pulls `.reviewd.yaml` from remote when the working copy is clean.
- **VPS / headless ready** — runs as a systemd service, no TTY needed. Non-interactive git, graceful shutdown, PID lock, XDG paths, env var substitution for secrets.

## Quick Start

### 1. Install

```bash
pip install reviewd
```

Or with [`uv`](https://docs.astral.sh/uv/):

```bash
uv tool install reviewd
```

Requires Python 3.12+. You also need `claude` or `gemini` CLI installed and authenticated.

### 2. Configure

```bash
reviewd init   # interactive wizard — detects repos, guides token creation, writes config
```

The wizard scans your repos, detects GitHub/BitBucket remotes, validates credentials, and writes both global and per-project configs. Prefer YAML? Choose "Sample config file" to get an annotated template instead.

<details>
<summary><b>GitHub setup</b></summary>

1. Create a [Fine-grained Personal Access Token](https://github.com/settings/personal-access-tokens/new) with **Pull requests: Read & Write**.
2. Config:

```yaml
github:
  token: ghp_YOUR_TOKEN

repos:
  - name: my-repo
    repo_slug: owner/my-repo
    path: ~/repos/my-repo
    provider: github
```

</details>

<details>
<summary><b>BitBucket setup</b></summary>

1. Create an [API token with scopes](https://id.atlassian.com/manage-profile/security/api-tokens) — select app: Bitbucket, scopes: `read:pullrequest:bitbucket`, `write:pullrequest:bitbucket`, `read:repository:bitbucket`.
2. Config (format is `email:token`):

```yaml
bitbucket:
  your-workspace: you@example.com:ATATT3x...

repos:
  - name: my-project
    path: ~/repos/my-project
    provider: bitbucket
    workspace: your-workspace
    repo_slug: repo-slug
```

</details>

Both providers can be used in the same config. Tokens support `${ENV_VAR}` substitution.

### 3. Review

```bash
reviewd pr my-project 42           # one-shot
reviewd pr my-project 42 --dry-run # preview
reviewd watch -v                   # daemon mode
```

## How It Works

```
Poll API → Check State (SQLite) → Fetch & Worktree → AI Review (Claude/Gemini) → Parse JSON → Post Comments → Cleanup
```

1. Fetches open PRs from GitHub/BitBucket
2. Skips already-reviewed commits, drafts, cooldowns, and small diffs
3. Creates a git worktree, runs configured test commands
4. Invokes the AI CLI with a structured prompt and JSON output schema
5. Posts inline comments + summary comment, tracks state in SQLite

## Configuration

### Global (`~/.config/reviewd/config.yaml`)

```yaml
poll_interval_seconds: 60
max_concurrent_reviews: 4

github:
  token: ${GITHUB_TOKEN}

bitbucket:
  your-workspace: you@example.com:${BB_API_TOKEN}
  other-workspace: other@example.com:${OTHER_BB_TOKEN}

cli: claude                    # or "gemini"
# model: claude-sonnet-4-5-20250514

# review_title: "reviewd ({cli})"
# footer: "Automated review by ..."
# skip_title_patterns: ['[no-review]', '[wip]', '[no-claudiu]']
# skip_authors: []

instructions: |
  Be concise and constructive.
  Every issue must include a concrete suggested fix.

repos:
  - name: gh-backend
    repo_slug: owner/gh-backend
    path: ~/repos/gh-backend
    provider: github

  - name: bb-frontend
    path: ~/repos/bb-frontend
    provider: bitbucket
    workspace: your-workspace
    cli: gemini
    model: gemini-2.5-pro
```

### Per-project (`.reviewd.yaml` in repo root)

```yaml
instructions: |
  Python 3.12+, Django 5.x.
  Check for missing select_related/prefetch_related.

test_commands:
  - uv run ruff check .
  - uv run pytest tests/ -x -q

skip_severities: [nitpick]       # options: critical, suggestion, nitpick, good
inline_comments_for: [critical]  # rest goes in summary
# max_inline_comments: 5         # skip all inline if exceeded
# min_diff_lines: 0              # initial review threshold (0 = disabled)
# min_diff_lines_update: 5       # re-review threshold for pushed commits
# review_cooldown_minutes: 30
# critical_task: true            # create PR task on critical findings (BitBucket)
```

### Auto-Approve

reviewd can automatically approve PRs that pass all configured gates. The AI is asked to evaluate the PR against your rules and provide an approval reason, which is shown in the summary comment.

```yaml
# in .reviewd.yaml
auto_approve:
  enabled: true
  max_diff_lines: 50        # block approval if diff exceeds this
  max_severity: nitpick     # highest allowed severity (good < nitpick < suggestion < critical)
  max_findings: 3           # block if more findings than this (excludes "good" findings)
  rules: |                  # custom rules sent to the AI for the approval decision
    Only approve safe, simple changes:
    - Minor refactors, renames, typo fixes
    - Small bug fixes with obvious correctness
    - Config/settings tweaks, dependency bumps
    Never approve changes with migrations or complex business logic.
```

**How it works:**

1. The AI reviews the PR normally, producing findings
2. The AI evaluates your `rules` and sets `approve: true/false` with a reason
3. reviewd checks the gates: `max_diff_lines`, `max_severity`, `max_findings`
4. If all gates pass **and** the AI approved, the PR is approved via the provider API
5. The approval reason is included in the summary comment

All gates must pass — if any one blocks, the PR is not approved. The `rules` field is sent verbatim to the AI as part of the review prompt, so write it as instructions.

`auto_approve` can also be set in the global config and will be inherited by all repos. Per-project settings override global ones.

## CLI Reference

```bash
reviewd init                                  # interactive setup wizard
reviewd init --sample                         # write sample config (non-interactive)
reviewd ls                                    # list repos and open PRs
reviewd watch -v                              # daemon mode
reviewd watch -v --dry-run                    # preview, no posting
reviewd watch -v --review-existing            # review not-yet-reviewed open PRs
reviewd watch --concurrency 8                 # override max concurrent reviews
reviewd pr <repo> <id>                        # one-shot review
reviewd pr <repo> <id> --force                # re-review (bypasses draft/skip)
reviewd status <repo>                         # review history
```

## Architecture

- **Polling, not webhooks** — no tunnel or public endpoint needed
- **Git worktrees** — near-instant isolated checkouts
- **Full AI tool access** — the AI reads files, runs commands, explores code
- **JSON schema** — structured findings, the tool just parses and posts
- **SQLite state** — WAL mode, thread-safe, tracks `(repo, pr_id, commit)` to avoid duplicates
- **Provider abstraction** — GitHub and BitBucket, extensible

## Security

> reviewd gives the AI CLI full tool access in git worktrees on your machine. Only watch repos where you trust the contributors.

**Claude CLI (recommended)** is the more secure option. It runs with:
- `--print` mode — read-only, no tool use, no code execution. The AI only sees the prompt and returns text.
- `--disallowedTools Write,Edit` — explicitly blocks file modification tools as an extra layer
- `--mcp-config '{"mcpServers":{}}' --strict-mcp-config` — disables all MCP servers, preventing external tool access
- `CLAUDECODE` env var is unset — prevents nested Claude Code sessions

**Gemini CLI** runs with `--approval-mode yolo` because it has no equivalent print-only mode. This means Gemini can execute commands and modify files in the worktree during review. Mitigated by:
- `-e none` — disables all extensions (no web access, no file tools beyond built-in)
- But it's inherently less sandboxed than Claude's `--print`

**General mitigations (both CLIs):**
- Reviews run in isolated git worktrees, not your working copy
- The prompt includes a security scope block forbidding file writes, network access, and secret access
- Per-project config (`.reviewd.yaml`) is read from the main repo, not the worktree — PR authors can't inject instructions
- `test_commands` come only from the repo owner's config, not from PR content

## Headless / VPS Deployment

reviewd runs fully headless — no TTY, no interactive prompts in the daemon path. Deploy it on a VPS alongside your AI CLI and forget about it.

### Quick setup

```bash
# 1. Install
pip install reviewd

# 2. Write sample config (non-interactive, no wizard)
reviewd init --sample

# 3. Edit config — add tokens, repos, paths
vim ~/.config/reviewd/config.yaml

# 4. Clone repos with deploy keys
git clone git@github.com:org/repo.git ~/repos/repo

# 5. Run as daemon
reviewd watch -v
```

### What makes it VPS-ready

- **`reviewd init --sample`** — writes an annotated config template without prompts. No TTY required.
- **`GIT_TERMINAL_PROMPT=0`** on all git operations — if SSH keys or credentials aren't set up, git fails fast instead of hanging waiting for a password.
- **`-v` flag** — disables the terminal status line (carriage returns, ANSI escape codes). Output becomes clean newline-separated log lines, suitable for journald or any log collector.
- **Signal handling** — SIGTERM/SIGINT trigger graceful shutdown: in-progress reviews finish, worktrees are cleaned up, state DB is closed. Works with systemd `Type=simple`.
- **PID lock** — prevents duplicate instances (`~/.local/share/reviewd/reviewd.pid`).
- **XDG paths** — config, state, and cache directories respect `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`. Deploy to any user/path.
- **`${ENV_VAR}` substitution** in config — keep tokens in environment variables or secrets managers instead of plaintext YAML.
- **Per-project config auto-pulls** — `.reviewd.yaml` is re-read on every review cycle and auto-pulled from remote if the working copy is clean. Push config changes and they take effect without restarting.
- **Claude `--print` works headless** — no TTY needed, reads prompt from stdin, writes to stdout/stderr.
- **Gemini `--approval-mode yolo -e none`** — no approval prompts, no extensions, fully non-interactive.

### systemd service example

```ini
[Unit]
Description=reviewd — AI code review daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=reviewd
ExecStart=/usr/local/bin/reviewd watch -v
Restart=on-failure
RestartSec=30
Environment=XDG_CONFIG_HOME=/home/reviewd/.config
Environment=XDG_DATA_HOME=/home/reviewd/.local/share

[Install]
WantedBy=multi-user.target
```

### Deploy key setup

```bash
# Generate a deploy key per repo
ssh-keygen -t ed25519 -f ~/.ssh/repo_deploy_key -N ""

# Add public key to GitHub/BitBucket as a deploy key (read-only is fine)
# Configure SSH to use it
cat >> ~/.ssh/config <<EOF
Host github.com
  IdentityFile ~/.ssh/repo_deploy_key
  IdentitiesOnly yes
EOF

# Test non-interactive access
GIT_TERMINAL_PROMPT=0 git fetch origin
```

### Global config changes require restart

The global config (`~/.config/reviewd/config.yaml`) is loaded once at startup. If you change poll interval, add repos, or rotate tokens, restart the service. Per-project `.reviewd.yaml` files are hot-reloaded on every review cycle.

## Roadmap

- [x] Parallel PR review queue
- [ ] GitLab support

## Disclaimer

> Built entirely with AI-assisted development (Claude Code), with thorough human review and guidance at every step. Because we have production code to ship and no time to hand-craft internal tooling.
>
> Why is that fine? It's a read-only tool that posts PR comments. The worst it can do is post a bad review.

## License

MIT
