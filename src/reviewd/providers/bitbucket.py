from __future__ import annotations

import logging
import re

import httpx

from reviewd.models import PRInfo
from reviewd.providers.base import GitProvider

logger = logging.getLogger(__name__)

BOT_MARKER = '[](reviewd)'
BB_API_BASE = 'https://api.bitbucket.org/2.0'

# Matches "user@domain:token" format for Basic auth (email:token)
_BASIC_AUTH_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+:.+$')


class BitbucketProvider(GitProvider):
    def __init__(self, workspace: str, auth_token: str):
        self.workspace = workspace
        # auth_token can be "email:token" (Basic auth) or a plain OAuth token (Bearer)
        if _BASIC_AUTH_RE.match(auth_token):
            email, token = auth_token.split(':', 1)
            auth = (email, token)
            headers = {'Content-Type': 'application/json'}
        else:
            auth = None
            headers = {'Authorization': f'Bearer {auth_token}', 'Content-Type': 'application/json'}
        self.client = httpx.Client(
            base_url=BB_API_BASE,
            auth=auth,
            headers=headers,
            timeout=30,
        )

    def _paginate(self, url: str, params: dict | None = None) -> list[dict]:
        results = []
        seen_ids: set[int] = set()
        params = params or {}
        page = 1
        while True:
            logger.debug('Paginate %s (page %d)', url, page)
            resp = self.client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            values = data.get('values', [])
            new_values = [v for v in values if v.get('id') not in seen_ids]
            if not new_values:
                logger.debug('No new items on page %d, stopping pagination', page)
                break
            for v in new_values:
                if 'id' in v:
                    seen_ids.add(v['id'])
            results.extend(new_values)
            logger.debug('Got %d items (total %d)', len(new_values), len(results))
            next_url = data.get('next')
            if not next_url:
                break
            url = next_url
            params = {}
            page += 1
        return results

    def _pr_from_data(self, repo_slug: str, data: dict) -> PRInfo:
        return PRInfo(
            repo_slug=repo_slug,
            pr_id=data['id'],
            title=data['title'],
            author=data['author']['display_name'],
            source_branch=data['source']['branch']['name'],
            destination_branch=data['destination']['branch']['name'],
            source_commit=data['source']['commit']['hash'] if data['source'].get('commit') else '',
            url=data['links']['html']['href'],
            draft=data.get('draft', False),
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
        end_line: int | None = None,
        source_commit: str | None = None,
    ) -> int:
        url = f'/repositories/{self.workspace}/{repo_slug}/pullrequests/{pr_id}/comments'
        payload: dict = {'content': {'raw': f'{body}\n\n{BOT_MARKER}'}}

        if file_path is not None:
            inline: dict = {'path': file_path}
            if end_line is not None:
                inline['from'] = line
                inline['to'] = end_line
            elif line is not None:
                inline['to'] = line
            payload['inline'] = inline

        resp = self.client.post(url, json=payload)
        resp.raise_for_status()
        comment_id = resp.json()['id']
        logger.info('Posted comment %d on PR #%d', comment_id, pr_id)
        return comment_id

    def delete_comment(self, repo_slug: str, pr_id: int, comment_id: int) -> bool:
        url = f'/repositories/{self.workspace}/{repo_slug}/pullrequests/{pr_id}/comments/{comment_id}'
        resp = self.client.delete(url)
        if resp.status_code == 204:
            logger.info('Deleted comment %d on PR #%d', comment_id, pr_id)
            return True
        logger.warning('Failed to delete comment %d: status=%d body=%s', comment_id, resp.status_code, resp.text[:200])
        return False

    def approve_pr(self, repo_slug: str, pr_id: int) -> None:
        url = f'/repositories/{self.workspace}/{repo_slug}/pullrequests/{pr_id}/approve'
        resp = self.client.post(url)
        if resp.status_code == 400:
            logger.debug('Cannot approve PR #%d (likely self-approve): %s', pr_id, resp.text[:200])
            return
        if resp.status_code >= 400:
            logger.error(
                'Failed to approve PR #%d: %d %s',
                pr_id,
                resp.status_code,
                resp.text,
            )
            return
        resp.raise_for_status()
        logger.info('Approved PR #%d', pr_id)

    def list_tasks(self, repo_slug: str, pr_id: int) -> list[dict]:
        url = f'/repositories/{self.workspace}/{repo_slug}/pullrequests/{pr_id}/tasks'
        return self._paginate(url)

    def create_task(self, repo_slug: str, pr_id: int, message: str) -> int:
        url = f'/repositories/{self.workspace}/{repo_slug}/pullrequests/{pr_id}/tasks'
        resp = self.client.post(url, json={'content': {'raw': message}})
        resp.raise_for_status()
        task_id = resp.json()['id']
        logger.info('Created task %d on PR #%d', task_id, pr_id)
        return task_id

    def delete_task(self, repo_slug: str, pr_id: int, task_id: int) -> bool:
        url = f'/repositories/{self.workspace}/{repo_slug}/pullrequests/{pr_id}/tasks/{task_id}'
        resp = self.client.delete(url)
        if resp.status_code == 204:
            logger.info('Deleted task %d on PR #%d', task_id, pr_id)
            return True
        logger.warning('Failed to delete task %d: status=%d', task_id, resp.status_code)
        return False
