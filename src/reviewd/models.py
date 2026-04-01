from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Severity(enum.StrEnum):
    CRITICAL = 'critical'
    SUGGESTION = 'suggestion'
    NITPICK = 'nitpick'
    GOOD = 'good'


SEVERITY_ORDER = {
    'good': 0,
    'nitpick': 1,
    'suggestion': 2,
    'critical': 3,
}


@dataclass
class Finding:
    severity: Severity
    category: str
    title: str
    file: str
    line: int | None
    end_line: int | None
    issue: str
    fix: str | None = None


@dataclass
class ReviewResult:
    overview: str
    findings: list[Finding]
    summary: str
    tests_passed: bool | None = None
    approve: bool = False
    approve_reason: str | None = None
    duration_seconds: float | None = None


@dataclass
class PRInfo:
    repo_slug: str
    pr_id: int
    title: str
    author: str
    source_branch: str
    destination_branch: str
    source_commit: str
    url: str
    draft: bool = False


@dataclass
class GithubConfig:
    token: str


@dataclass
class AutoApproveConfig:
    enabled: bool = False
    max_diff_lines: int | None = None
    max_severity: str | None = None
    max_findings: int | None = None
    rules: str | None = None
    show_blocked_reason: bool = True


@dataclass
class ProjectConfig:
    instructions: str | None = None
    test_commands: list[str] = field(default_factory=list)
    inline_comments_for: list[str] = field(default_factory=lambda: ['critical'])
    max_inline_comments: int | None = None
    skip_severities: list[str] = field(default_factory=list)
    show_overview: bool = False
    min_diff_lines: int = 0
    min_diff_lines_update: int = 5
    review_cooldown_minutes: int = 0
    auto_approve: AutoApproveConfig = field(default_factory=AutoApproveConfig)
    critical_task: bool = False
    critical_task_message: str = 'Critical issue found by AI review. Dismiss if false positive.'


class CLI(enum.StrEnum):
    CLAUDE = 'claude'
    GEMINI = 'gemini'
    CODEX = 'codex'


@dataclass
class RepoConfig:
    name: str
    path: str
    provider: str = 'bitbucket'
    repo_slug: str | None = None
    workspace: str | None = None
    github: GithubConfig | None = None
    cli: CLI = CLI.CLAUDE
    model: str | None = None

    @property
    def slug(self) -> str:
        return self.repo_slug or self.name


@dataclass
class GlobalConfig:
    repos: list[RepoConfig]
    bitbucket: dict[str, str] = field(default_factory=dict)
    github: GithubConfig | None = None
    state_db: str = ''
    cli: CLI = CLI.CLAUDE
    model: str | None = None
    cli_args: list[str] = field(default_factory=list)
    cli_defaults: dict[CLI, list[str]] = field(default_factory=dict)
    instructions: str | None = None
    auto_approve: AutoApproveConfig | None = None
    inline_comments_for: list[str] | None = None
    skip_title_patterns: list[str] = field(default_factory=lambda: ['[no-review]', '[wip]', '[no-claudiu]'])
    skip_authors: list[str] = field(default_factory=list)
    poll_interval_seconds: int = 60
    max_concurrent_reviews: int = 4
    review_title: str = "review'd by {cli}"
    footer: str = (
        'Automated review by [reviewd](https://github.com/simion/reviewd){duration}.'
        ' Findings are AI-generated and may not be accurate.'
    )
