from __future__ import annotations

import logging

import httpx

from nea_claudiu.models import GithubConfig, PRInfo
from nea_claudiu.providers.base import GitProvider

logger = logging.getLogger(__name__)

BOT_MARKER = '[](nea-claudiu)'
GH_API_BASE = 'https://api.github.com'


class GithubProvider(GitProvider):
    def __init__(self, config: GithubConfig):
        self.client = httpx.Client(
            base_url=GH_API_BASE,
            headers={
                'Authorization': f'Bearer {config.token}',
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
            timeout=30,
        )

    def _paginate(self, url: str, params: dict | None = None) -> list[dict]:
        results = []
        params = params or {}
        while True:
            resp = self.client.get(url, params=params)
            resp.raise_for_status()
            results.extend(resp.json())
            link = resp.headers.get('link', '')
            next_url = _parse_next_link(link)
            if not next_url:
                break
            url = next_url
            params = {}
        return results

    def _pr_from_data(self, repo_slug: str, data: dict) -> PRInfo:
        return PRInfo(
            repo_slug=repo_slug,
            pr_id=data['number'],
            title=data['title'],
            author=data['user']['login'],
            source_branch=data['head']['ref'],
            destination_branch=data['base']['ref'],
            source_commit=data['head']['sha'],
            url=data['html_url'],
        )

    def list_open_prs(self, repo_slug: str) -> list[PRInfo]:
        url = f'/repos/{repo_slug}/pulls'
        items = self._paginate(url, {'state': 'open', 'per_page': '100'})
        return [self._pr_from_data(repo_slug, item) for item in items]

    def get_pr(self, repo_slug: str, pr_id: int) -> PRInfo:
        url = f'/repos/{repo_slug}/pulls/{pr_id}'
        resp = self.client.get(url)
        resp.raise_for_status()
        return self._pr_from_data(repo_slug, resp.json())

    def post_comment(
        self,
        repo_slug: str,
        pr_id: int,
        body: str,
        *,
        file_path: str | None = None,
        line: int | None = None,
        source_commit: str | None = None,
    ) -> int:
        marked_body = f'{body}\n\n{BOT_MARKER}'

        if file_path is not None:
            commit_id = source_commit
            if not commit_id:
                pr_resp = self.client.get(f'/repos/{repo_slug}/pulls/{pr_id}')
                pr_resp.raise_for_status()
                commit_id = pr_resp.json()['head']['sha']

            url = f'/repos/{repo_slug}/pulls/{pr_id}/comments'
            payload: dict = {
                'body': marked_body,
                'commit_id': commit_id,
                'path': file_path,
                'side': 'RIGHT',
            }
            if line is not None:
                payload['line'] = line
        else:
            url = f'/repos/{repo_slug}/issues/{pr_id}/comments'
            payload = {'body': marked_body}

        resp = self.client.post(url, json=payload)
        resp.raise_for_status()
        comment_id = resp.json()['id']
        logger.info('Posted comment %d on PR #%d', comment_id, pr_id)
        return comment_id

    def delete_bot_comments(self, repo_slug: str, pr_id: int) -> int:
        deleted = 0

        # Delete issue comments (general comments)
        issue_url = f'/repos/{repo_slug}/issues/{pr_id}/comments'
        issue_comments = self._paginate(issue_url, {'per_page': '100'})
        for comment in issue_comments:
            if BOT_MARKER in comment.get('body', ''):
                resp = self.client.delete(f'/repos/{repo_slug}/issues/comments/{comment["id"]}')
                if resp.status_code == 204:
                    deleted += 1
                    logger.info('Deleted issue comment %d on PR #%d', comment['id'], pr_id)
                else:
                    logger.warning('Failed to delete issue comment %d: %d', comment['id'], resp.status_code)

        # Delete PR review comments (inline comments)
        review_url = f'/repos/{repo_slug}/pulls/{pr_id}/comments'
        review_comments = self._paginate(review_url, {'per_page': '100'})
        for comment in review_comments:
            if BOT_MARKER in comment.get('body', ''):
                resp = self.client.delete(f'/repos/{repo_slug}/pulls/comments/{comment["id"]}')
                if resp.status_code == 204:
                    deleted += 1
                    logger.info('Deleted review comment %d on PR #%d', comment['id'], pr_id)
                else:
                    logger.warning('Failed to delete review comment %d: %d', comment['id'], resp.status_code)

        return deleted

    def approve_pr(self, repo_slug: str, pr_id: int) -> None:
        url = f'/repos/{repo_slug}/pulls/{pr_id}/reviews'
        resp = self.client.post(url, json={'event': 'APPROVE'})
        resp.raise_for_status()
        logger.info('Approved PR #%d', pr_id)


def _parse_next_link(link_header: str) -> str | None:
    for part in link_header.split(','):
        if 'rel="next"' in part:
            url = part.split(';')[0].strip().strip('<>')
            return url
    return None
