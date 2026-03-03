from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Severity(enum.StrEnum):
    CRITICAL = 'critical'
    SUGGESTION = 'suggestion'
    NITPICK = 'nitpick'
    GOOD = 'good'


@dataclass
class Finding:
    severity: Severity
    category: str
    title: str
    file: str
    line: int | None
    issue: str
    fix: str | None = None


@dataclass
class ReviewResult:
    overview: str
    findings: list[Finding]
    summary: str
    tests_passed: bool | None = None


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


@dataclass
class BitbucketConfig:
    workspace: str
    auth_token: str
    poll_interval_seconds: int = 60


@dataclass
class ProjectConfig:
    instructions: str | None = None
    test_commands: list[str] = field(default_factory=list)
    skip_title_patterns: list[str] = field(default_factory=list)
    skip_authors: list[str] = field(default_factory=list)
    inline_comments_for: list[str] = field(default_factory=lambda: ['critical'])
    approve_if_no_critical: bool = False


class AICli(enum.StrEnum):
    CLAUDE = 'claude'
    GEMINI = 'gemini'


@dataclass
class RepoConfig:
    name: str
    path: str
    provider: str = 'bitbucket'
    bitbucket: BitbucketConfig | None = None
    ai_cli: AICli = AICli.CLAUDE


@dataclass
class GlobalConfig:
    bitbucket: BitbucketConfig
    repos: list[RepoConfig]
    state_db: str = '~/.local/share/nea-claudiu/state.db'
    ai_cli: AICli = AICli.CLAUDE
    instructions: str | None = None
