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
reviewd init   # set up global config + per-project .reviewd.yaml
```

<details>
<summary><b>GitHub setup</b></summary>

1. Create a [Personal Access Token](https://github.com/settings/tokens) with the **`repo`** scope.
2. Export it: `export GITHUB_TOKEN=ghp_...`
3. Config:

```yaml
github:
  token: ${GITHUB_TOKEN}

repos:
  - name: my-repo
    repo_slug: owner/my-repo
    path: ~/repos/my-repo
    provider: github
```

</details>

<details>
<summary><b>BitBucket setup</b></summary>

1. Create an [App Password](https://bitbucket.org/account/settings/app-passwords/) with **Pull requests: Read** and **Write**.
2. Export it: `export BB_AUTH_TOKEN=ATCTT3x...`
3. Config:

```yaml
bitbucket:
  your-workspace: ${BB_AUTH_TOKEN}

repos:
  - name: my-project
    path: ~/repos/my-project
    provider: bitbucket
    workspace: your-workspace
```

</details>

Both providers can be used in the same config.

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

github:
  token: ${GITHUB_TOKEN}

bitbucket:
  your-workspace: ${BB_AUTH_TOKEN}
  other-workspace: ${OTHER_BB_TOKEN}

cli: claude                    # or "gemini"
# model: claude-sonnet-4-5-20250514

# review_title: "Code Review by Nea' ~~Caisă~~ Claudiu"
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
reviewd init                                  # set up global + project config
reviewd ls                                    # list repos and open PRs
reviewd watch -v                              # daemon mode
reviewd watch -v --dry-run                    # preview, no posting
reviewd watch -v --review-existing            # review not-yet-reviewed open PRs
reviewd pr <repo> <id>                        # one-shot review
reviewd pr <repo> <id> --force                # re-review (bypasses draft/skip)
reviewd status <repo>                         # review history
```

## Architecture

- **Polling, not webhooks** — no tunnel or public endpoint needed
- **Git worktrees** — near-instant isolated checkouts
- **Full AI tool access** — the AI reads files, runs commands, explores code
- **JSON schema** — structured findings, the tool just parses and posts
- **SQLite state** — tracks `(repo, pr_id, commit)` to avoid duplicates
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

## Roadmap

- [ ] Parallel PR review queue — currently PRs are reviewed sequentially, which is fine for most teams since each review takes 1-3 minutes and the poll loop catches up quickly
- [ ] GitLab support

## Disclaimer

> Built entirely with AI-assisted development (Claude Code), with thorough human review and guidance at every step. Because we have production code to ship and no time to hand-craft internal tooling.
>
> Why is that fine? It's a read-only tool that posts PR comments. The worst it can do is post a bad review.

## License

MIT
