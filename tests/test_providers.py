"""Provider HTTP interactions: mock httpx, verify correct API calls."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from reviewd.models import GithubConfig, GitlabConfig
from reviewd.providers.bitbucket import BitbucketProvider
from reviewd.providers.github import GithubProvider
from reviewd.providers.gitlab import GitlabProvider

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


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------

GL_MRS = 'https://gitlab.com/api/v4/projects/grp%2Fsub%2Frepo/merge_requests'


def _gl_mr(iid=7, head_sha='head1', **overrides):
    return {
        'iid': iid,
        'title': 'Add feature',
        'author': {'username': 'alice'},
        'source_branch': 'feat',
        'target_branch': 'main',
        'sha': head_sha,
        'web_url': f'https://gitlab.com/grp/sub/repo/-/merge_requests/{iid}',
        'draft': False,
        'diff_refs': {'base_sha': 'base1', 'start_sha': 'start1', 'head_sha': head_sha},
        **overrides,
    }


def _gl_provider(**kwargs):
    return GitlabProvider(GitlabConfig(token='fake', **kwargs))


GL_DIFF = """@@ -10,4 +10,5 @@ def handler():
 a = 1
-b = 2
+c = 3
+d = 4
 e = 5
 f = 6
@@ -30,2 +31,3 @@ def other():
 g = 7
+h = 8
 i = 9
