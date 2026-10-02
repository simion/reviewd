"""Config: env var substitution, max_concurrent_reviews, GIT_TERMINAL_PROMPT, provider resolution."""

from __future__ import annotations

import pytest
import yaml

from reviewd.config import get_provider, load_global_config
from reviewd.providers.bitbucket import BitbucketProvider
from reviewd.providers.github import GithubProvider
from reviewd.providers.gitlab import GitlabProvider


def _write_config(tmp_path, data: dict) -> str:
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.dump(data))
    return str(path)


def test_env_var_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_GH_TOKEN', 'ghp_secret123')
    path = _write_config(
        tmp_path,
        {
            'github': {'token': '${TEST_GH_TOKEN}'},
            'repos': [{'name': 'r', 'path': '/tmp/r', 'provider': 'github'}],
        },
    )
    config = load_global_config(path)
    assert config.github.token == 'ghp_secret123'


def test_missing_env_var_raises(tmp_path, monkeypatch):
    monkeypatch.delenv('NONEXISTENT_VAR_XYZ', raising=False)
    path = _write_config(
        tmp_path,
        {
            'github': {'token': '${NONEXISTENT_VAR_XYZ}'},
            'repos': [{'name': 'r', 'path': '/tmp/r', 'provider': 'github'}],
        },
    )
    with pytest.raises(ValueError, match='NONEXISTENT_VAR_XYZ is not set'):
        load_global_config(path)


def test_max_concurrent_reviews_parsed(tmp_path):
    path = _write_config(
        tmp_path,
        {
            'max_concurrent_reviews': 8,
            'repos': [{'name': 'r', 'path': '/tmp/r', 'provider': 'github'}],
        },
    )
    config = load_global_config(path)
    assert config.max_concurrent_reviews == 8


def test_max_concurrent_reviews_default(tmp_path):
    path = _write_config(
        tmp_path,
        {
            'repos': [{'name': 'r', 'path': '/tmp/r', 'provider': 'github'}],
        },
    )
    config = load_global_config(path)
    assert config.max_concurrent_reviews == 4


def test_git_env_has_terminal_prompt_disabled():
    from reviewd.reviewer import _GIT_ENV

    assert _GIT_ENV['GIT_TERMINAL_PROMPT'] == '0'

    from reviewd.config import _GIT_ENV as _CONFIG_GIT_ENV

    assert _CONFIG_GIT_ENV['GIT_TERMINAL_PROMPT'] == '0'


def test_gitlab_config_env_vars_and_default_url(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_GL_TOKEN', 'glpat-secret')
    path = _write_config(
        tmp_path,
        {
            'gitlab': {'token': '${TEST_GL_TOKEN}'},
            'repos': [{'name': 'r', 'path': '/tmp/r', 'provider': 'gitlab', 'repo_slug': 'grp/r'}],
        },
    )
    config = load_global_config(path)
    assert config.gitlab.token == 'glpat-secret'
    assert config.gitlab.url == 'https://gitlab.com'


def test_gitlab_url_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_GL_URL', 'https://gitlab.example.com')
    path = _write_config(
        tmp_path,
        {
            'gitlab': {'token': 't', 'url': '${TEST_GL_URL}'},
            'repos': [{'name': 'r', 'path': '/tmp/r', 'provider': 'gitlab'}],
        },
    )
    assert load_global_config(path).gitlab.url == 'https://gitlab.example.com'


def test_gitlab_per_repo_override(tmp_path):
    path = _write_config(
        tmp_path,
        {
            'gitlab': {'token': 'global-token'},
            'repos': [
                {'name': 'a', 'path': '/tmp/a', 'provider': 'gitlab', 'repo_slug': 'grp/a'},
                {
                    'name': 'b',
                    'path': '/tmp/b',
                    'provider': 'gitlab',
                    'repo_slug': 'team/b',
                    'gitlab': {'token': 'corp-token', 'url': 'https://gitlab.corp.io'},
                },
            ],
        },
    )
    config = load_global_config(path)
    provider_a, provider_b = (get_provider(config, repo) for repo in config.repos)

    assert isinstance(provider_a, GitlabProvider)
    assert str(provider_a.client.base_url) == 'https://gitlab.com/api/v4/'
    assert provider_a.client.headers['Authorization'] == 'Bearer global-token'
    assert str(provider_b.client.base_url) == 'https://gitlab.corp.io/api/v4/'
    assert provider_b.client.headers['Authorization'] == 'Bearer corp-token'


def test_gitlab_missing_config_raises(tmp_path):
    path = _write_config(
        tmp_path,
        {'repos': [{'name': 'lonely', 'path': '/tmp/r', 'provider': 'gitlab'}]},
    )
    config = load_global_config(path)
    with pytest.raises(ValueError, match='No gitlab config found for repo "lonely"'):
        get_provider(config, config.repos[0])


def test_mixed_providers_resolve_correctly(tmp_path):
    path = _write_config(
        tmp_path,
        {
            'github': {'token': 'gh'},
            'gitlab': {'token': 'gl'},
            'bitbucket': {'team': 'bb'},
            'repos': [
                {'name': 'gh', 'path': '/tmp/gh', 'provider': 'github', 'repo_slug': 'o/gh'},
                {'name': 'gl', 'path': '/tmp/gl', 'provider': 'gitlab', 'repo_slug': 'g/gl'},
                {'name': 'bb', 'path': '/tmp/bb', 'provider': 'bitbucket', 'workspace': 'team'},
            ],
        },
    )
    config = load_global_config(path)
    providers = [type(get_provider(config, repo)) for repo in config.repos]
    assert providers == [GithubProvider, GitlabProvider, BitbucketProvider]
