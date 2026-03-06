# CLAUDE.md — reviewd

## What This Is

Local CLI tool that polls GitHub/BitBucket for open PRs, reviews them using Claude/Gemini CLI, and posts structured comments back. Invokes `claude --print` or `gemini -p` as local subprocesses — no API keys, uses existing CLI subscriptions.

## Architecture

```
Poller (GitHub/BB API) → State Check (SQLite) → Worktree (git) → AI Review (CLI) → Parse JSON → Post Comments
```

- **Polling**, not webhooks — runs locally, no tunnel needed
- **Git worktrees** for isolation — no interference with working copy
- **AI has full tool access** — reads files, explores code, runs commands in the worktree
- **JSON output** — prompt requests structured JSON as last block, extracted via regex
- **SQLite** for state — tracks `(repo, pr_id, source_commit)` to avoid duplicate reviews
- **ID-based comment cleanup** — tracks posted comment IDs in SQLite, deletes by ID on re-review

## Project Conventions

- Python 3.12+, no backward compatibility
- Dependencies managed with `uv`
- Google style, single quotes for strings, double quotes for messages
- No broad except clauses
- No unnecessary docstrings or comments
- Tests only when explicitly asked
- Never add Co-Authored-By to commits

## Key Files

| File | Purpose |
|------|---------|
| `src/reviewd/cli.py` | Click CLI: `ls`, `watch`, `pr`, `status` commands |
| `src/reviewd/daemon.py` | Poll loop, boot summary, status line, orchestration, signal handling |
| `src/reviewd/reviewer.py` | Worktree lifecycle + AI CLI invocation (Popen) + JSON extraction |
| `src/reviewd/prompt.py` | Built-in review prompt template + builder |
| `src/reviewd/commenter.py` | Format findings as markdown, post via provider, delete old comments by ID |
| `src/reviewd/config.py` | YAML + `${ENV_VAR}` loading, global + per-project merge, provider factory |
| `src/reviewd/state.py` | SQLite: reviews + posted_comments (with get/delete by repo+PR) |
| `src/reviewd/models.py` | Dataclasses: PRInfo, Finding, ReviewResult, configs, CLI enum |
| `src/reviewd/providers/base.py` | Abstract GitProvider ABC |
| `src/reviewd/providers/bitbucket.py` | BitBucket 2.0 API (httpx, pagination with ID dedup, inline comments) |
| `src/reviewd/providers/github.py` | GitHub REST API v3 (httpx, Link header pagination, review comments) |

## Config

### Global: `~/.config/reviewd/config.yaml`

Provider credentials, repos list, poll interval, AI CLI choice, model, cli_args, global `instructions`, `review_title`, `footer`.

### Per-project: `{repo_root}/.reviewd.yaml`

Project-specific `instructions`, `test_commands`, `inline_comments_for`, `auto_approve`, `critical_task`.

Instructions merge: global + per-project concatenated (global first). Old `guidelines`/`explore` fields still supported.

### Per-repo overrides in global config

`cli`, `model`, `repo_slug` (decouples display name from API slug), per-repo provider credentials.

## How the Review Works

1. Fetch PR metadata from provider API
2. Clean up stale worktrees from previous interrupted runs
3. Create git worktree at `{repo}/.reviewd-worktrees/pr-{id}`
4. Build prompt: PR metadata + merged instructions + validation commands + JSON schema
5. Run `claude --print --model X -p "<prompt>"` or `gemini --approval-mode yolo -e none -p "<prompt>"` via Popen
6. Stream stderr for progress, ticker thread logs elapsed time every 30s
7. Extract last ```json``` block from stdout
8. Delete old bot comments by tracked IDs from SQLite
9. Post inline comments (single-line, with `suggestion` code fence) + summary comment
10. Cleanup worktree

## CLI

```bash
reviewd init                                  # set up global + project config
reviewd ls                                    # list repos + open PRs
reviewd watch -v                              # daemon mode
reviewd watch -v --review-existing            # review not-yet-reviewed open PRs
reviewd watch -v --cli gemini                 # override AI CLI
reviewd pr pydpf 42                           # one-shot review
reviewd pr pydpf 42 --dry-run                 # preview without posting
reviewd pr pydpf 42 --force                   # re-review even if already done
reviewd pr pydpf 42 --cli gemini              # override AI CLI
reviewd status pydpf                          # review history
```

## Prompt Injection Defenses

- Prompt includes a security scope block (before any user-controlled content) that forbids file writes, network access, accessing secrets, and following instructions embedded in code
- Gemini CLI: `-e none` disables all extensions
- Project config (`.reviewd.yaml`) is read from the main repo, not the worktree — PR authors cannot inject instructions via config
- `test_commands` come only from the repo owner's config, not from PR content

## Releasing

Publish directly to PyPI with `uv publish`. No GitHub Releases — `gh` CLI is not available.

Requires `UV_PUBLISH_TOKEN` env var (PyPI API token).

```bash
# 1. Bump version in pyproject.toml
# 2. Commit and push
git add pyproject.toml && git commit -m "Bump version to X.Y.Z" && git push
# 3. Clean old builds, build, and publish
rm -rf dist && uv build && uv publish
```

## Known Limitations

- BitBucket markdown doesn't support HTML comments — bot marker uses empty link `[](reviewd)`
- Can't `git fetch` by commit hash from BB — if source branch is deleted, commit must exist locally
- Claude CLI rejects nested sessions — must unset `CLAUDECODE` env var in subprocess
- Gemini CLI loads global extensions by default — use `-e none` to disable
- Inline suggestions are single-line only (TODO: multi-line support)
- AI may hallucinate line numbers — prompt instructs to double-check but not guaranteed