"""


def _gl_diffs(files=None):
    if files is None:
        files = [{'old_path': 'app.py', 'new_path': 'app.py', 'diff': GL_DIFF}]
    return respx.get(f'{GL_MRS}/7/diffs').mock(return_value=httpx.Response(200, json=files))


@respx.mock
def test_gitlab_list_open_prs():
    route = respx.get(GL_MRS).mock(return_value=httpx.Response(200, json=[_gl_mr()]))
    prs = _gl_provider().list_open_prs('grp/sub/repo')
    assert len(prs) == 1
    pr = prs[0]
    assert pr.pr_id == 7
    assert pr.title == 'Add feature'
    assert pr.author == 'alice'
    assert pr.source_branch == 'feat'
    assert pr.destination_branch == 'main'
    assert pr.source_commit == 'head1'
    assert pr.url == 'https://gitlab.com/grp/sub/repo/-/merge_requests/7'
    assert pr.repo_slug == 'grp/sub/repo'
    req = route.calls.last.request
    assert req.url.params['state'] == 'opened'
    assert req.headers['Authorization'] == 'Bearer fake'


@respx.mock
def test_gitlab_list_open_prs_follows_pagination_with_query():
    next_url = f'{GL_MRS}?state=opened&per_page=100&page=2'
    route = respx.get(GL_MRS).mock(
        side_effect=[
            httpx.Response(200, json=[_gl_mr(iid=1)], headers={'link': f'<{next_url}>; rel="next"'}),
            httpx.Response(200, json=[_gl_mr(iid=2)]),
        ],
    )
    prs = _gl_provider().list_open_prs('grp/sub/repo')
    assert [p.pr_id for p in prs] == [1, 2]
    assert route.call_count == 2
    assert str(route.calls[1].request.url) == next_url


@respx.mock
def test_gitlab_encodes_project_path():
    route = respx.get(f'{GL_MRS}/7').mock(return_value=httpx.Response(200, json=_gl_mr()))
    _gl_provider().get_pr('grp/sub/repo', 7)
    assert route.calls.last.request.url.raw_path.startswith(b'/api/v4/projects/grp%2Fsub%2Frepo/')


@pytest.mark.parametrize(
    ('fields', 'expected'),
    [
        ({'draft': True}, True),
        ({'draft': False}, False),
        ({'work_in_progress': True}, True),
        ({}, False),
    ],
)
@respx.mock
def test_gitlab_draft_detection(fields, expected):
    mr = _gl_mr()
    del mr['draft']
    mr.update(fields)
    respx.get(f'{GL_MRS}/7').mock(return_value=httpx.Response(200, json=mr))
    assert _gl_provider().get_pr('grp/sub/repo', 7).draft is expected


@respx.mock
def test_gitlab_null_sha():
    respx.get(f'{GL_MRS}/7').mock(return_value=httpx.Response(200, json=_gl_mr(sha=None, diff_refs=None)))
    assert _gl_provider().get_pr('grp/sub/repo', 7).source_commit == ''


@respx.mock
def test_gitlab_post_summary_comment():
    route = respx.post(f'{GL_MRS}/7/notes').mock(return_value=httpx.Response(201, json={'id': 555}))
    cid = _gl_provider().post_comment('grp/sub/repo', 7, 'Summary')
    assert cid == 555
    body = json.loads(route.calls.last.request.content)['body']
    assert body.startswith('Summary')
    assert body.endswith('[](reviewd)')


@respx.mock
def test_gitlab_post_inline_comment_uses_cached_diff_refs():
    respx.get(GL_MRS).mock(return_value=httpx.Response(200, json=[_gl_mr()]))
    get_mr = respx.get(f'{GL_MRS}/7')
    route = respx.post(f'{GL_MRS}/7/discussions').mock(
        return_value=httpx.Response(201, json={'id': 'disc-1', 'notes': [{'id': 777}]}),
    )
    _gl_diffs()
    provider = _gl_provider()
    provider.list_open_prs('grp/sub/repo')

    cid = provider.post_comment('grp/sub/repo', 7, 'Bug', file_path='app.py', line=12, source_commit='head1')

    assert cid == 777
    assert not get_mr.called
    payload = json.loads(route.calls.last.request.content)
    assert payload['body'].endswith('[](reviewd)')
    assert payload['position'] == {
        'base_sha': 'base1',
        'start_sha': 'start1',
        'head_sha': 'head1',
        'old_path': 'app.py',
        'new_path': 'app.py',
        'position_type': 'text',
        'new_line': 12,
    }


@respx.mock
def test_gitlab_post_inline_comment_fetches_diff_refs_when_not_cached():
    get_mr = respx.get(f'{GL_MRS}/7').mock(return_value=httpx.Response(200, json=_gl_mr()))
    route = respx.post(f'{GL_MRS}/7/discussions').mock(
        return_value=httpx.Response(201, json={'notes': [{'id': 1}]}),
    )
    _gl_diffs()
    _gl_provider().post_comment('grp/sub/repo', 7, 'Bug', file_path='app.py', line=3)
    assert get_mr.call_count == 1
    assert json.loads(route.calls.last.request.content)['position']['head_sha'] == 'head1'


@respx.mock
def test_gitlab_post_inline_comment_refetches_when_head_moved():
    respx.get(GL_MRS).mock(return_value=httpx.Response(200, json=[_gl_mr(head_sha='old')]))
    get_mr = respx.get(f'{GL_MRS}/7').mock(return_value=httpx.Response(200, json=_gl_mr(head_sha='new')))
    route = respx.post(f'{GL_MRS}/7/discussions').mock(
        return_value=httpx.Response(201, json={'notes': [{'id': 1}]}),
    )
    _gl_diffs()
    provider = _gl_provider()
    provider.list_open_prs('grp/sub/repo')

    provider.post_comment('grp/sub/repo', 7, 'Bug', file_path='app.py', line=3, source_commit='new')

    assert get_mr.call_count == 1
    assert json.loads(route.calls.last.request.content)['position']['head_sha'] == 'new'


@respx.mock
def test_gitlab_post_file_level_comment():
    respx.get(f'{GL_MRS}/7').mock(return_value=httpx.Response(200, json=_gl_mr()))
    route = respx.post(f'{GL_MRS}/7/discussions').mock(
        return_value=httpx.Response(201, json={'notes': [{'id': 1}]}),
    )
    _gl_diffs()
    _gl_provider().post_comment('grp/sub/repo', 7, 'Whole file', file_path='app.py')
    position = json.loads(route.calls.last.request.content)['position']
    assert position['position_type'] == 'file'
    assert 'new_line' not in position


@respx.mock
def test_gitlab_post_range_comment_anchors_on_end_line():
    respx.get(f'{GL_MRS}/7').mock(return_value=httpx.Response(200, json=_gl_mr()))
    route = respx.post(f'{GL_MRS}/7/discussions').mock(
        return_value=httpx.Response(201, json={'notes': [{'id': 1}]}),
    )
    _gl_diffs()
    _gl_provider().post_comment('grp/sub/repo', 7, 'Range', file_path='app.py', line=10, end_line=14)
    assert json.loads(route.calls.last.request.content)['position']['new_line'] == 14


@pytest.mark.parametrize(
    ('line', 'old_line'),
    [
        (10, 10),  # context line inside a hunk
        (11, None),  # added line
        (13, 12),  # context line after a removal and two additions
        (32, None),  # added line in the second hunk
        (33, 31),  # context line in the second hunk
    ],
)
@respx.mock
def test_gitlab_inline_comment_line_mapping(line, old_line):
    respx.get(f'{GL_MRS}/7').mock(return_value=httpx.Response(200, json=_gl_mr()))
    _gl_diffs()
    route = respx.post(f'{GL_MRS}/7/discussions').mock(return_value=httpx.Response(201, json={'notes': [{'id': 1}]}))

    _gl_provider().post_comment('grp/sub/repo', 7, 'Bug', file_path='app.py', line=line, source_commit='head1')

    position = json.loads(route.calls.last.request.content)['position']
    assert position['new_line'] == line
    assert position.get('old_line') == old_line


@respx.mock
def test_gitlab_inline_comment_on_renamed_file_uses_old_path():
    respx.get(f'{GL_MRS}/7').mock(return_value=httpx.Response(200, json=_gl_mr()))
    _gl_diffs([{'old_path': 'old/app.py', 'new_path': 'app.py', 'diff': GL_DIFF}])
    route = respx.post(f'{GL_MRS}/7/discussions').mock(return_value=httpx.Response(201, json={'notes': [{'id': 1}]}))

    _gl_provider().post_comment('grp/sub/repo', 7, 'Bug', file_path='app.py', line=13, source_commit='head1')

    position = json.loads(route.calls.last.request.content)['position']
    assert (position['old_path'], position['new_path'], position['old_line']) == ('old/app.py', 'app.py', 12)


@pytest.mark.parametrize(
    ('files', 'line', 'end_line', 'label', 'old_path'),
    [
        ([{'old_path': 'app.py', 'new_path': 'app.py', 'diff': GL_DIFF}], 5, None, 'Line 5', 'app.py'),
        ([{'old_path': 'app.py', 'new_path': 'app.py', 'diff': GL_DIFF}], 20, None, 'Line 20', 'app.py'),
        ([{'old_path': 'app.py', 'new_path': 'app.py', 'diff': GL_DIFF}], 40, None, 'Line 40', 'app.py'),
        ([{'old_path': 'app.py', 'new_path': 'app.py', 'diff': GL_DIFF}], 3, 5, 'Lines 3-5', 'app.py'),
        ([{'old_path': 'old/app.py', 'new_path': 'app.py', 'diff': GL_DIFF}], 20, None, 'Line 20', 'old/app.py'),
        ([{'old_path': 'app.py', 'new_path': 'app.py', 'diff': ''}], 13, None, 'Line 13', 'app.py'),  # collapsed
        ([{'old_path': 'other.py', 'new_path': 'other.py', 'diff': GL_DIFF}], 13, None, 'Line 13', 'app.py'),
    ],
)
@respx.mock
def test_gitlab_unclassifiable_line_falls_back_to_file_comment(files, line, end_line, label, old_path):
    respx.get(f'{GL_MRS}/7').mock(return_value=httpx.Response(200, json=_gl_mr()))
    _gl_diffs(files)
    route = respx.post(f'{GL_MRS}/7/discussions').mock(return_value=httpx.Response(201, json={'notes': [{'id': 1}]}))
    body = '🔴 **Bug**\n\nSomething is off.\n\n```suggestion\nfixed = True\n```'

    _gl_provider().post_comment(
        'grp/sub/repo', 7, body, file_path='app.py', line=line, end_line=end_line, source_commit='head1'
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload['position']['position_type'] == 'file'
    assert (payload['position']['old_path'], payload['position']['new_path']) == (old_path, 'app.py')
    assert 'new_line' not in payload['position']
    assert 'old_line' not in payload['position']
    assert payload['body'] == f'{label}: 🔴 **Bug**\n\nSomething is off.\n\n[](reviewd)'


@respx.mock
def test_gitlab_diff_fetched_once_per_head_commit():
    respx.get(f'{GL_MRS}/7').mock(return_value=httpx.Response(200, json=_gl_mr()))
    diffs = _gl_diffs()
    respx.post(f'{GL_MRS}/7/discussions').mock(return_value=httpx.Response(201, json={'notes': [{'id': 1}]}))
    provider = _gl_provider()

    for line in (10, 13, 20):
        provider.post_comment('grp/sub/repo', 7, 'Bug', file_path='app.py', line=line, source_commit='head1')

    assert diffs.call_count == 1


@pytest.mark.parametrize(('status', 'expected'), [(204, True), (404, False)])
@respx.mock
def test_gitlab_delete_comment(status, expected):
    route = respx.delete(f'{GL_MRS}/7/notes/777').mock(return_value=httpx.Response(status))
    assert _gl_provider().delete_comment('grp/sub/repo', 7, 777) is expected
    assert route.called


@respx.mock
def test_gitlab_approve_success():
    respx.post(f'{GL_MRS}/7/approve').mock(return_value=httpx.Response(201, json={}))
    assert _gl_provider().approve_pr('grp/sub/repo', 7) is True


@pytest.mark.parametrize('status', [403, 405])
@respx.mock
def test_gitlab_approve_refused_returns_gracefully(status):
    respx.post(f'{GL_MRS}/7/approve').mock(return_value=httpx.Response(status, json={'message': 'nope'}))
    assert _gl_provider().approve_pr('grp/sub/repo', 7) is False


@respx.mock
def test_gitlab_approve_401_with_valid_token_returns_gracefully():
    respx.post(f'{GL_MRS}/7/approve').mock(return_value=httpx.Response(401, json={'message': '401 Unauthorized'}))
    user = respx.get('https://gitlab.com/api/v4/user').mock(return_value=httpx.Response(200, json={'username': 'bot'}))
    assert _gl_provider().approve_pr('grp/sub/repo', 7) is False
    assert user.called


@respx.mock
def test_gitlab_approve_401_with_rejected_token_raises():
    respx.post(f'{GL_MRS}/7/approve').mock(return_value=httpx.Response(401, json={'message': '401 Unauthorized'}))
    respx.get('https://gitlab.com/api/v4/user').mock(return_value=httpx.Response(401))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _gl_provider().approve_pr('grp/sub/repo', 7)
    assert exc.value.request.url.path.endswith('/approve')


@respx.mock
def test_gitlab_approve_insufficient_scope_raises():
    respx.post(f'{GL_MRS}/7/approve').mock(
        return_value=httpx.Response(403, json={'error': 'insufficient_scope', 'scope': 'api'}),
    )
    with pytest.raises(httpx.HTTPStatusError):
        _gl_provider().approve_pr('grp/sub/repo', 7)


@respx.mock
def test_gitlab_approve_server_error_raises():
    respx.post(f'{GL_MRS}/7/approve').mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        _gl_provider().approve_pr('grp/sub/repo', 7)


@respx.mock
def test_gitlab_retries_on_rate_limit(monkeypatch):
    sleeps = []
    monkeypatch.setattr('reviewd.providers.base.time.sleep', sleeps.append)
    route = respx.get(f'{GL_MRS}/7').mock(
        side_effect=[
            httpx.Response(429, headers={'Retry-After': '3'}),
            httpx.Response(200, json=_gl_mr()),
        ],
    )
    assert _gl_provider().get_pr('grp/sub/repo', 7).pr_id == 7
    assert route.call_count == 2
    assert sleeps == [3]


@respx.mock
def test_gitlab_inline_comment_without_diff_refs_raises_clear_error():
    respx.get(f'{GL_MRS}/7').mock(return_value=httpx.Response(200, json=_gl_mr(diff_refs=None)))
    discussions = respx.post(f'{GL_MRS}/7/discussions')
    with pytest.raises(RuntimeError, match='MR !7 has no diff_refs yet'):
        _gl_provider().post_comment('grp/sub/repo', 7, 'body', file_path='a.py', line=3, source_commit='head1')
    assert not discussions.called


@pytest.mark.parametrize(
    ('url', 'expected'),
    [
        ('https://gitlab.example.com', 'https://gitlab.example.com/api/v4/'),
        ('https://gitlab.example.com/', 'https://gitlab.example.com/api/v4/'),
        ('https://example.com/gitlab/', 'https://example.com/gitlab/api/v4/'),
    ],
)
def test_gitlab_base_url(url, expected):
    assert str(_gl_provider(url=url).client.base_url) == expected


@respx.mock
def test_gitlab_self_hosted_with_subpath_requests():
    route = respx.get('https://example.com/gitlab/api/v4/projects/grp%2Frepo/merge_requests/3').mock(
        return_value=httpx.Response(200, json=_gl_mr(iid=3)),
    )
    _gl_provider(url='https://example.com/gitlab').get_pr('grp/repo', 3)
    assert route.called
