from __future__ import annotations

import logging
import time

import httpx

from reviewd.models import GithubConfig, PRInfo
from reviewd.providers.base import GitProvider

logger = logging.getLogger(__name__)

BOT_MARKER = '[](reviewd)'
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

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        max_retries = 3
        for attempt in range(max_retries + 1):
            resp = self.client.request(method, url, **kwargs)
            if resp.status_code != 429 or attempt == max_retries:
                resp.raise_for_status()
                return resp
            retry_after = int(resp.headers.get('Retry-After', 2**attempt))
            logger.warning('Rate limited (429), retrying in %ds (attempt %d/%d)', retry_after, attempt + 1, max_retries)
            time.sleep(retry_after)
        return resp  # unreachable

    def _request_raw(self, method: str, url: str, **kwargs) -> httpx.Response:
        max_retries = 3
        for attempt in range(max_retries + 1):
            resp = self.client.request(method, url, **kwargs)
            if resp.status_code != 429 or attempt == max_retries:
                return resp
            retry_after = int(resp.headers.get('Retry-After', 2**attempt))
            logger.warning('Rate limited (429), retrying in %ds (attempt %d/%d)', retry_after, attempt + 1, max_retries)
            time.sleep(retry_after)
        return resp  # unreachable

    def _paginate(self, url: str, params: dict | None = None) -> list[dict]:
        results = []
        params = params or {}
        while True:
            resp = self._request('GET', url, params=params)
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
            draft=data.get('draft', False),
        )

    def list_open_prs(self, repo_slug: str) -> list[PRInfo]:
        url = f'/repos/{repo_slug}/pulls'
        items = self._paginate(url, {'state': 'open', 'per_page': '100'})
        return [self._pr_from_data(repo_slug, item) for item in items]

    def get_pr(self, repo_slug: str, pr_id: int) -> PRInfo:
        url = f'/repos/{repo_slug}/pulls/{pr_id}'
        resp = self._request('GET', url)
        return self._pr_from_data(repo_slug, resp.json())

    def post_comment(
        self,
        repo_slug: str,
        pr_id: int,
        body: str,
        *,
        file_path: str | None = None,
        line: int | None = None,
        end_line: int | None = None,
        source_commit: str | None = None,
    ) -> int:
        marked_body = f'{body}\n\n{BOT_MARKER}'

        if file_path is not None:
            commit_id = source_commit
            if not commit_id:
                pr_resp = self._request('GET', f'/repos/{repo_slug}/pulls/{pr_id}')
                commit_id = pr_resp.json()['head']['sha']

            url = f'/repos/{repo_slug}/pulls/{pr_id}/comments'
            payload: dict = {
                'body': marked_body,
                'commit_id': commit_id,
                'path': file_path,
                'side': 'RIGHT',
            }
            if end_line is not None:
                payload['start_line'] = line
                payload['line'] = end_line
                payload['start_side'] = 'RIGHT'
            elif line is not None:
                payload['line'] = line
        else:
            url = f'/repos/{repo_slug}/issues/{pr_id}/comments'
            payload = {'body': marked_body}

        resp = self._request('POST', url, json=payload)
        comment_id = resp.json()['id']
        logger.info('Posted comment %d on PR #%d', comment_id, pr_id)
        return comment_id

    def delete_comment(self, repo_slug: str, pr_id: int, comment_id: int) -> bool:
        # Try issue comment first, then review comment
        resp = self._request_raw('DELETE', f'/repos/{repo_slug}/issues/comments/{comment_id}')
        if resp.status_code == 204:
            logger.info('Deleted issue comment %d on PR #%d', comment_id, pr_id)
            return True
        resp = self._request_raw('DELETE', f'/repos/{repo_slug}/pulls/comments/{comment_id}')
        if resp.status_code == 204:
            logger.info('Deleted review comment %d on PR #%d', comment_id, pr_id)
            return True
        logger.warning('Failed to delete comment %d on PR #%d: %d', comment_id, pr_id, resp.status_code)
        return False

    def approve_pr(self, repo_slug: str, pr_id: int) -> bool:
        url = f'/repos/{repo_slug}/pulls/{pr_id}/reviews'
        resp = self._request_raw('POST', url, json={'event': 'APPROVE'})
        if resp.status_code == 422:
            logger.warning('Cannot approve PR #%d (likely self-approve): %s', pr_id, resp.text[:200])
            return False
        resp.raise_for_status()
        logger.info('Approved PR #%d', pr_id)
        return True


def _parse_next_link(link_header: str) -> str | None:
    for part in link_header.split(','):
        if 'rel="next"' in part:
            url = part.split(';')[0].strip().strip('<>')
            return url
    return None
