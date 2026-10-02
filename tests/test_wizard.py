"""Wizard: remote detection, token validation, generated config round trip."""

from __future__ import annotations

import subprocess

import httpx
import pytest
import respx

from reviewd.config import get_provider, load_global_config
from reviewd.wizard import _build_global_config_yaml, _detect_remote, _validate_gitlab_token


def _repo_with_remote(tmp_path, url: str) -> str:
    repo = tmp_path / 'myrepo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
    subprocess.run(['git', 'remote', 'add', 'origin', url], cwd=repo, check=True)
    return str(repo)


@pytest.mark.parametrize(
    ('url', 'slug', 'gitlab_url'),
    [
        ('git@gitlab.com:grp/sub/repo.git', 'grp/sub/repo', 'https://gitlab.com'),
        ('https://gitlab.com/grp/repo.git', 'grp/repo', 'https://gitlab.com'),
        ('https://gitlab.com/grp/repo', 'grp/repo', 'https://gitlab.com'),
        ('ssh://git@gitlab.corp.io:2222/team/svc.git', 'team/svc', 'https://gitlab.corp.io'),
        ('https://oauth2:tok@gitlab.corp.io/team/a/b.git', 'team/a/b', 'https://gitlab.corp.io'),
    ],
)
def test_detect_gitlab_remote(tmp_path, url, slug, gitlab_url):
    info = _detect_remote(_repo_with_remote(tmp_path, url))
    assert info['provider'] == 'gitlab'
    assert info['slug'] == slug
    assert info['gitlab_url'] == gitlab_url
    assert info['name'] == 'myrepo'


@pytest.mark.parametrize(
    ('url', 'provider'),
    [
        ('git@github.com:owner/gitlab-mirror.git', 'github'),
        ('git@bitbucket.org:ws/gitlab-tools.git', 'bitbucket'),
    ],
)
def test_detect_non_gitlab_remote_mentioning_gitlab(tmp_path, url, provider):
    assert _detect_remote(_repo_with_remote(tmp_path, url))['provider'] == provider


@pytest.mark.parametrize(
    'url',
    [
        'git@git.example.com:team/repo.git',
        'https://example.com/gitlab/grp/repo.git',
        'git@git.example.com:gitlab/sub/repo.git',
    ],
)
def test_detect_unknown_remote(tmp_path, url):
    assert _detect_remote(_repo_with_remote(tmp_path, url)) is None


def _gitlab_repo(name: str, slug: str, gitlab_url: str) -> dict:
    return {'name': name, 'path': f'/tmp/{name}', 'provider': 'gitlab', 'slug': slug, 'gitlab_url': gitlab_url}


def test_config_yaml_single_gitlab_com_instance():
    text = _build_global_config_yaml(
        [_gitlab_repo('a', 'grp/a', 'https://gitlab.com')],
        None,
        {},
        {'https://gitlab.com': 'tok'},
        'claude',
    )
    assert text.startswith('gitlab:\n  token: "tok"\n\n')
    assert 'url:' not in text
    assert '    repo_slug: grp/a' in text


def test_config_yaml_self_hosted_instance_writes_url():
    text = _build_global_config_yaml(
        [_gitlab_repo('a', 'grp/a', 'https://gitlab.corp.io')],
        None,
        {},
        {'https://gitlab.corp.io': 'tok'},
        'claude',
    )
    assert text.startswith('gitlab:\n  token: "tok"\n  url: https://gitlab.corp.io\n')


def test_config_yaml_round_trip_multiple_instances(tmp_path):
    text = _build_global_config_yaml(
        [
            _gitlab_repo('a', 'grp/a', 'https://gitlab.com'),
            _gitlab_repo('b', 'team/sub/b', 'https://gitlab.corp.io'),
        ],
        None,
        {},
        {'https://gitlab.corp.io': 'corp-tok', 'https://gitlab.com': 'com-tok'},
        'claude',
    )
    path = tmp_path / 'config.yaml'
    path.write_text(text)
    config = load_global_config(str(path))

    assert config.gitlab.url == 'https://gitlab.com'
    assert config.gitlab.token == 'com-tok'
    repo_a, repo_b = config.repos
    assert repo_a.gitlab is None
    assert repo_b.slug == 'team/sub/b'

    provider_a, provider_b = get_provider(config, repo_a), get_provider(config, repo_b)
    assert str(provider_a.client.base_url) == 'https://gitlab.com/api/v4/'
    assert provider_a.client.headers['Authorization'] == 'Bearer com-tok'
    assert str(provider_b.client.base_url) == 'https://gitlab.corp.io/api/v4/'
    assert provider_b.client.headers['Authorization'] == 'Bearer corp-tok'


@respx.mock
def test_validate_gitlab_token_success():
    route = respx.get('https://gitlab.corp.io/api/v4/user').mock(
        return_value=httpx.Response(200, json={'username': 'alice'}),
    )
    assert _validate_gitlab_token('https://gitlab.corp.io', 'tok') == 'alice'
    assert route.calls.last.request.headers['Authorization'] == 'Bearer tok'


@respx.mock
def test_validate_gitlab_token_rejected():
    respx.get('https://gitlab.com/api/v4/user').mock(return_value=httpx.Response(401))
    assert _validate_gitlab_token('https://gitlab.com', 'bad') is None


@respx.mock
def test_validate_gitlab_token_network_error():
    respx.get('https://gitlab.com/api/v4/user').mock(side_effect=httpx.ConnectError('unreachable'))
    assert _validate_gitlab_token('https://gitlab.com', 'tok') is None
