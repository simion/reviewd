from __future__ import annotations

import logging

from reviewd.models import (
    CLI,
    SEVERITY_ORDER,
    AutoApproveConfig,
    Finding,
    GlobalConfig,
    PRInfo,
    ProjectConfig,
    ReviewResult,
    Severity,
)
from reviewd.providers.base import GitProvider
from reviewd.state import StateDB

logger = logging.getLogger(__name__)

TASK_MARKER = '[reviewd]'

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


# TODO: support multi-line suggestions (end_line) — needs correct line range in provider API calls
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
    project_config: ProjectConfig,
    cli: CLI = CLI.CLAUDE,
    approved: bool = False,
) -> str:
    cli_name = cli.value.capitalize()
    title = global_config.review_title.replace('{cli}', cli_name)
    lines = [f'## {title}', '']
    if project_config.show_overview and result.overview:
        lines.extend([result.overview, ''])

    if result.tests_passed is not None:
        status = 'passed' if result.tests_passed else 'FAILED'
        lines.append(f'**Tests:** {status}')
        lines.append('')

    # Findings with inline comments appear only inline, not in the summary
    summary_findings = [f for f in result.findings if id(f) not in inline_ids]

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
            lines.append(_format_finding_summary(finding))
        lines.append('')

    if result.summary:
        lines.append(f'**Bottom line:** {result.summary}')
        lines.append('')

    if approved and result.approve_reason:
        lines.append(f'**Auto-approve rationale:** {result.approve_reason}')
        lines.append('')

    lines.append(f'*{global_config.footer}*')
    lines.append('*Replies to this comment are not monitored.*')

    return '\n'.join(lines)


def _sync_critical_task(provider, pr: PRInfo, result: ReviewResult, project_config: ProjectConfig):
    try:
        tasks = provider.list_tasks(pr.repo_slug, pr.pr_id)
        for task in tasks:
            if TASK_MARKER in task.get('content', {}).get('raw', ''):
                provider.delete_task(pr.repo_slug, pr.pr_id, task['id'])
        has_critical = any(f.severity == Severity.CRITICAL for f in result.findings)
        if has_critical:
            message = f'{TASK_MARKER} {project_config.critical_task_message}'
            provider.create_task(pr.repo_slug, pr.pr_id, message)
    except Exception:
        logger.exception('Failed to sync critical task on PR #%d', pr.pr_id)


def _check_auto_approve_gates(
    aa: AutoApproveConfig,
    result: ReviewResult,
    diff_lines: int | None,
) -> str | None:
    if aa.max_diff_lines is not None and diff_lines is not None and diff_lines > aa.max_diff_lines:
        return f'diff too large ({diff_lines} > {aa.max_diff_lines})'

    if aa.max_findings is not None:
        issue_count = sum(1 for f in result.findings if f.severity != Severity.GOOD)
        if issue_count > aa.max_findings:
            return f'too many findings ({issue_count} > {aa.max_findings})'

    if aa.max_severity is not None:
        max_allowed = SEVERITY_ORDER.get(aa.max_severity, 3)
        for f in result.findings:
            f_order = SEVERITY_ORDER.get(f.severity.value, 3)
            if f_order > max_allowed:
                return f'finding severity {f.severity.value} exceeds max {aa.max_severity}'

    if not result.approve:
        return 'AI did not approve'

    return None


