"""Full review cycle: AI output → parse → post comments → state updated."""

from __future__ import annotations

import httpx
import respx
from helpers import AI_JSON_OUTPUT, make_finding, make_result

from reviewd.commenter import post_review
from reviewd.models import GitlabConfig, ProjectConfig, Severity
from reviewd.providers.gitlab import GitlabProvider
from reviewd.reviewer import extract_json, parse_review_result


def test_full_review_posts_inline_and_summary(provider, state_db, pr, global_config, project_config):
    """AI returns 2 findings → 1 critical gets inline, both appear in summary, state tracks IDs."""
    project_config = ProjectConfig(inline_comments_for=['critical'])

    data = extract_json(f'Some preamble\n```json\n{AI_JSON_OUTPUT}\n```\nDone.')
    result = parse_review_result(data)

    assert len(result.findings) == 2
    assert result.findings[1].severity == Severity.CRITICAL

    post_review(provider, state_db, pr, result, project_config, global_config)

    # 1 inline (critical) + 1 summary
    assert len(provider.posted_comments) == 2

    inline = provider.posted_comments[0]
    assert inline['file_path'] == 'src/db.py'
    assert inline['line'] == 25
    assert 'SQL injection' in inline['body']

    summary = provider.posted_comments[1]
    assert summary['file_path'] is None
    assert 'SQL injection' in summary['body']
    assert 'Use f-string' in summary['body']

    # State has both comment IDs tracked
    tracked = state_db.get_comment_ids(pr.repo_slug, pr.pr_id)
    assert len(tracked) == 2


def test_re_review_deletes_old_comments_first(provider, state_db, pr, global_config, project_config):
    """Second review deletes old comments before posting new ones."""
    result = make_result([make_finding()])

    # First review
    post_review(provider, state_db, pr, result, project_config, global_config)
    first_ids = state_db.get_comment_ids(pr.repo_slug, pr.pr_id)
    assert len(first_ids) == 1

    # Second review
    post_review(provider, state_db, pr, result, project_config, global_config)

    assert provider.deleted_comments == first_ids
    new_ids = state_db.get_comment_ids(pr.repo_slug, pr.pr_id)
    assert len(new_ids) == 1
    assert new_ids[0] != first_ids[0]


def test_duplicate_findings_deduplicated(provider, state_db, pr, global_config, project_config):
    """Two findings with same file/line/title → only one posted."""
    f1 = make_finding(title='Same issue', file='a.py', line=1)
    f2 = make_finding(title='Same issue', file='a.py', line=1)
    result = make_result([f1, f2])

    post_review(provider, state_db, pr, result, project_config, global_config)

    summary = provider.posted_comments[0]['body']
    assert summary.count('Same issue') == 1


def test_skip_severities_filtered(provider, state_db, pr, global_config):
    """Findings with skipped severities don't appear in output."""
    project_config = ProjectConfig(skip_severities=['nitpick'])
    result = make_result(
        [
            make_finding(severity='critical', title='Real bug'),
            make_finding(severity='nitpick', title='Style nit'),
        ]
    )

    post_review(provider, state_db, pr, result, project_config, global_config)

    summary = provider.posted_comments[0]['body']
    assert 'Real bug' in summary
    assert 'Style nit' not in summary


def test_dry_run_posts_nothing(provider, state_db, pr, global_config, project_config, capsys):
    """Dry run prints output but makes no API calls."""
    result = make_result([make_finding()])

    post_review(provider, state_db, pr, result, project_config, global_config, dry_run=True)

    assert len(provider.posted_comments) == 0
    assert len(state_db.get_comment_ids(pr.repo_slug, pr.pr_id)) == 0
    out = capsys.readouterr().out
    assert 'DRY RUN' in out
    assert 'Test finding' in out


GL_MR = 'https://gitlab.com/api/v4/projects/team%2Fmy-repo/merge_requests/42'


def _mock_gitlab_mr():
    respx.get(GL_MR).mock(
        return_value=httpx.Response(
            200,
            json={
                'iid': 42,
                'title': 'Fix bug in parser',
                'author': {'username': 'alice'},
                'source_branch': 'fix/parser',
                'target_branch': 'main',
                'sha': 'abc1234567890',  # pragma: allowlist secret
                'web_url': 'https://gitlab.com/team/my-repo/-/merge_requests/42',
                'diff_refs': {'base_sha': 'b', 'start_sha': 's', 'head_sha': 'abc1234567890'},
            },
        ),
    )
    respx.get(f'{GL_MR}/diffs').mock(return_value=httpx.Response(200, json=[]))


@respx.mock
def test_gitlab_inline_rejected_still_posts_rest(state_db, pr, global_config):
    """GitLab rejects an inline comment on a line outside the diff → skipped, others + summary posted."""
    _mock_gitlab_mr()
    discussions = respx.post(f'{GL_MR}/discussions').mock(
        side_effect=[
            httpx.Response(400, json={'message': {'line_code': ["can't be blank"]}}),
            httpx.Response(201, json={'notes': [{'id': 2}]}),
        ],
    )
    notes = respx.post(f'{GL_MR}/notes').mock(return_value=httpx.Response(201, json={'id': 3}))
    result = make_result(
        [
            make_finding(severity='critical', title='Outside diff', line=999),
            make_finding(severity='critical', title='Inside diff', line=10),
        ]
    )
    provider = GitlabProvider(GitlabConfig(token='fake'))

    post_review(provider, state_db, pr, result, ProjectConfig(inline_comments_for=['critical']), global_config)

    assert discussions.call_count == 2
    assert notes.call_count == 1
    assert sorted(state_db.get_comment_ids(pr.repo_slug, pr.pr_id)) == [2, 3]


@respx.mock
def test_gitlab_re_review_deletes_old_notes(state_db, pr, global_config):
    _mock_gitlab_mr()
    respx.post(f'{GL_MR}/discussions').mock(
        side_effect=[
            httpx.Response(201, json={'notes': [{'id': 10}]}),
            httpx.Response(201, json={'notes': [{'id': 20}]}),
        ],
    )
    respx.post(f'{GL_MR}/notes').mock(
        side_effect=[httpx.Response(201, json={'id': 11}), httpx.Response(201, json={'id': 21})],
    )
    deletes = respx.delete(url__regex=rf'{GL_MR}/notes/\d+').mock(return_value=httpx.Response(204))
    result = make_result([make_finding(severity='critical')])
    project_config = ProjectConfig(inline_comments_for=['critical'])

    post_review(GitlabProvider(GitlabConfig(token='fake')), state_db, pr, result, project_config, global_config)
    post_review(GitlabProvider(GitlabConfig(token='fake')), state_db, pr, result, project_config, global_config)

    deleted = sorted(int(str(call.request.url).rsplit('/', 1)[1]) for call in deletes.calls)
    assert deleted == [10, 11]
    assert sorted(state_db.get_comment_ids(pr.repo_slug, pr.pr_id)) == [20, 21]


@respx.mock
def test_gitlab_critical_task_is_skipped(state_db, pr, global_config):
    """critical_task is BitBucket-only; on GitLab no task endpoints are touched."""
    respx.post(f'{GL_MR}/notes').mock(return_value=httpx.Response(201, json={'id': 1}))
    unexpected = respx.route().mock(return_value=httpx.Response(404))
    result = make_result([make_finding(severity='critical')])
    project_config = ProjectConfig(inline_comments_for=[], critical_task=True)

    post_review(GitlabProvider(GitlabConfig(token='fake')), state_db, pr, result, project_config, global_config)

    assert not unexpected.called
