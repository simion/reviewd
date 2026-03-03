from __future__ import annotations

import logging

from nea_claudiu.models import Finding, GlobalConfig, PRInfo, ProjectConfig, ReviewResult, Severity
from nea_claudiu.providers.base import GitProvider
from nea_claudiu.state import StateDB

logger = logging.getLogger(__name__)

SEVERITY_EMOJI = {
    Severity.CRITICAL: '\U0001f534',
    Severity.SUGGESTION: '\U0001f7e1',
    Severity.NITPICK: '\U0001f535',
    Severity.GOOD: '\U0001f7e2',
}


def _format_finding_summary(finding: Finding) -> str:
    loc = ''
    if finding.file:
        loc = f' — `{finding.file}`'
        if finding.line:
            loc += f' (line {finding.line})'
    return f'- **{finding.title}**{loc}\n  {finding.issue}'


def _format_inline_comment(finding: Finding) -> str:
    emoji = SEVERITY_EMOJI.get(finding.severity, '')
    parts = [f'{emoji} **{finding.title}**', finding.issue]
    if finding.fix:
        parts.append(f'```suggestion\n{finding.fix}\n```')
    return '\n\n'.join(parts)


def _format_summary_comment(
    result: ReviewResult,
    inline_ids: set[int],
    global_config: GlobalConfig,
) -> str:
    lines = [f'## Code Review by {global_config.reviewer_name}', '', result.overview, '']

    if result.tests_passed is not None:
        status = 'passed' if result.tests_passed else 'FAILED'
        lines.append(f'**Tests:** {status}')
        lines.append('')

    # Exclude suggestions that have inline comments — they only appear inline
    summary_findings = [
        f for f in result.findings
        if not (id(f) in inline_ids and f.severity == Severity.SUGGESTION)
    ]

    grouped: dict[Severity, list[Finding]] = {}
    for f in summary_findings:
        grouped.setdefault(f.severity, []).append(f)

    for severity in [Severity.CRITICAL, Severity.SUGGESTION, Severity.NITPICK, Severity.GOOD]:
        findings = grouped.get(severity, [])
        if not findings:
            continue
        emoji = SEVERITY_EMOJI[severity]
        lines.append(f'### {emoji} {severity.value.capitalize()} ({len(findings)})')
        lines.append('')
        for finding in findings:
            summary = _format_finding_summary(finding)
            if id(finding) in inline_ids:
                summary += ' *(inline comment)*'
            lines.append(summary)
        lines.append('')

    if result.summary:
        lines.append('---')
        lines.append(f'**Bottom line:** {result.summary}')

    lines.append('')
    lines.append('---')
    lines.append(f'*{global_config.footer}*')

    return '\n'.join(lines)


def post_review(
    provider: GitProvider,
    state_db: StateDB,
    pr: PRInfo,
    result: ReviewResult,
    project_config: ProjectConfig,
    global_config: GlobalConfig,
    dry_run: bool = False,
):
    inline_severities = {s for s in project_config.inline_comments_for}
    inline_findings = [
        f for f in result.findings
        if f.severity.value in inline_severities and f.file and f.line
    ]
    inline_ids = {id(f) for f in inline_findings}

    if dry_run:
        _print_dry_run(result, inline_findings, inline_ids, global_config)
        return

    deleted = provider.delete_bot_comments(pr.repo_slug, pr.pr_id)
    if deleted:
        logger.info('Deleted %d old bot comments', deleted)

    for finding in inline_findings:
        body = _format_inline_comment(finding)
        comment_id = provider.post_comment(
            pr.repo_slug, pr.pr_id, body,
            file_path=finding.file, line=finding.line,
            source_commit=pr.source_commit,
        )
        state_db.record_comment(pr.repo_slug, pr.pr_id, comment_id)

    summary_body = _format_summary_comment(result, inline_ids, global_config)
    comment_id = provider.post_comment(pr.repo_slug, pr.pr_id, summary_body)
    state_db.record_comment(pr.repo_slug, pr.pr_id, comment_id)

    if project_config.approve_if_no_critical:
        has_critical = any(f.severity == Severity.CRITICAL for f in result.findings)
        if not has_critical:
            provider.approve_pr(pr.repo_slug, pr.pr_id)
            logger.info('Auto-approved PR #%d (no critical findings)', pr.pr_id)


def _print_dry_run(
    result: ReviewResult,
    inline_findings: list[Finding],
    inline_ids: set[int],
    global_config: GlobalConfig,
):
    print('\n' + '=' * 60)
    print('DRY RUN — would post the following comments:')
    print('=' * 60)

    if inline_findings:
        print(f'\n--- Inline Comments ({len(inline_findings)}) ---')
        for f in inline_findings:
            print(f'\n  File: {f.file}:{f.line}')
            print(f'  {_format_inline_comment(f)}')

    print('\n--- Summary Comment ---')
    print(_format_summary_comment(result, inline_ids, global_config))
    print('=' * 60 + '\n')
