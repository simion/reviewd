# reviewd

[![PyPI](https://img.shields.io/pypi/v/reviewd)](https://pypi.org/project/reviewd/)
[![Python 3.12+](https://img.shields.io/pypi/pyversions/reviewd)](https://pypi.org/project/reviewd/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/simion/reviewd/actions/workflows/ci.yml/badge.svg)](https://github.com/simion/reviewd/actions/workflows/ci.yml)

**Your local code review assistant** — review GitHub and BitBucket pull requests from your terminal, powered by Claude Code / Gemini / Codex CLI.

- Review your team's PRs using Claude, Gemini, or Codex CLI — right from your workstation
- All local — no CI pipeline, no cloud service, no new accounts
- Secure by default — only accesses repos you already have cloned locally

> If you already have `claude`, `gemini`, or `codex` CLI and local git clones, you're 5 minutes away from AI-assisted code reviews.

## Features

- **Reuses what you already have** — your local git repos, your Claude/Gemini/Codex CLI subscription, your existing credentials. Nothing new to install or pay for.
- **Full codebase context** — reviews run on your actual local repos, not shallow CI clones. The AI can read any file, follow imports, and understand the full picture.
- **Fast via git worktrees** — isolated checkouts that share `.git`. No re-cloning. Reviews start in milliseconds.
- **Parallel reviews** — concurrent PR processing with configurable concurrency. Per-repo git locks, thread-safe SQLite.
- **Runs real commands** — configure linters, type checkers, and test suites to run during review. Failures are included in the AI's analysis.
- **Structured output** — severity-tagged findings with inline comments on specific lines and a summary comment.
- **Batch mode or one-shot** — review a single PR on demand, or run a continuous local review loop across all your repos in an open terminal.
- **Multi-repo, multi-AI** — different repos can use different AI backends, models, and review instructions.
- **Smart re-reviews** — new commits on a PR trigger a fresh review; old comments are cleaned up automatically.
- **Draft-aware** — in batch mode, drafts are skipped unless the title contains `[review]`, `[claudiu]`, `[ask]`, or `[bot review]`. The `pr` command always reviews regardless of draft status.
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

Requires Python 3.12+. You also need `claude`, `gemini`, or `codex` CLI installed and authenticated.

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

Two token types — pick one:

**Option A: Workspace Access Token** (recommended) — workspace-level access:
1. Go to Settings gear -> **Workspace settings** -> Security -> **[Access tokens](https://bitbucket.org/{workspace}/workspace/settings/access-tokens)**
2. Create access token with Permissions: Pull requests -> Read + Write

```yaml
bitbucket:
  your-workspace: ATCTT3x...    # workspace token (Bearer auth)
```

**Option B: User API Token** — acts as your personal account:
1. Create an [API token](https://id.atlassian.com/manage-profile/security/api-tokens) -> app: Bitbucket -> all scopes
2. Grant the user access to repos in Workspace settings -> User directory

```yaml
bitbucket:
  your-workspace: you@example.com:ATATT3x...   # user token (Basic auth)
```

Then add your repos:

```yaml
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
reviewd pr my-project 42           # one-shot review
reviewd pr my-project 42 --dry-run # preview without posting
reviewd watch -v                   # continuous review loop
```

## How It Works

```
Check API -> State Check (SQLite) -> Fetch & Worktree -> AI Review (Claude/Gemini/Codex) -> Parse JSON -> Post Comments -> Cleanup
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

cli: claude                    # or "gemini" or "codex"
# model: claude-sonnet-4-5-20250514

# review_title: "review'd by {cli}"
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
    cli: gemini                   # or "codex"
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
reviewd init --sample                         # write sample config (skip wizard)
reviewd ls                                    # list repos and open PRs
reviewd watch -v                              # continuous review loop (verbose)
reviewd watch -v --dry-run                    # preview, no posting
reviewd watch -v --review-existing            # review not-yet-reviewed open PRs
reviewd watch --concurrency 8                 # override max concurrent reviews
reviewd pr <repo> <id>                        # one-shot review (reviews drafts too)
reviewd pr <repo> <id> --force                # re-review (bypasses already-reviewed/cooldown/skip)
reviewd status <repo>                         # review history
```

## Architecture

- **Polling-based** — checks provider APIs on a configurable interval, no webhooks or public endpoints needed
- **Git worktrees** — near-instant isolated checkouts
- **Full AI tool access** — the AI reads files, runs commands, explores code in the worktree
- **JSON schema** — structured findings, the tool just parses and posts
- **SQLite state** — WAL mode, thread-safe, tracks `(repo, pr_id, commit)` to avoid duplicates
- **Provider abstraction** — GitHub and BitBucket, extensible

## Security

> reviewd gives the AI CLI full tool access in git worktrees on your machine. Only review repos where you trust the contributors.

**Claude CLI** runs with the strongest sandboxing:
- `--print` mode — standard CLI output mode with full read and analysis capabilities (file reading, commands, grep, glob). The AI explores the worktree and returns structured text.
- `--disallowedTools Write,Edit` — blocks file modification tools while keeping read/execute tools available. This is tool-level enforcement that the AI cannot bypass.
- `--mcp-config '{"mcpServers":{}}' --strict-mcp-config` — disables all MCP servers, preventing external tool access
- `CLAUDECODE` env var is unset — prevents nested Claude Code sessions

**Gemini CLI** runs with `--approval-mode yolo` (no confirmation prompts). This means Gemini can execute commands and modify files in the worktree during review. Mitigated by:
- `-e none` — disables all extensions (no web access, no additional tools)
- Inherently less sandboxed than Claude since there's no tool-level write blocking

**Codex CLI** runs with `codex exec` (agent mode):
- `--sandbox workspace-write` — OS-level sandbox restricting operations to the working directory
- No equivalent of Claude's `--disallowedTools` — the sandbox allows file writes within the workdir. Since reviews run in disposable worktrees, this is harmless (the worktree is deleted after each review).

**General mitigations (all CLIs):**
- Reviews run in isolated git worktrees, not your working copy — any file modifications are discarded
- The prompt includes a security scope block (placed before any user-controlled content) forbidding file writes, network access, and secret access
- Per-project config (`.reviewd.yaml`) is read from the main repo, not the worktree — PR authors can't inject instructions via config changes
- `test_commands` come only from the repo owner's config, not from PR content
- Prompt injection attempts in code under review are flagged as security findings

## Roadmap

- [x] Parallel PR review queue
- [ ] GitLab support

## Disclaimer

> Built entirely with AI-assisted development (Claude Code), with thorough human review and guidance at every step. Because we have production code to ship and no time to hand-craft internal tooling.
>
> Why is that fine? It's a read-only tool that posts PR comments. The worst it can do is post a bad review.

## License

MIT
