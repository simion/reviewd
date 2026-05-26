from __future__ import annotations

import logging

import httpx

from reviewd.config import effective_formal_review
from reviewd.models import (
    CLI,
    SEVERITY_ORDER,
    AutoApproveConfig,
    Finding,
    GlobalConfig,
    InlineComment,
    PRInfo,
    ProjectConfig,
    RepoConfig,
    ReviewEvent,
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


_MAX_TALLY_DOTS = 3


def _format_inline_tally(inline_findings: list[Finding]) -> str:
    """Compact emoji tally of inline findings, e.g. '🔴🔴 🟡🟡🟡+2 — posted as inline comments'."""
    grouped: dict[Severity, int] = {}
    for f in inline_findings:
        grouped[f.severity] = grouped.get(f.severity, 0) + 1

    parts = []
    for severity in [Severity.CRITICAL, Severity.SUGGESTION, Severity.NITPICK]:
        count = grouped.get(severity, 0)
        if count == 0:
            continue
        emoji = SEVERITY_EMOJI[severity]
        shown = min(count, _MAX_TALLY_DOTS)
        part = emoji * shown
        if count > _MAX_TALLY_DOTS:
            part += f'+{count - _MAX_TALLY_DOTS}'
        parts.append(part)

    if not parts:
        return ''
    return ' '.join(parts) + ' — posted as inline comments'


def _format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f'{m}m {s}s'
    return f'{s}s'


def _format_summary_comment(
    result: ReviewResult,
    inline_ids: set[int],
    global_config: GlobalConfig,
    project_config: ProjectConfig,
    cli: CLI = CLI.CLAUDE,
    model: str | None = None,
    approved: bool = False,
    approve_blocked_reason: str | None = None,
) -> str:
    cli_name = cli.value.capitalize()
    title = global_config.review_title.replace('{cli}', cli_name)
    model_label = model or cli_name
    lines = [f'## {title}', '']

    # Tally of findings posted as inline comments (not shown in summary)
    inline_findings = [f for f in result.findings if id(f) in inline_ids]
    if inline_findings:
        lines.append(_format_inline_tally(inline_findings))
        lines.append('')

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

    if approve_blocked_reason:
        lines.append(f'**Auto-approve blocked:** AI recommended approval, but {approve_blocked_reason}.')
        lines.append('')

    duration_str = f' in {_format_duration(result.duration_seconds)}' if result.duration_seconds else ''
    footer = global_config.footer.replace('{duration}', duration_str).replace('{model}', model_label)
    lines.append(f'*{footer}*')
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
    """Returns a blocking reason string, or None if auto-approve should proceed."""
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


def _resolve_auto_approve(
    aa: AutoApproveConfig,
    result: ReviewResult,
    diff_lines: int | None,
) -> tuple[bool, str | None]:
    """Returns (approved, blocked_reason_to_show).

    blocked_reason_to_show is set only when the AI recommended approval
    but a config gate prevented it and show_blocked_reason is enabled.
    """
    blocked = _check_auto_approve_gates(aa, result, diff_lines)
    if not blocked:
        return True, None

    # AI wanted to approve but a gate stopped it
    show_reason = aa.show_blocked_reason and result.approve and blocked != 'AI did not approve'
    return False, blocked if show_reason else None


def _select_review_event(
    result: ReviewResult,
    project_config: ProjectConfig,
    diff_lines: int | None,
) -> tuple[ReviewEvent, bool, str | None]:
    """Returns (event, approved, approve_blocked_reason). Order: APPROVE > REQUEST_CHANGES > COMMENT."""
    aa = project_config.auto_approve
    approved = False
    approve_blocked_reason = None
    if aa.enabled:
        approved, approve_blocked_reason = _resolve_auto_approve(aa, result, diff_lines)

    if approved:
        return ReviewEvent.APPROVE, True, None

    has_critical = any(f.severity == Severity.CRITICAL for f in result.findings)
    if has_critical:
        return ReviewEvent.REQUEST_CHANGES, False, approve_blocked_reason

    return ReviewEvent.COMMENT, False, approve_blocked_reason


def _dismiss_prior_reviews(provider: GitProvider, state_db: StateDB, pr: PRInfo):
    prior_ids = state_db.get_review_ids(pr.repo_slug, pr.pr_id)
    if not prior_ids:
        return
    logger.info('Processing %d prior reviews on PR #%d', len(prior_ids), pr.pr_id)
    for review_id in prior_ids:
        try:
            state = provider.get_review_state(pr.repo_slug, pr.pr_id, review_id)
        except httpx.HTTPError as e:
            logger.warning('Could not fetch prior review %d state: %s — removing from state', review_id, e)
            state_db.delete_review(pr.repo_slug, pr.pr_id, review_id)
            continue

        if state == 'CHANGES_REQUESTED':
            if provider.dismiss_review(
                pr.repo_slug,
                pr.pr_id,
                review_id,
                'Superseded by newer reviewd review',
            ):
                state_db.delete_review(pr.repo_slug, pr.pr_id, review_id)
            # else: leave in state DB so next pass retries the dismissal
        else:
            state_db.delete_review(pr.repo_slug, pr.pr_id, review_id)


def _filter_inline_findings_by_diff(
    inline_findings: list[Finding],
    provider: GitProvider,
    pr: PRInfo,
) -> list[Finding]:
    if not inline_findings:
        return inline_findings
    try:
        diff_lines = provider.get_diff_lines(pr.repo_slug, pr.pr_id)
    except NotImplementedError:
        return inline_findings
    except httpx.HTTPError as e:
        logger.warning('Could not fetch diff lines for pre-filter: %s — skipping filter', e)
        return inline_findings

    kept = []
    for f in inline_findings:
        if f.file in diff_lines and f.line in diff_lines[f.file]:
            kept.append(f)
        else:
            logger.info('Dropping hallucinated inline finding %s:%s (not in diff)', f.file, f.line)
    return kept


def post_review(
    provider: GitProvider,
    state_db: StateDB,
    pr: PRInfo,
    result: ReviewResult,
    repo_config: RepoConfig,
    project_config: ProjectConfig,
    global_config: GlobalConfig,
    cli: CLI = CLI.CLAUDE,
    model: str | None = None,
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
        duration_seconds=result.duration_seconds,
    )

    inline_severities = {s for s in project_config.inline_comments_for}
    inline_findings = [f for f in result.findings if f.severity.value in inline_severities and f.file and f.line]

    inline_findings = _filter_inline_findings_by_diff(inline_findings, provider, pr)

    max_inline = project_config.max_inline_comments
    if max_inline is not None and len(inline_findings) > max_inline:
        logger.info(
            'Inline comments (%d) exceed max (%d), skipping all inline',
            len(inline_findings),
            max_inline,
        )
        inline_findings = []

    inline_ids = {id(f) for f in inline_findings}

    use_formal = (
        provider.supports_formal_review
        and effective_formal_review(global_config, repo_config)
    )
    if effective_formal_review(global_config, repo_config) and not provider.supports_formal_review:
        logger.warning(
            'formal_review enabled but provider %s does not support it — falling back to comment-based review',
            type(provider).__name__,
        )

    if dry_run:
        _print_dry_run(
            result,
            inline_findings,
            inline_ids,
            global_config,
            project_config,
            cli,
            model=model,
            diff_lines=diff_lines,
            use_formal=use_formal,
        )
        return

    if use_formal:
        _post_formal_review(
            provider,
            state_db,
            pr,
            result,
            inline_findings,
            inline_ids,
            project_config,
            global_config,
            cli,
            model,
            diff_lines,
        )
    else:
        _post_comment_review(
            provider,
            state_db,
            pr,
            result,
            inline_findings,
            inline_ids,
            project_config,
            global_config,
            cli,
            model,
            diff_lines,
        )


def _post_formal_review(
    provider: GitProvider,
    state_db: StateDB,
    pr: PRInfo,
    result: ReviewResult,
    inline_findings: list[Finding],
    inline_ids: set[int],
    project_config: ProjectConfig,
    global_config: GlobalConfig,
    cli: CLI,
    model: str | None,
    diff_lines: int | None,
):
    event, approved, approve_blocked_reason = _select_review_event(result, project_config, diff_lines)
    logger.info('Posting formal review on PR #%d: event=%s', pr.pr_id, event.value)

    _dismiss_prior_reviews(provider, state_db, pr)

    old_comment_ids = state_db.get_comment_ids(pr.repo_slug, pr.pr_id)
    if old_comment_ids:
        logger.info('Deleting %d old inline comments on PR #%d', len(old_comment_ids), pr.pr_id)
        deleted = 0
        for cid in old_comment_ids:
            if provider.delete_comment(pr.repo_slug, pr.pr_id, cid):
                deleted += 1
        state_db.delete_comments(pr.repo_slug, pr.pr_id)
        logger.info('Deleted %d/%d old inline comments', deleted, len(old_comment_ids))

    body = _format_summary_comment(
        result,
        inline_ids,
        global_config,
        project_config,
        cli,
        model=model,
        approved=approved,
        approve_blocked_reason=approve_blocked_reason,
    )

    inline_payload = [
        InlineComment(path=f.file, line=f.line, body=_format_inline_comment(f))
        for f in inline_findings
    ]

    review_id = provider.submit_review(
        pr.repo_slug,
        pr.pr_id,
        body=body,
        event=event,
        inline_comments=inline_payload,
        source_commit=pr.source_commit,
    )
    if review_id is not None:
        state_db.record_review(pr.repo_slug, pr.pr_id, review_id)
    else:
        logger.warning('Formal review on PR #%d returned no ID (likely self-PR 422)', pr.pr_id)


def _post_comment_review(
    provider: GitProvider,
    state_db: StateDB,
    pr: PRInfo,
    result: ReviewResult,
    inline_findings: list[Finding],
    inline_ids: set[int],
    project_config: ProjectConfig,
    global_config: GlobalConfig,
    cli: CLI,
    model: str | None,
    diff_lines: int | None,
):
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
    approve_blocked_reason = None
    if aa.enabled:
        approved, approve_blocked_reason = _resolve_auto_approve(aa, result, diff_lines)
        if not approved:
            logger.info('Auto-approve blocked for PR #%d: %s', pr.pr_id, approve_blocked_reason or 'AI did not approve')

    logger.info('Posting summary comment')
    summary_body = _format_summary_comment(
        result,
        inline_ids,
        global_config,
        project_config,
        cli,
        model=model,
        approved=approved,
        approve_blocked_reason=approve_blocked_reason,
    )
    comment_id = provider.post_comment(pr.repo_slug, pr.pr_id, summary_body)
    state_db.record_comment(pr.repo_slug, pr.pr_id, comment_id)

    if project_config.critical_task and hasattr(provider, 'list_tasks'):
        _sync_critical_task(provider, pr, result, project_config)

    if approved and provider.approve_pr(pr.repo_slug, pr.pr_id):
        logger.info('Auto-approved PR #%d', pr.pr_id)


def _print_dry_run(
    result: ReviewResult,
    inline_findings: list[Finding],
    inline_ids: set[int],
    global_config: GlobalConfig,
    project_config: ProjectConfig,
    cli: CLI = CLI.CLAUDE,
    model: str | None = None,
    diff_lines: int | None = None,
    use_formal: bool = False,
):
    if use_formal:
        _print_dry_run_formal(result, inline_findings, inline_ids, global_config, project_config, cli, model, diff_lines)
        return

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
    approve_blocked_reason = None
    if aa.enabled:
        approved, approve_blocked_reason = _resolve_auto_approve(aa, result, diff_lines)
        if not approved:
            print(f'\n--- Auto-Approve: BLOCKED ({approve_blocked_reason or "AI did not approve"}) ---')

    print('\n--- Summary Comment ---')
    print(
        _format_summary_comment(
            result,
            inline_ids,
            global_config,
            project_config,
            cli,
            model=model,
            approved=approved,
            approve_blocked_reason=approve_blocked_reason,
        )
    )

    if aa.enabled and approved:
        print('\n--- Auto-Approve: WOULD APPROVE ---')

    print('=' * 60 + '\n')


def _print_dry_run_formal(
    result: ReviewResult,
    inline_findings: list[Finding],
    inline_ids: set[int],
    global_config: GlobalConfig,
    project_config: ProjectConfig,
    cli: CLI,
    model: str | None,
    diff_lines: int | None,
):
    event, approved, approve_blocked_reason = _select_review_event(result, project_config, diff_lines)

    print('\n' + '=' * 60)
    print(f'DRY RUN — would submit a formal review: event={event.value}')
    print('=' * 60)

    if inline_findings:
        print(f'\n--- Inline Comments ({len(inline_findings)}) ---')
        for f in inline_findings:
            print(f'\n  File: {f.file}:{f.line}')
            print(f'  {_format_inline_comment(f)}')

    print('\n--- Review Body ---')
    print(
        _format_summary_comment(
            result,
            inline_ids,
            global_config,
            project_config,
            cli,
            model=model,
            approved=approved,
            approve_blocked_reason=approve_blocked_reason,
        )
    )
    print('=' * 60 + '\n')
