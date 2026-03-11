# CLAUDE.md — reviewd

Local AI code review daemon for GitHub/BitBucket PRs. See README.md for full docs.

## Project Conventions

- Python 3.12+, no backward compatibility
- Dependencies managed with `uv`
- Google style, single quotes for strings, double quotes for messages
- No broad except clauses
- No unnecessary docstrings or comments
- Tests only when explicitly asked
- Never add Co-Authored-By to commits
- Only commit or release when explicitly asked

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
- Codex CLI outputs raw JSON (no markdown fences) — extract_json falls back to raw JSON object parsing
- Inline suggestions are single-line only (TODO: multi-line support)
- AI may hallucinate line numbers — prompt instructs to double-check but not guaranteed
