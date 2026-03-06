from __future__ import annotations

import pytest

from reviewd.models import GlobalConfig, PRInfo, ProjectConfig, RepoConfig
from reviewd.providers.base import GitProvider
from reviewd.state import StateDB


class FakeProvider(GitProvider):
    def __init__(self):
        self.posted_comments: list[dict] = []
        self.deleted_comments: list[int] = []
        self.approved: list[tuple[str, int]] = []
        self._next_comment_id = 100

    def list_open_prs(self, repo_slug):
        return []

    def get_pr(self, repo_slug, pr_id):
        raise NotImplementedError

    def post_comment(self, repo_slug, pr_id, body, *, file_path=None, line=None, end_line=None, source_commit=None):
        cid = self._next_comment_id
        self._next_comment_id += 1
        self.posted_comments.append(
            {
                'id': cid,
                'repo_slug': repo_slug,
                'pr_id': pr_id,
                'body': body,
                'file_path': file_path,
                'line': line,
            }
        )
        return cid

    def delete_comment(self, repo_slug, pr_id, comment_id):
        self.deleted_comments.append(comment_id)
        return True

    def approve_pr(self, repo_slug, pr_id):
        self.approved.append((repo_slug, pr_id))


@pytest.fixture()
def pr():
    return PRInfo(
        repo_slug='team/my-repo',
        pr_id=42,
        title='Fix bug in parser',
        author='alice',
        source_branch='fix/parser',
        destination_branch='main',
        source_commit='abc1234567890',  # pragma: allowlist secret
        url='https://example.com/pr/42',
    )


@pytest.fixture()
def global_config():
    return GlobalConfig(
        repos=[RepoConfig(name='my-repo', path='/tmp/repo', provider='bitbucket', workspace='team')],
        review_title='Test Review',
        footer='Test footer.',
    )


@pytest.fixture()
def project_config():
    return ProjectConfig()


@pytest.fixture()
def provider():
    return FakeProvider()


@pytest.fixture()
def state_db(tmp_path):
    db = StateDB(str(tmp_path / 'test.db'))
    yield db
    db.close()
