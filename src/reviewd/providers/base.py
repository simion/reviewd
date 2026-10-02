from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

import httpx

from reviewd.models import PRInfo

logger = logging.getLogger(__name__)


def parse_next_link(link_header: str) -> str | None:
    for part in link_header.split(','):
        if 'rel="next"' in part:
            url = part.split(';')[0].strip().strip('<>')
            return url
    return None


class GitProvider(ABC):
    client: httpx.Client

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
        return resp  # unreachable, but keeps type checkers happy

    def _request_raw(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Like _request but without raise_for_status — caller handles status codes."""
        max_retries = 3
        for attempt in range(max_retries + 1):
            resp = self.client.request(method, url, **kwargs)
            if resp.status_code != 429 or attempt == max_retries:
                return resp
            retry_after = int(resp.headers.get('Retry-After', 2**attempt))
            logger.warning('Rate limited (429), retrying in %ds (attempt %d/%d)', retry_after, attempt + 1, max_retries)
            time.sleep(retry_after)
        return resp  # unreachable

    @abstractmethod
    def list_open_prs(self, repo_slug: str) -> list[PRInfo]: ...

    @abstractmethod
    def get_pr(self, repo_slug: str, pr_id: int) -> PRInfo: ...

    @abstractmethod
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
    ) -> int: ...

    @abstractmethod
    def delete_comment(self, repo_slug: str, pr_id: int, comment_id: int) -> bool: ...

    @abstractmethod
    def approve_pr(self, repo_slug: str, pr_id: int) -> bool: ...
