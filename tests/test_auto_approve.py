"""Auto-approve: gates checked, approve endpoint called or blocked."""

from __future__ import annotations

from helpers import make_finding, make_result

from reviewd.commenter import post_review
from reviewd.models import AutoApproveConfig, ProjectConfig


def test_approve_called_when_all_gates_pass(provider, state_db, pr, global_config):
    """AI approves + gates pass → provider.approve_pr called."""
    project_config = ProjectConfig(
        auto_approve=AutoApproveConfig(enabled=True, max_severity='suggestion', max_findings=5),
    )
    result = make_result(
        [make_finding(severity='nitpick')],
        approve=True,
        approve_reason='Looks good',
    )

    post_review(provider, state_db, pr, result, project_config, global_config, diff_lines=20)

    assert len(provider.approved) == 1
    assert provider.approved[0] == (pr.repo_slug, pr.pr_id)
    # Rationale shows in summary
    summary = provider.posted_comments[-1]['body']
    assert 'Looks good' in summary


def test_approve_blocked_by_severity(provider, state_db, pr, global_config):
    """Critical finding blocks approval when max_severity is suggestion."""
    project_config = ProjectConfig(
        auto_approve=AutoApproveConfig(enabled=True, max_severity='suggestion'),
    )
    result = make_result(
        [make_finding(severity='critical')],
        approve=True,
        approve_reason='Should not show',
    )

    post_review(provider, state_db, pr, result, project_config, global_config)

    assert len(provider.approved) == 0


def test_approve_blocked_by_diff_size(provider, state_db, pr, global_config):
    """Diff exceeds max_diff_lines → no approval."""
    project_config = ProjectConfig(
        auto_approve=AutoApproveConfig(enabled=True, max_diff_lines=50),
    )
    result = make_result(approve=True, approve_reason='Small change')

    post_review(provider, state_db, pr, result, project_config, global_config, diff_lines=100)

    assert len(provider.approved) == 0


def test_approve_blocked_by_finding_count(provider, state_db, pr, global_config):
    """Too many non-good findings → no approval."""
    project_config = ProjectConfig(
        auto_approve=AutoApproveConfig(enabled=True, max_findings=1),
    )
    result = make_result(
        [make_finding(title='A'), make_finding(title='B', line=20)],
        approve=True,
        approve_reason='Minor stuff',
    )

    post_review(provider, state_db, pr, result, project_config, global_config)

    assert len(provider.approved) == 0


def test_approve_blocked_when_ai_says_no(provider, state_db, pr, global_config):
    """AI sets approve=False → no approval even if gates pass."""
    project_config = ProjectConfig(
        auto_approve=AutoApproveConfig(enabled=True),
    )
    result = make_result(approve=False)

    post_review(provider, state_db, pr, result, project_config, global_config)

    assert len(provider.approved) == 0


def test_no_approve_when_disabled(provider, state_db, pr, global_config, project_config):
    """Default config (auto_approve disabled) → never calls approve."""
    result = make_result(approve=True, approve_reason='Looks great')

    post_review(provider, state_db, pr, result, project_config, global_config)

    assert len(provider.approved) == 0
    # Rationale hidden when disabled
    summary = provider.posted_comments[-1]['body']
    assert 'Auto-approve rationale' not in summary


def test_good_findings_excluded_from_count(provider, state_db, pr, global_config):
    """Good findings don't count toward max_findings."""
    project_config = ProjectConfig(
        auto_approve=AutoApproveConfig(enabled=True, max_findings=0),
    )
    result = make_result(
        [make_finding(severity='good', title='Nice code')],
        approve=True,
        approve_reason='Clean PR',
    )

    post_review(provider, state_db, pr, result, project_config, global_config)

    assert len(provider.approved) == 1
