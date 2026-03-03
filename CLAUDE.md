# CLAUDE.md — nea-claudiu

## What This Is

Local CLI daemon that polls BitBucket for open PRs, reviews them using Claude/Gemini CLI, and posts structured comments back. The tool owns the review prompt and output schema. Users provide project-specific guidelines via `.nea-claudiu.yaml` in each repo.

## Architecture

```
Poller (BB API) → State Check (SQLite) → Worktree (git) → AI Review (CLI --print) → Parse JSON → Post Comments (BB API)
```

- **Polling**, not webhooks — runs locally, no tunnel needed
- **Git worktrees** for isolation — no interference with working copy
- **AI has full tool access** — reads files, explores code, runs commands in the worktree
- **JSON output** — prompt requests structured JSON as last block, extracted via regex
- **SQLite** for state — tracks `(repo, pr_id, source_commit)` to avoid duplicate reviews

## Project Conventions

- Python 3.12+, no backward compatibility
- Dependencies managed with `uv`
- Google style, single quotes for strings, double quotes for messages
- No broad except clauses
- No unnecessary docstrings or comments
- Tests only when explicitly asked

## Key Files

| File | Purpose |
|------|---------|
| `src/nea_claudiu/cli.py` | Click CLI: `watch`, `review`, `status` commands |
| `src/nea_claudiu/daemon.py` | Poll loop, orchestration, skip logic |
| `src/nea_claudiu/reviewer.py` | Worktree lifecycle + AI CLI invocation + JSON extraction |
| `src/nea_claudiu/prompt.py` | Built-in review prompt template + builder |
| `src/nea_claudiu/commenter.py` | Format findings as markdown, post via provider |
| `src/nea_claudiu/config.py` | YAML + `${ENV_VAR}` loading, per-project merge |
| `src/nea_claudiu/state.py` | SQLite: reviews + posted_comments |
| `src/nea_claudiu/models.py` | Dataclasses: PRInfo, Finding, ReviewResult, configs |
| `src/nea_claudiu/providers/base.py` | Abstract GitProvider ABC |
| `src/nea_claudiu/providers/bitbucket.py` | BitBucket 2.0 API (httpx, pagination, inline comments) |

## Config Locations

- **Global**: `~/.config/nea-claudiu/config.yaml` — BB credentials, repos list, poll interval, AI CLI choice
- **Per-project**: `{repo_root}/.nea-claudiu.yaml` — guidelines, explore instructions, test commands, skip patterns
- **State DB**: `~/.local/share/nea-claudiu/state.db`

## How the Review Works

1. Fetch PR metadata from BB API
2. Create git worktree at `{repo}/.nea-claudiu-worktrees/pr-{id}` (handles deleted branches by falling back to commit hash)
3. Build prompt: PR metadata + user guidelines + explore/validation instructions + JSON schema
4. Run `claude --print -p "<prompt>"` or `gemini -p "<prompt>"` in the worktree directory (strips `CLAUDECODE` env var to allow nested invocation)
5. Extract last ```json``` block from stdout
6. Post inline comments for critical findings + one summary comment with the rest
7. Cleanup worktree

## Bot Comment Marker

`[](nea-claudiu)` — an empty markdown link appended to every bot comment. Used to identify and delete old comments on re-review. BitBucket doesn't support HTML comments in markdown (they render as visible text).

## Known Limitations & Gotchas

- BitBucket markdown doesn't support inline images — they always render as block elements
- BitBucket markdown doesn't support HTML (comments, img tags) — renders as raw text
- Can't `git fetch` by commit hash from BB (not allowed by default) — if source branch is deleted after merge, the commit must already exist locally
- Claude CLI rejects nested sessions — must unset `CLAUDECODE` env var in subprocess
- `gemini` CLI support is implemented but untested — invoked as `gemini -p <prompt>`

## Running

```bash
# Install globally
uv tool install -e ~/r/nea-claudiu

# One-shot review
nea-claudiu review pydpf --pr 1234
nea-claudiu review pydpf --pr 1234 --dry-run

# Daemon mode
nea-claudiu watch -v
nea-claudiu watch -v --dry-run

# Review history
nea-claudiu status pydpf
```

## Future Plans

### GitHub Provider
- Implement `providers/github.py` with the same `GitProvider` interface
- Use `gh` CLI or GitHub API via httpx
- Config: `provider: github` per repo, with `github.token` auth

### Smarter Re-review
- Currently re-reviews entire PR on any new commit
- Could diff only new commits since last review
- Track which findings are still relevant vs resolved

### Review Quality
- Add `test_commands` support with `{changed_files}` substitution (template exists, not yet wired to actual changed file detection from BB API)
- Consider adding a "confidence" field to findings
- Let users configure severity thresholds for inline vs summary comments

### Comment Threading
- BB API supports reply threads — could thread related findings under a parent comment
- Would make long reviews more navigable

### Notifications
- Slack/webhook notification when a review is posted
- Summary of findings count by severity

### Multi-model Support
- Allow configuring different models per repo (e.g., use opus for critical repos, sonnet for others)
- Pass model flags to the AI CLI
