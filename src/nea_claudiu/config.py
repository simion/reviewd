from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from nea_claudiu.models import (
    AICli,
    BitbucketConfig,
    GithubConfig,
    GlobalConfig,
    ProjectConfig,
    RepoConfig,
)
from nea_claudiu.providers.base import GitProvider

ENV_VAR_PATTERN = re.compile(r'\$\{(\w+)\}')


def _resolve_env_vars(value: str) -> str:
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ValueError(f'Environment variable {var_name} is not set')
        return env_value

    return ENV_VAR_PATTERN.sub(replacer, value)


def _parse_bitbucket_config(data: dict) -> BitbucketConfig:
    return BitbucketConfig(
        workspace=_resolve_env_vars(str(data['workspace'])),
        auth_token=_resolve_env_vars(str(data['auth_token'])),
    )


def _parse_github_config(data: dict) -> GithubConfig:
    return GithubConfig(
        token=_resolve_env_vars(str(data['token'])),
    )


def load_global_config(path: str | Path | None = None) -> GlobalConfig:
    if path is None:
        path = Path('~/.config/nea-claudiu/config.yaml').expanduser()
    else:
        path = Path(path).expanduser()

    with open(path) as f:
        data = yaml.safe_load(f)

    global_bb = None
    if 'bitbucket' in data:
        global_bb = _parse_bitbucket_config(data['bitbucket'])

    global_gh = None
    if 'github' in data:
        global_gh = _parse_github_config(data['github'])

    global_ai_cli = AICli(data.get('ai_cli', 'claude'))

    repos = []
    for repo_data in data.get('repos', []):
        repo_bb = None
        if 'bitbucket' in repo_data:
            repo_bb = _parse_bitbucket_config(repo_data['bitbucket'])

        repo_gh = None
        if 'github' in repo_data:
            repo_gh = _parse_github_config(repo_data['github'])

        repos.append(RepoConfig(
            name=repo_data['name'],
            path=str(Path(repo_data['path']).expanduser()),
            provider=repo_data.get('provider', 'bitbucket'),
            bitbucket=repo_bb,
            github=repo_gh,
            ai_cli=AICli(repo_data.get('ai_cli', global_ai_cli)),
        ))

    state_db = data.get('state_db', '~/.local/share/nea-claudiu/state.db')
    state_db = str(Path(_resolve_env_vars(state_db)).expanduser())

    return GlobalConfig(
        repos=repos,
        bitbucket=global_bb,
        github=global_gh,
        state_db=state_db,
        ai_cli=global_ai_cli,
        instructions=data.get('instructions'),
        poll_interval_seconds=data.get('poll_interval_seconds', 60),
        reviewer_name=data.get('reviewer_name', "Nea' ~Caisă~ Claudiu"),
        footer=data.get(
            'footer',
            'Automated review by [nea-claudiu](https://github.com/simion/nea-claudiu). '
            'Findings are AI-generated — use your judgment.',
        ),
    )


def load_project_config(repo_path: str | Path, global_config: GlobalConfig) -> ProjectConfig:
    config_path = Path(repo_path) / '.nea-claudiu.yaml'
    data = {}
    if config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

    # Merge instructions: global + per-project
    parts = []
    if global_config.instructions:
        parts.append(global_config.instructions.strip())
    if data.get('instructions'):
        parts.append(data['instructions'].strip())
    # Backwards compat: support old guidelines/explore fields
    if data.get('guidelines'):
        parts.append(data['guidelines'].strip())
    if data.get('explore'):
        parts.append(data['explore'].strip())
    instructions = '\n\n'.join(parts) if parts else None

    return ProjectConfig(
        instructions=instructions,
        test_commands=data.get('test_commands', []),
        skip_title_patterns=data.get('skip_title_patterns', []),
        skip_authors=data.get('skip_authors', []),
        inline_comments_for=data.get('inline_comments_for', ['critical']),
        approve_if_no_critical=data.get('approve_if_no_critical', False),
    )


def resolve_bitbucket_config(global_config: GlobalConfig, repo_config: RepoConfig) -> BitbucketConfig:
    if repo_config.bitbucket is not None:
        return repo_config.bitbucket
    if global_config.bitbucket is not None:
        return global_config.bitbucket
    raise ValueError(f'No bitbucket config found for repo "{repo_config.name}"')


def resolve_github_config(global_config: GlobalConfig, repo_config: RepoConfig) -> GithubConfig:
    if repo_config.github is not None:
        return repo_config.github
    if global_config.github is not None:
        return global_config.github
    raise ValueError(f'No github config found for repo "{repo_config.name}"')


def get_provider(global_config: GlobalConfig, repo_config: RepoConfig) -> GitProvider:
    if repo_config.provider == 'github':
        from nea_claudiu.providers.github import GithubProvider
        config = resolve_github_config(global_config, repo_config)
        return GithubProvider(config)

    from nea_claudiu.providers.bitbucket import BitbucketProvider
    config = resolve_bitbucket_config(global_config, repo_config)
    return BitbucketProvider(config)
