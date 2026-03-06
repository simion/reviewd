"""Provider HTTP interactions: mock httpx, verify correct API calls."""

from __future__ import annotations

import json

import httpx
import respx

from reviewd.models import GithubConfig
from reviewd.providers.bitbucket import BitbucketProvider
from reviewd.providers.github import GithubProvider

# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


@respx.mock
def test_github_list_open_prs():
    respx.get('https://api.github.com/repos/owner/repo/pulls').mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    'number': 1,
                    'title': 'Fix bug',
                    'user': {'login': 'alice'},
                    'head': {'ref': 'fix', 'sha': 'abc123'},
                    'base': {'ref': 'main'},
                    'html_url': 'https://github.com/owner/repo/pull/1',
                    'draft': False,
                },
            ],
        ),
    )
    provider = GithubProvider(GithubConfig(token='fake'))
    prs = provider.list_open_prs('owner/repo')
    assert len(prs) == 1
    assert prs[0].pr_id == 1
    assert prs[0].author == 'alice'
    assert prs[0].source_branch == 'fix'


@respx.mock
def test_github_post_inline_comment():
    respx.post('https://api.github.com/repos/owner/repo/pulls/1/comments').mock(
        return_value=httpx.Response(201, json={'id': 999}),
    )
    provider = GithubProvider(GithubConfig(token='fake'))
    cid = provider.post_comment('owner/repo', 1, 'Issue here', file_path='main.py', line=10, source_commit='abc')
    assert cid == 999
    req = respx.calls.last.request
    body = req.content.decode()
    assert 'main.py' in body
    parsed = json.loads(body)
    assert parsed['line'] == 10


@respx.mock
def test_github_post_summary_comment():
    respx.post('https://api.github.com/repos/owner/repo/issues/1/comments').mock(
        return_value=httpx.Response(201, json={'id': 500}),
    )
    provider = GithubProvider(GithubConfig(token='fake'))
    cid = provider.post_comment('owner/repo', 1, 'Summary here')
    assert cid == 500


@respx.mock
def test_github_delete_comment_tries_both_endpoints():
    respx.delete('https://api.github.com/repos/owner/repo/issues/comments/123').mock(
        return_value=httpx.Response(404),
    )
    respx.delete('https://api.github.com/repos/owner/repo/pulls/comments/123').mock(
        return_value=httpx.Response(204),
    )
    provider = GithubProvider(GithubConfig(token='fake'))
    assert provider.delete_comment('owner/repo', 1, 123) is True


@respx.mock
def test_github_approve_self_returns_gracefully():
    respx.post('https://api.github.com/repos/owner/repo/pulls/1/reviews').mock(
        return_value=httpx.Response(422, json={'message': 'Can not approve your own pull request'}),
    )
    provider = GithubProvider(GithubConfig(token='fake'))
    # Should not raise
    provider.approve_pr('owner/repo', 1)


@respx.mock
def test_github_approve_success():
    respx.post('https://api.github.com/repos/owner/repo/pulls/1/reviews').mock(
        return_value=httpx.Response(200, json={'id': 1}),
    )
    provider = GithubProvider(GithubConfig(token='fake'))
    provider.approve_pr('owner/repo', 1)


# ---------------------------------------------------------------------------
# BitBucket
# ---------------------------------------------------------------------------


@respx.mock
def test_bitbucket_list_open_prs():
    respx.get('https://api.bitbucket.org/2.0/repositories/team/repo/pullrequests').mock(
        return_value=httpx.Response(
            200,
            json={
                'values': [
                    {
                        'id': 7,
                        'title': 'Add feature',
                        'author': {'display_name': 'bob'},
                        'source': {'branch': {'name': 'feat'}, 'commit': {'hash': 'def456'}},
                        'destination': {'branch': {'name': 'main'}},
                        'links': {'html': {'href': 'https://bb.org/pr/7'}},
                    }
                ],
            },
        ),
    )
    provider = BitbucketProvider('team', 'fake-token')
    prs = provider.list_open_prs('repo')
    assert len(prs) == 1
    assert prs[0].pr_id == 7
    assert prs[0].author == 'bob'


@respx.mock
def test_bitbucket_pagination_dedup():
    """BB sometimes returns duplicate items across pages — verify dedup by ID."""
    page1 = {
        'values': [{'id': 1, 'data': 'a'}, {'id': 2, 'data': 'b'}],
        'next': 'https://api.bitbucket.org/2.0/next-page',
    }
    page2 = {
        'values': [{'id': 2, 'data': 'b'}, {'id': 3, 'data': 'c'}],
    }
    respx.get('https://api.bitbucket.org/2.0/repositories/team/repo/items').mock(
        return_value=httpx.Response(200, json=page1),
    )
    respx.get('https://api.bitbucket.org/2.0/next-page').mock(
        return_value=httpx.Response(200, json=page2),
    )
    provider = BitbucketProvider('team', 'fake-token')
    results = provider._paginate('/repositories/team/repo/items')
    ids = [r['id'] for r in results]
    assert ids == [1, 2, 3]


@respx.mock
def test_bitbucket_approve_self_returns_gracefully():
    respx.post('https://api.bitbucket.org/2.0/repositories/team/repo/pullrequests/7/approve').mock(
        return_value=httpx.Response(400, text='You can not approve your own pull request'),
    )
    provider = BitbucketProvider('team', 'fake-token')
    # Should not raise
    provider.approve_pr('repo', 7)


@respx.mock
def test_bitbucket_post_inline_comment():
    respx.post('https://api.bitbucket.org/2.0/repositories/team/repo/pullrequests/7/comments').mock(
        return_value=httpx.Response(201, json={'id': 42}),
    )
    provider = BitbucketProvider('team', 'fake-token')
    cid = provider.post_comment('repo', 7, 'Issue', file_path='app.py', line=5)
    assert cid == 42
    req = respx.calls.last.request
    body = req.content.decode()
    assert 'app.py' in body
