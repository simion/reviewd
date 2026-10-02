from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from reviewd.models import GitlabConfig, PRInfo
from reviewd.providers.base import GitProvider, parse_next_link

logger = logging.getLogger(__name__)

BOT_MARKER = '[](reviewd)'

HUNK_HEADER = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


SUGGESTION_BLOCK = re.compile(r'\n*```suggestion\n.*?\n```', re.S)


@dataclass
class _FileDiff:
    old_path: str
    # new line -> old line for every line shown in the diff; None means the line was added
    lines: dict[int, int | None] = field(default_factory=dict)


def _parse_diff(old_path: str, diff: str) -> _FileDiff:
    parsed = _FileDiff(old_path=old_path)
    old = new = 0
    for text in diff.splitlines():
        header = HUNK_HEADER.match(text)
        if header:
            old, new = int(header.group(1)), int(header.group(3))
        elif text.startswith('+'):
            parsed.lines[new] = None
            new += 1
        elif text.startswith('-'):
            old += 1
        elif text.startswith(' ') or text == '':
            parsed.lines[new] = old
            old += 1
            new += 1
    return parsed


def _line_label(line: int | None, end_line: int | None) -> str:
    if line is not None and end_line is not None and end_line != line:
        return f'Lines {line}-{end_line}'
    return f'Line {end_line if end_line is not None else line}'


class GitlabProvider(GitProvider):
    def __init__(self, config: GitlabConfig):
        self.client = httpx.Client(
            base_url=f'{config.url.rstrip("/")}/api/v4',
            headers={'Authorization': f'Bearer {config.token}'},
            timeout=30,
        )
        self._diff_refs: dict[tuple[str, int], dict] = {}
        self._file_diffs: dict[tuple[str, int, str], dict[str, _FileDiff]] = {}

    def _paginate(self, url: str, params: dict | None = None) -> list[dict]:
        results = []
        while True:
            resp = self._request('GET', url, params=params)
            results.extend(resp.json())
            next_url = parse_next_link(resp.headers.get('link', ''))
            if not next_url:
                break
            # The next link carries the full query; params={} would make httpx strip it
            url = next_url
            params = None
        return results

    @staticmethod
    def _mr_url(repo_slug: str, pr_id: int | None = None) -> str:
        url = f'/projects/{quote(repo_slug, safe="")}/merge_requests'
        return url if pr_id is None else f'{url}/{pr_id}'

    def _pr_from_data(self, repo_slug: str, data: dict) -> PRInfo:
        if data.get('diff_refs'):
            self._diff_refs[(repo_slug, data['iid'])] = data['diff_refs']
        return PRInfo(
            repo_slug=repo_slug,
            pr_id=data['iid'],
            title=data['title'],
            author=data['author']['username'],
            source_branch=data['source_branch'],
            destination_branch=data['target_branch'],
            source_commit=data['sha'] or '',
            url=data['web_url'],
            draft=data.get('draft', data.get('work_in_progress', False)),
        )

    def _get_diff_refs(self, repo_slug: str, pr_id: int, source_commit: str | None) -> dict:
        refs = self._diff_refs.get((repo_slug, pr_id))
        if refs is None or (source_commit and refs['head_sha'] != source_commit):
            self.get_pr(repo_slug, pr_id)
            refs = self._diff_refs.get((repo_slug, pr_id))
            if refs is None:
                raise RuntimeError(f'MR !{pr_id} has no diff_refs yet, cannot position inline comment')
        return refs

    def _get_file_diffs(self, repo_slug: str, pr_id: int, head_sha: str) -> dict[str, _FileDiff]:
        key = (repo_slug, pr_id, head_sha)
        if key not in self._file_diffs:
            files = self._paginate(f'{self._mr_url(repo_slug, pr_id)}/diffs', {'per_page': '100'})
            # Collapsed or too-large files come back with an empty diff; leave them out rather than guess
            self._file_diffs[key] = {f['new_path']: _parse_diff(f['old_path'], f['diff']) for f in files if f['diff']}
        return self._file_diffs[key]

    @staticmethod
    def _line_position(file_diff: _FileDiff | None, file_path: str, line: int) -> dict | None:
        if file_diff is None or line not in file_diff.lines:
            return None
        position = {'position_type': 'text', 'old_path': file_diff.old_path, 'new_path': file_path, 'new_line': line}
        old_line = file_diff.lines[line]
        if old_line is not None:
            position['old_line'] = old_line
        return position

    def list_open_prs(self, repo_slug: str) -> list[PRInfo]:
        items = self._paginate(self._mr_url(repo_slug), {'state': 'opened', 'per_page': '100'})
        return [self._pr_from_data(repo_slug, item) for item in items]

    def get_pr(self, repo_slug: str, pr_id: int) -> PRInfo:
        resp = self._request('GET', self._mr_url(repo_slug, pr_id))
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
        mr_url = self._mr_url(repo_slug, pr_id)

        if file_path is None:
            resp = self._request('POST', f'{mr_url}/notes', json={'body': marked_body})
            comment_id = resp.json()['id']
        else:
            refs = self._get_diff_refs(repo_slug, pr_id, source_commit)
            position: dict = {
                'base_sha': refs['base_sha'],
                'start_sha': refs['start_sha'],
                'head_sha': refs['head_sha'],
            }
            file_diff = self._get_file_diffs(repo_slug, pr_id, refs['head_sha']).get(file_path)
            target_line = end_line if end_line is not None else line
            line_position = None if target_line is None else self._line_position(file_diff, file_path, target_line)
            if line_position:
                position |= line_position
            else:
                if target_line is not None:
                    # A guessed anchor can land on the wrong line; a file-level comment naming the line can't
                    logger.info('%s:%d is not in the MR diff, posting a file-level comment', file_path, target_line)
                    label = _line_label(line, end_line)
                    marked_body = f'{label}: {SUGGESTION_BLOCK.sub("", body)}\n\n{BOT_MARKER}'
                old_path = file_diff.old_path if file_diff else file_path
                position |= {'position_type': 'file', 'old_path': old_path, 'new_path': file_path}
            resp = self._request('POST', f'{mr_url}/discussions', json={'body': marked_body, 'position': position})
            comment_id = resp.json()['notes'][0]['id']

        logger.info('Posted comment %d on MR !%d', comment_id, pr_id)
        return comment_id

    def delete_comment(self, repo_slug: str, pr_id: int, comment_id: int) -> bool:
        resp = self._request_raw('DELETE', f'{self._mr_url(repo_slug, pr_id)}/notes/{comment_id}')
        if resp.status_code == 204:
            logger.info('Deleted comment %d on MR !%d', comment_id, pr_id)
            return True
        logger.warning('Failed to delete comment %d on MR !%d: %d', comment_id, pr_id, resp.status_code)
        return False

    def approve_pr(self, repo_slug: str, pr_id: int) -> bool:
        resp = self._request_raw('POST', f'{self._mr_url(repo_slug, pr_id)}/approve')
        # GitLab also answers 401 when the user may not approve, so only a token rejected by /user is an auth failure
        token_rejected = resp.status_code == 401 and self._request_raw('GET', '/user').status_code == 401
        if token_rejected or (resp.status_code == 403 and 'insufficient_scope' in resp.text):
            resp.raise_for_status()
        if resp.status_code in (401, 403, 405):
            logger.warning('Cannot approve MR !%d (already approved or self-approve): %s', pr_id, resp.text[:200])
            return False
        resp.raise_for_status()
        logger.info('Approved MR !%d', pr_id)
        return True
