from __future__ import annotations

import logging

import httpx

from nea_claudiu.models import BitbucketConfig, PRInfo
from nea_claudiu.providers.base import GitProvider

logger = logging.getLogger(__name__)

BOT_MARKER = '[](nea-claudiu)'
BB_API_BASE = 'https://api.bitbucket.org/2.0'


class BitbucketProvider(GitProvider):
    def __init__(self, config: BitbucketConfig):
        self.workspace = config.workspace
        self.client = httpx.Client(
            base_url=BB_API_BASE,
            headers={
                'Authorization': f'Bearer {config.auth_token}',
                'Content-Type': 'application/json',
            },
            timeout=30,
        )

    def _paginate(self, url: str, params: dict | None = None) -> list[dict]:
        results = []
        params = params or {}
        while True:
            resp = self.client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get('values', []))
            next_url = data.get('next')
            if not next_url:
                break
            url = next_url
            params = {}
        return results

    def _pr_from_data(self, repo_slug: str, data: dict) -> PRInfo:
        return PRInfo(
            repo_slug=repo_slug,
            pr_id=data['id'],
            title=data['title'],
            author=data['author']['display_name'],
            source_branch=data['source']['branch']['name'],
            destination_branch=data['destination']['branch']['name'],
            source_commit=data['source']['commit']['hash'],
            url=data['links']['html']['href'],
        )

    def list_open_prs(self, repo_slug: str) -> list[PRInfo]:
        url = f'/repositories/{self.workspace}/{repo_slug}/pullrequests'
        items = self._paginate(url, {'state': 'OPEN'})
        return [self._pr_from_data(repo_slug, item) for item in items]

    def get_pr(self, repo_slug: str, pr_id: int) -> PRInfo:
        url = f'/repositories/{self.workspace}/{repo_slug}/pullrequests/{pr_id}'
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
        url = f'/repositories/{self.workspace}/{repo_slug}/pullrequests/{pr_id}/comments'
        payload: dict = {'content': {'raw': f'{body}\n\n{BOT_MARKER}'}}

        if file_path is not None:
            inline: dict = {'path': file_path}
            if line is not None:
                inline['to'] = line
            payload['inline'] = inline

        resp = self.client.post(url, json=payload)
        resp.raise_for_status()
        comment_id = resp.json()['id']
        logger.info('Posted comment %d on PR #%d', comment_id, pr_id)
        return comment_id

    def delete_bot_comments(self, repo_slug: str, pr_id: int) -> int:
        url = f'/repositories/{self.workspace}/{repo_slug}/pullrequests/{pr_id}/comments'
        comments = self._paginate(url)
        deleted = 0
        for comment in comments:
            raw = comment.get('content', {}).get('raw', '')
            if BOT_MARKER in raw:
                comment_id = comment['id']
                delete_url = f'{url}/{comment_id}'
                resp = self.client.delete(delete_url)
                if resp.status_code == 204:
                    deleted += 1
                    logger.info('Deleted bot comment %d on PR #%d', comment_id, pr_id)
                else:
                    logger.warning('Failed to delete comment %d: %d', comment_id, resp.status_code)
        return deleted

    def approve_pr(self, repo_slug: str, pr_id: int) -> None:
        url = f'/repositories/{self.workspace}/{repo_slug}/pullrequests/{pr_id}/approve'
        resp = self.client.post(url)
        resp.raise_for_status()
        logger.info('Approved PR #%d', pr_id)
