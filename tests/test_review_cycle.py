"""Full review cycle: AI output → parse → post comments → state updated."""

from __future__ import annotations

from helpers import AI_JSON_OUTPUT, make_finding, make_result

from reviewd.commenter import post_review
from reviewd.models import ProjectConfig, Severity
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