def post_review(
    provider: GitProvider,
    state_db: StateDB,
    pr: PRInfo,
    result: ReviewResult,
    project_config: ProjectConfig,
    global_config: GlobalConfig,
    cli: CLI = CLI.CLAUDE,
    dry_run: bool = False,
    diff_lines: int | None = None,
):
    # Deduplicate findings by file + line + title
    seen: set[tuple] = set()
    unique_findings = []
    for f in result.findings:
        key = (f.file, f.line, f.title)
        if key not in seen:
            seen.add(key)
            unique_findings.append(f)
        else:
            logger.debug('Skipping duplicate finding: %s:%s %s', f.file, f.line, f.title)
    # Filter out skipped severities
    skip = {s for s in project_config.skip_severities}
    if skip:
        unique_findings = [f for f in unique_findings if f.severity.value not in skip]
        logger.info('Filtered out %s severities, %d findings remain', skip, len(unique_findings))

    result = ReviewResult(
        overview=result.overview,
        findings=unique_findings,
        summary=result.summary,
        tests_passed=result.tests_passed,
        approve=result.approve,
        approve_reason=result.approve_reason,
    )

    inline_severities = {s for s in project_config.inline_comments_for}
    inline_findings = [f for f in result.findings if f.severity.value in inline_severities and f.file and f.line]

    max_inline = project_config.max_inline_comments
    if max_inline is not None and len(inline_findings) > max_inline:
        logger.info(
            'Inline comments (%d) exceed max (%d), skipping all inline',
            len(inline_findings),
            max_inline,
        )
        inline_findings = []

    inline_ids = {id(f) for f in inline_findings}

    if dry_run:
        _print_dry_run(
            result,
            inline_findings,
            inline_ids,
            global_config,
            project_config,
            cli,
            diff_lines=diff_lines,
        )
        return

    logger.info('Posting review: %d inline + summary comment', len(inline_findings))

    old_comment_ids = state_db.get_comment_ids(pr.repo_slug, pr.pr_id)
    if old_comment_ids:
        logger.info('Deleting %d old comments on PR #%d', len(old_comment_ids), pr.pr_id)
        deleted = 0
        for cid in old_comment_ids:
            if provider.delete_comment(pr.repo_slug, pr.pr_id, cid):
                deleted += 1
        state_db.delete_comments(pr.repo_slug, pr.pr_id)
        logger.info('Deleted %d/%d old comments', deleted, len(old_comment_ids))

    for i, finding in enumerate(inline_findings, 1):
        logger.info('Posting inline comment %d/%d: %s:%s', i, len(inline_findings), finding.file, finding.line)
        body = _format_inline_comment(finding)
        try:
            comment_id = provider.post_comment(
                pr.repo_slug,
                pr.pr_id,
                body,
                file_path=finding.file,
                line=finding.line,
                source_commit=pr.source_commit,
            )
            state_db.record_comment(pr.repo_slug, pr.pr_id, comment_id)
        except Exception:
            logger.exception('Failed to post inline comment on %s:%s, skipping', finding.file, finding.line)

    aa = project_config.auto_approve
    approved = False
    if aa.enabled:
        blocked = _check_auto_approve_gates(aa, result, diff_lines)
        if blocked:
            logger.info('Auto-approve blocked for PR #%d: %s', pr.pr_id, blocked)
        else:
            approved = True

    logger.info('Posting summary comment')
    summary_body = _format_summary_comment(
        result, inline_ids, global_config, project_config, cli, approved=approved
    )
    comment_id = provider.post_comment(pr.repo_slug, pr.pr_id, summary_body)
    state_db.record_comment(pr.repo_slug, pr.pr_id, comment_id)

    if project_config.critical_task and hasattr(provider, 'list_tasks'):
        _sync_critical_task(provider, pr, result, project_config)

    if approved:
        provider.approve_pr(pr.repo_slug, pr.pr_id)
        logger.info('Auto-approved PR #%d', pr.pr_id)


def _print_dry_run(
    result: ReviewResult,
    inline_findings: list[Finding],
    inline_ids: set[int],
    global_config: GlobalConfig,
    project_config: ProjectConfig,
    cli: CLI = CLI.CLAUDE,
    diff_lines: int | None = None,
):
    print('\n' + '=' * 60)
    print('DRY RUN — would post the following comments:')
    print('=' * 60)

    if inline_findings:
        print(f'\n--- Inline Comments ({len(inline_findings)}) ---')
        for f in inline_findings:
            print(f'\n  File: {f.file}:{f.line}')
            print(f'  {_format_inline_comment(f)}')

    aa = project_config.auto_approve
    approved = False
    if aa.enabled:
        blocked = _check_auto_approve_gates(aa, result, diff_lines)
        if blocked:
            print(f'\n--- Auto-Approve: BLOCKED ({blocked}) ---')
        else:
            approved = True

    print('\n--- Summary Comment ---')
    print(
        _format_summary_comment(result, inline_ids, global_config, project_config, cli, approved=approved)
    )

    if aa.enabled and approved:
        print('\n--- Auto-Approve: WOULD APPROVE ---')

    print('=' * 60 + '\n')
