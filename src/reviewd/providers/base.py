from __future__ import annotations

from abc import ABC, abstractmethod

from reviewd.models import InlineComment, PRInfo, ReviewEvent


class GitProvider(ABC):
    supports_formal_review: bool = False

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

    @abstractmethod
    def submit_review(
        self,
        repo_slug: str,
        pr_id: int,
        body: str,
        event: ReviewEvent,
        inline_comments: list[InlineComment],
        source_commit: str,
    ) -> int | None: ...

    @abstractmethod
    def dismiss_review(
        self,
        repo_slug: str,
        pr_id: int,
        review_id: int,
        message: str,
    ) -> bool: ...

    @abstractmethod
    def get_review_state(self, repo_slug: str, pr_id: int, review_id: int) -> str: ...

    @abstractmethod
    def get_diff_lines(self, repo_slug: str, pr_id: int) -> dict[str, set[int]]: ...
