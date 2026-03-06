"""State DB lifecycle: reviews tracked, comments recorded/cleaned, re-review works."""

from __future__ import annotations


def test_review_lifecycle(state_db):
    """start → finish → has_review returns True."""
    state_db.start_review('repo', 1, 'abc123')
    # in_progress also counts as reviewed (prevents duplicate reviews)
    assert state_db.has_review('repo', 1, 'abc123') is True

    state_db.finish_review('repo', 1, 'abc123')
    assert state_db.has_review('repo', 1, 'abc123') is True


def test_different_commit_not_reviewed(state_db):
    """Same PR, different commit → not reviewed."""
    state_db.start_review('repo', 1, 'abc123')
    state_db.finish_review('repo', 1, 'abc123')
    assert state_db.has_review('repo', 1, 'def456') is False


def test_comment_tracking_and_cleanup(state_db):
    """Record comments, retrieve IDs, delete clears them."""
    state_db.record_comment('repo', 1, 100)
    state_db.record_comment('repo', 1, 101)
    state_db.record_comment('repo', 2, 200)  # different PR

    ids = state_db.get_comment_ids('repo', 1)
    assert sorted(ids) == [100, 101]

    state_db.delete_comments('repo', 1)
    assert state_db.get_comment_ids('repo', 1) == []
    assert state_db.get_comment_ids('repo', 2) == [200]  # untouched


def test_has_any_review(state_db):
    """has_any_review checks for any successful review of the PR."""
    assert state_db.has_any_review('repo', 1) is False

    state_db.start_review('repo', 1, 'abc')
    assert state_db.has_any_review('repo', 1) is False  # in_progress

    state_db.finish_review('repo', 1, 'abc')
    assert state_db.has_any_review('repo', 1) is True


def test_error_review_not_counted(state_db):
    """Failed review doesn't block re-review."""
    state_db.start_review('repo', 1, 'abc')
    state_db.finish_review('repo', 1, 'abc', error='Something broke')
    # Error status means we should re-review
    assert state_db.has_any_review('repo', 1) is False


def test_review_history(state_db):
    """get_review_history returns recent reviews."""
    state_db.start_review('repo', 1, 'abc')
    state_db.finish_review('repo', 1, 'abc')
    state_db.start_review('repo', 2, 'def')
    state_db.finish_review('repo', 2, 'def')

    history = state_db.get_review_history('repo')
    assert len(history) == 2
    # Both PRs present; ordered by created_at DESC
    pr_ids = {h['pr_id'] for h in history}
    assert pr_ids == {1, 2}
