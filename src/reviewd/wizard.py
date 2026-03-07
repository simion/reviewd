from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import click
import httpx
import questionary
from questionary import Style

REMOTE_PATTERNS = [
    (r'github\.com[:/](?P<slug>[^/\s]+/[^/\s]+)', 'github'),
    (r'bitbucket\.org[:/](?P<slug>[^/\s]+/[^/\s]+)', 'bitbucket'),
]

STYLE = Style([
    ('qmark', 'fg:cyan bold'),
    ('question', 'bold'),
    ('answer', 'fg:cyan'),
    ('pointer', 'fg:cyan bold'),
    ('highlighted', 'fg:white bold'),
    ('selected', 'fg:ansidarkgreen noinherit'),
    ('instruction', 'fg:ansigray'),
])


def _section(title: str):
    click.echo()
    click.echo(click.style(f'── {title} ', fg='cyan') + click.style('─' * (46 - len(title)), fg='cyan'))
    click.echo()


def _success(msg: str):
    click.echo(click.style(f'  ✓ {msg}', fg='green'))


def _error(msg: str):
    click.echo(click.style(f'  ✗ {msg}', fg='red'))


def _info(msg: str):
    click.echo(click.style(f'  {msg}', dim=True))


def _detect_remote(repo_path: str) -> dict | None:
    result = subprocess.run(
        ['git', 'remote', 'get-url', 'origin'],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    url = result.stdout.strip().removesuffix('.git')
    for pattern, provider in REMOTE_PATTERNS:
        match = re.search(pattern, url)
        if match:
            slug = match.group('slug')
            parts = slug.split('/')
            info = {
                'provider': provider,
                'name': Path(repo_path).resolve().name,
                'path': str(Path(repo_path).resolve()),
                'remote_url': url,
            }
            if provider == 'github':
                info['slug'] = slug
            elif provider == 'bitbucket':
                info['workspace'] = parts[0]
                info['slug'] = parts[-1]
            return info
    return None


def _git_repo_root(path: str) -> str | None:
    result = subprocess.run(
        ['git', 'rev-parse', '--show-toplevel'],
        cwd=path,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def _scan_repos(directory: str) -> list[dict]:
    repos = []
    base = Path(directory).expanduser().resolve()
    if not base.is_dir():
        return repos

    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name.startswith('.'):
            continue
        if not (entry / '.git').exists():
            continue
        info = _detect_remote(str(entry))
        if info:
            repos.append(info)
    return repos


def _short_remote(repo: dict) -> str:
    url = repo.get('remote_url', '')
    if not url:
        return ''
    for prefix in ('https://', 'http://', 'ssh://', 'git@'):
        url = url.removeprefix(prefix)
    if ':' in url:
        url = url.replace(':', '/', 1)
    return url


def _validate_github_token(token: str) -> str | None:
    try:
        resp = httpx.get(
            'https://api.github.com/user',
            headers={'Authorization': f'Bearer {token}'},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get('login')
    except httpx.HTTPError:
        pass
    return None


def _validate_bitbucket_token(email: str, token: str) -> str | None:
    try:
        resp = httpx.get(
            'https://api.bitbucket.org/2.0/user',
            auth=(email, token),
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get('display_name')
        # 403 means credentials are valid but token lacks read:me scope — still OK
        if resp.status_code == 403:
            return email
    except httpx.HTTPError:
        pass
    return None


def _prompt_github_token(repo_names: list[str]) -> str:
    repos_str = ', '.join(click.style(n, bold=True) for n in repo_names)
    click.echo(f'  GitHub token needed for: {repos_str}')
    click.echo()
    gh_url = 'https://github.com/settings/personal-access-tokens/new'
    click.echo('  Create a Fine-grained Personal Access Token:')
    click.echo()
    click.echo(f'    1. Go to {click.style(gh_url, fg="cyan", underline=True)}')
    click.echo('    2. Set a token name (e.g. "reviewd") and expiration')
    click.echo('    3. Repository access → "All repositories" or select specific ones')
    click.echo('    4. Permissions → Repository permissions → Pull requests → Read & Write')
    click.echo('    5. Click "Generate token" and paste below')
    click.echo()

    while True:
        token = questionary.password('GitHub token:', style=STYLE).unsafe_ask()
        if not token:
            continue
        click.echo('  Validating...')
        username = _validate_github_token(token)
        if username:
            _success(f'Authenticated as {username}')
            return token
        _error('Invalid token, try again')


def _prompt_bitbucket_tokens(bb_repos: list[dict]) -> dict[str, str]:
    """Prompt for BitBucket credentials. Returns {workspace: 'email:token'}."""
    workspaces = list({r['workspace'] for r in bb_repos if r.get('workspace')})
    repos_str = ', '.join(click.style(r['name'], bold=True) for r in bb_repos)
    ws_str = ', '.join(workspaces)
    click.echo(f'  BitBucket repos: {repos_str} (workspace: {ws_str})')
    click.echo()

    bb_url = 'https://id.atlassian.com/manage-profile/security/api-tokens'
    click.echo('  Create a BitBucket API token:')
    click.echo()
    click.echo(f'    1. Go to {click.style(bb_url, fg="cyan", underline=True)}')
    click.echo('    2. Click "Create API token with scopes"')
    click.echo('    3. Set a name (e.g. "reviewd") and expiration')
    click.echo('    4. Select app: Bitbucket')
    click.echo('    5. Select scopes:')
    click.echo('       • read:pullrequest:bitbucket')
    click.echo('       • write:pullrequest:bitbucket')
    click.echo('       • read:repository:bitbucket')
    click.echo('    6. Create token and paste below')
    click.echo()

    email = questionary.text('Atlassian account email:', style=STYLE).unsafe_ask()

    while True:
        token = questionary.password('BitBucket API token:', style=STYLE).unsafe_ask()
        if not token:
            continue
        click.echo('  Validating...')
        display_name = _validate_bitbucket_token(email, token)
        if display_name:
            _success(f'Authenticated as {display_name}')
            cred = f'{email}:{token}'
            return {ws: cred for ws in workspaces}
        _error('Invalid token, try again')


def _build_global_config_yaml(
    repos: list[dict],
    github_token: str | None,
    bitbucket_creds: dict[str, str],
    cli: str,
) -> str:
    lines = []

    if github_token:
        lines.append('github:')
        lines.append(f'  token: {github_token}')
        lines.append('')

    if bitbucket_creds:
        lines.append('bitbucket:')
        for ws, cred in sorted(bitbucket_creds.items()):
            lines.append(f'  {ws}: {cred}')
        lines.append('')

    lines.append(f'cli: {cli}')
    lines.append('# model: claude-sonnet-4-5-20250514')
    lines.append('# cli_args: []')
    lines.append('')
    lines.append('# poll_interval_seconds: 60')
    lines.append('# max_concurrent_reviews: 4')
    lines.append('')
    lines.append('# Global review instructions (merged with per-project instructions)')
    lines.append('# instructions: |')
    lines.append('#   Be concise and constructive.')
    lines.append('#   Every issue must include a concrete suggested fix.')
    lines.append('')
    lines.append('# skip_title_patterns: ["[no-review]", "[wip]"]')
    lines.append('# skip_authors: []')
    lines.append('')
    lines.append('# auto_approve:')
    lines.append('#   enabled: false')
    lines.append('#   max_diff_lines: 50')
    lines.append('#   max_severity: nitpick')
    lines.append('#   max_findings: 3')
    lines.append('')

    lines.append('repos:')
    for repo in repos:
        lines.append(f'  - name: {repo["name"]}')
        lines.append(f'    path: {repo["path"]}')
        lines.append(f'    provider: {repo["provider"]}')
        if repo['provider'] == 'github':
            lines.append(f'    repo_slug: {repo["slug"]}')
        elif repo['provider'] == 'bitbucket':
            if repo.get('workspace'):
                lines.append(f'    workspace: {repo["workspace"]}')
            if repo.get('slug'):
                lines.append(f'    repo_slug: {repo["slug"]}')
        lines.append('')

    return '\n'.join(lines)


PROJECT_CONFIG_TEMPLATE = """\
# Project-specific review instructions (merged with global instructions)
# instructions: |
#   Review ONLY code in the diff — never comment on unchanged or pre-existing code.
#   Be constructive and specific — every issue must include a concrete suggested fix.
#   Never suggest adding docstrings, inline comments, or tests unless clearly needed.
#   If the diff is very large, prioritize critical items; mention nitpicks were deprioritized.

# Commands to run in the PR worktree before reviewing
# test_commands:
#   - uv run ruff check .
#   - uv run pytest tests/ -x -q

# Which severities get inline comments (vs summary only)
# Options: critical, suggestion, nitpick, good
inline_comments_for: [critical]

# Skip posting findings of these severities entirely
# Options: critical, suggestion, nitpick, good
# skip_severities: [nitpick]
"""


SAMPLE_CONFIG = """\
# reviewd global configuration
# Docs: https://github.com/simion/reviewd

# ─── GitHub ───────────────────────────────────────────────────────────
# Create a Fine-grained Personal Access Token:
#   1. Go to https://github.com/settings/personal-access-tokens/new
#   2. Set a token name (e.g. "reviewd") and expiration
#   3. Repository access → "All repositories" or select specific ones
#   4. Permissions → Repository permissions → Pull requests → Read & Write
#   5. Generate token
github:
  token: ghp_YOUR_TOKEN_HERE

# ─── BitBucket ────────────────────────────────────────────────────────
# Create an API token with scopes:
#   1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
#   2. Click "Create API token with scopes"
#   3. Set a name (e.g. "reviewd") and expiration
#   4. Select app: Bitbucket
#   5. Select scopes:
#      • read:pullrequest:bitbucket
#      • write:pullrequest:bitbucket
#      • read:repository:bitbucket
#   6. Create token
# Format: workspace_name: email:token
# bitbucket:
#   my-workspace: me@example.com:ATATT3x...

# ─── AI CLI ───────────────────────────────────────────────────────────
cli: claude                           # claude or gemini
# model: claude-sonnet-4-5-20250514
# cli_args: []

# ─── Polling ──────────────────────────────────────────────────────────
# poll_interval_seconds: 60
# max_concurrent_reviews: 4

# ─── Review Settings ─────────────────────────────────────────────────
# Global review instructions (merged with per-project instructions)
# instructions: |
#   Be concise and constructive.
#   Every issue must include a concrete suggested fix.

# skip_title_patterns: ["[no-review]", "[wip]"]
# skip_authors: []

# auto_approve:
#   enabled: false
#   max_diff_lines: 50
#   max_severity: nitpick
#   max_findings: 3

# ─── Repos ────────────────────────────────────────────────────────────
repos:
  # GitHub example:
  # - name: my-project
  #   path: /path/to/my-project
  #   provider: github
  #   repo_slug: owner/repo-name      # owner/repo from GitHub URL

  # BitBucket example:
  # - name: my-bb-project
  #   path: /path/to/my-bb-project
  #   provider: bitbucket
  #   workspace: my-workspace          # workspace from BitBucket URL
  #   repo_slug: repo-name             # repo slug from BitBucket URL
"""


def run_wizard():
    try:
        _run_wizard_inner()
    except KeyboardInterrupt:
        click.echo()
        click.echo(click.style('Setup cancelled.', fg='yellow'))


def _run_wizard_inner():
    config_dir = Path('~/.config/reviewd').expanduser()
    config_path = config_dir / 'config.yaml'

    setup_mode = questionary.select(
        'How would you like to set up reviewd?',
        choices=[
            questionary.Choice('Interactive wizard (guided setup)', value='interactive'),
            questionary.Choice('Sample config file (edit YAML manually)', value='sample'),
        ],
        style=STYLE,
        instruction='(↑↓ move, enter select)',
    ).unsafe_ask()

    if setup_mode == 'sample':
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path.write_text(SAMPLE_CONFIG)
        click.echo()
        _success(f'Created {config_path}')
        click.echo()
        click.echo('  Edit the config file to add your tokens and repos:')
        click.echo(f'    {click.style(str(config_path), fg="cyan")}')
        click.echo()
        click.echo('  The file includes instructions for creating GitHub and BitBucket tokens.')
        click.echo('  Uncomment and fill in the sections you need.')
        click.echo()
        return

    selected_repos: list[dict] = []

    _section('Repository Setup')

    # 1. Detect current repo
    cwd_root = _git_repo_root('.')
    if cwd_root:
        info = _detect_remote(cwd_root)
        if info:
            remote = _short_remote(info)
            click.echo(f'  Detected repo: {click.style(info["name"], bold=True)}  ({remote})')
            add_current = questionary.confirm(
                'Watch this repo?',
                default=True,
                style=STYLE,
            ).unsafe_ask()
            if add_current:
                selected_repos.append(info)
                _success(f'Added {info["name"]}')

    # 2. Scan for more repos
    if selected_repos:
        add_more = questionary.confirm('Add more repos?', default=True, style=STYLE).unsafe_ask()
    else:
        add_more = True

    if add_more:
        scan_dir = questionary.path(
            'Where do you keep your repos?',
            only_directories=True,
            style=STYLE,
        ).unsafe_ask()

        if scan_dir:
            scan_dir = str(Path(scan_dir).expanduser())
            _info(f'Scanning {scan_dir}...')

            found = _scan_repos(scan_dir)
            already_paths = {r['path'] for r in selected_repos}
            available = [r for r in found if r['path'] not in already_paths]

            if not available:
                _info('No repos with recognized remotes (GitHub/BitBucket) found.')
            else:
                # Build choices for checkbox
                choices = []
                for repo in available:
                    remote = _short_remote(repo)
                    label = f'{repo["name"]:<20s} {remote}'
                    choices.append(questionary.Choice(label, value=repo))

                picked = questionary.checkbox(
                    'Select repos to watch:',
                    choices=choices,
                    style=STYLE,
                    instruction='(↑↓ move, space toggle, enter to continue)',
                ).unsafe_ask()

                if picked:
                    selected_repos.extend(picked)
                    for r in picked:
                        _success(f'Added {r["name"]}')

    if not selected_repos:
        click.echo()
        click.echo(click.style('No repos selected. Run `reviewd init` again when ready.', fg='yellow'))
        return

    # Verify git fetch works non-interactively for each repo
    click.echo()
    _info('Checking git access for selected repos...')
    for repo in selected_repos:
        result = subprocess.run(
            ['git', 'fetch', '--dry-run', 'origin'],
            cwd=repo['path'],
            capture_output=True,
            timeout=15,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        )
        if result.returncode == 0:
            _success(f'{repo["name"]} — git fetch OK')
        else:
            _error(f'{repo["name"]} — git fetch failed (credentials not cached?)')
            _info('reviewd needs non-interactive git fetch. Set up a credential helper:')
            _info('  git config --global credential.helper store')
            _info('  Then run: cd ' + repo['path'] + ' && git fetch')

    # 3. Collect credentials per provider
    providers = {r['provider'] for r in selected_repos}
    github_token = None
    bitbucket_creds: dict[str, str] = {}

    if 'github' in providers:
        _section('GitHub Credentials')
        gh_repos = [r['name'] for r in selected_repos if r['provider'] == 'github']
        github_token = _prompt_github_token(gh_repos)

    if 'bitbucket' in providers:
        _section('BitBucket Credentials')
        bb_repos = [r for r in selected_repos if r['provider'] == 'bitbucket']
        bitbucket_creds = _prompt_bitbucket_tokens(bb_repos)

    # 4. AI CLI choice
    _section('AI CLI')
    cli = questionary.select(
        'Which AI CLI?',
        choices=['claude', 'gemini'],
        default='claude',
        style=STYLE,
        instruction='(↑↓ move, enter select)',
    ).unsafe_ask()

    # 5. Write configs
    _section('Writing Configuration')

    config_yaml = _build_global_config_yaml(selected_repos, github_token, bitbucket_creds, cli)
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(config_yaml)
    _success(f'Created {config_path}')

    # Project configs
    project_repos = [r for r in selected_repos if Path(r['path']).is_dir()]
    existing = [r for r in project_repos if (Path(r['path']) / '.reviewd.yaml').exists()]
    missing = [r for r in project_repos if not (Path(r['path']) / '.reviewd.yaml').exists()]

    if missing:
        if existing:
            existing_names = ', '.join(r['name'] for r in existing)
            _info(f'.reviewd.yaml already exists in: {existing_names}')
        missing_names = ', '.join(r['name'] for r in missing)
        create_project = questionary.confirm(
            f'Create .reviewd.yaml in {missing_names}? (per-project settings)',
            default=True,
            style=STYLE,
        ).unsafe_ask()

        if create_project:
            for repo in missing:
                project_path = Path(repo['path']) / '.reviewd.yaml'
                project_path.write_text(PROJECT_CONFIG_TEMPLATE)
                _success(f'Created {repo["name"]}/.reviewd.yaml')

    # 6. Done
    click.echo()
    click.echo(click.style('Setup complete!', fg='green', bold=True))
    click.echo()
    click.echo(f'  {click.style("reviewd ls", bold=True)}              — see your repos and open PRs')
    click.echo(f'  {click.style("reviewd watch", bold=True)}           — start watching for new PRs')
    click.echo(f'  {click.style("reviewd pr <repo> <id>", bold=True)}  — one-shot review of a specific PR')
    click.echo()
    config_file = click.style(str(config_path), dim=True)
    click.echo(f'  To add more repos or change settings, edit {config_file}')
    click.echo('  Per-project .reviewd.yaml files have commented-out sample config for all options')
    click.echo()
