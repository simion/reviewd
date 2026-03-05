from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

from reviewd.commenter import post_review
from reviewd.config import get_provider, load_project_config
from reviewd.models import GlobalConfig, PRInfo, ProjectConfig, RepoConfig
from reviewd.providers.base import GitProvider
from reviewd.reviewer import cleanup_stale_worktrees, get_diff_lines, review_pr
from reviewd.state import StateDB

logger = logging.getLogger(__name__)

_is_verbose = False


def _get_pid_lock_path(state_db_path: str) -> Path:
    return Path(state_db_path).parent / 'reviewd.pid'


def _acquire_pid_lock(lock_path: Path):
    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text().strip())
            os.kill(old_pid, 0)
            logger.error('Another watch process is already running (pid %d)', old_pid)
            raise SystemExit(1)
        except (ValueError, ProcessLookupError):
            logger.info('Removing stale pid lock (pid gone)')
        except PermissionError:
            logger.error('Another watch process is already running (pid lock exists)')
            raise SystemExit(1) from None
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(str(os.getpid()))


def _release_pid_lock(lock_path: Path):
    lock_path.unlink(missing_ok=True)


def _status(msg: str, *, clear: bool = True):
    if _is_verbose:
        return
    if clear:
        sys.stderr.write(f'\r\033[K{msg}')
    else:
        sys.stderr.write(f'\r\033[K{msg}\n')
    sys.stderr.flush()


_REVIEW_TAGS = {'[ask]', '[bot review]', '[review]', '[claudiu]'}


def _has_review_tag(title: str) -> bool:
    title_lower = title.lower()
    return any(tag in title_lower for tag in _REVIEW_TAGS)


def _should_skip(pr: PRInfo, global_config: GlobalConfig) -> bool:
    if pr.draft and not _has_review_tag(pr.title):
        logger.debug('Skipping PR #%d: draft', pr.pr_id)
        return True
    title_lower = pr.title.lower()
    for pattern in global_config.skip_title_patterns:
        if pattern.lower() in title_lower:
            logger.info('Skipping PR #%d: title matches "%s"', pr.pr_id, pattern)
            return True
    if pr.author in global_config.skip_authors:
        logger.info('Skipping PR #%d: author "%s" is excluded', pr.pr_id, pr.author)
        return True
    return False


def _process_pr(
    pr: PRInfo,
    repo_config: RepoConfig,
    project_config: ProjectConfig,
    global_config: GlobalConfig,
    provider: GitProvider,
    state_db: StateDB,
    dry_run: bool = False,
    force: bool = False,
):
    if not force and _should_skip(pr, global_config):
        return

    if not force and state_db.has_review(pr.repo_slug, pr.pr_id, pr.source_commit):
        logger.debug('PR #%d@%s already reviewed, skipping', pr.pr_id, pr.source_commit[:8])
        return

    if not force and project_config.review_cooldown_minutes > 0:
        minutes = state_db.minutes_since_last_review(pr.repo_slug, pr.pr_id)
        if minutes is not None and minutes < project_config.review_cooldown_minutes:
            remaining = int(project_config.review_cooldown_minutes - minutes)
            logger.info('PR #%d in cooldown (%dmin remaining), skipping', pr.pr_id, remaining)
            return

    if not force:
        is_update = state_db.has_any_review(pr.repo_slug, pr.pr_id)
        threshold = project_config.min_diff_lines_update if is_update else project_config.min_diff_lines
        if threshold > 0:
            diff_lines = get_diff_lines(repo_config.path, pr)
            if 0 <= diff_lines < threshold:
                logger.info('PR #%d diff too small (%d lines < %d), skipping', pr.pr_id, diff_lines, threshold)
                return

    logger.log(25, 'Reviewing PR #%d: %s (commit %s)', pr.pr_id, pr.title, pr.source_commit[:8])
    state_db.start_review(pr.repo_slug, pr.pr_id, pr.source_commit)

    try:
        result = review_pr(
            repo_config.path,
            pr,
            project_config,
            cli=repo_config.cli,
            model=repo_config.model or global_config.model,
            cli_args=global_config.cli_args,
        )
        post_review(
            provider,
            state_db,
            pr,
            result,
            project_config,
            global_config,
            cli=repo_config.cli,
            model=repo_config.model or global_config.model,
            dry_run=dry_run,
        )
        state_db.finish_review(pr.repo_slug, pr.pr_id, pr.source_commit)
        logger.log(25, 'Finished review of PR #%d (%d findings)', pr.pr_id, len(result.findings))
    except Exception as e:
        state_db.finish_review(pr.repo_slug, pr.pr_id, pr.source_commit, error=str(e))
        logger.exception('Failed to review PR #%d', pr.pr_id)


def _process_repo(
    repo_config: RepoConfig,
    global_config: GlobalConfig,
    state_db: StateDB,
    dry_run: bool = False,
):
    provider = get_provider(global_config, repo_config)
    project_config = load_project_config(repo_config.path, global_config)

    logger.debug('Checking repo: %s', repo_config.name)
    prs = provider.list_open_prs(repo_config.slug)
    logger.debug('Found %d open PRs in %s', len(prs), repo_config.name)

    for pr in prs:
        _process_pr(pr, repo_config, project_config, global_config, provider, state_db, dry_run=dry_run)


def _boot_summary(global_config: GlobalConfig, state_db: StateDB, review_existing: bool):
    for repo_config in global_config.repos:
        provider = get_provider(global_config, repo_config)
        prs = provider.list_open_prs(repo_config.slug)
        logger.info(
            'Watching \033[32m%s\033[0m (%s, %s) — %d open PRs',
            repo_config.name,
            repo_config.provider,
            repo_config.cli.value,
            len(prs),
        )
        skipped = 0
        reviewed = 0
        drafts = 0
        for pr in prs:
            if pr.draft and not _has_review_tag(pr.title):
                logger.info('  \033[2m⏸ #%d  %s  (%s) — draft\033[0m', pr.pr_id, pr.title, pr.author)
                drafts += 1
                continue
            already_reviewed = state_db.has_review(pr.repo_slug, pr.pr_id, pr.source_commit)
            if already_reviewed:
                logger.info('  ✓ #%d  \033[37m%s\033[0m  (%s)', pr.pr_id, pr.title, pr.author)
                reviewed += 1
            elif review_existing:
                logger.info('  • #%d  %s  (%s) — will review', pr.pr_id, pr.title, pr.author)
            else:
                logger.info('  ⏭ #%d  %s  (%s) — skipping', pr.pr_id, pr.title, pr.author)
                state_db.start_review(pr.repo_slug, pr.pr_id, pr.source_commit)
                state_db.finish_review(pr.repo_slug, pr.pr_id, pr.source_commit)
                skipped += 1
        if skipped:
            logger.info(
                '  \033[33m%d not yet reviewed — use --review-existing to include them\033[0m',
                skipped,
            )


def run_poll_loop(
    global_config: GlobalConfig,
    dry_run: bool = False,
    review_existing: bool = False,
    verbose: bool = False,
):
    global _is_verbose
    _is_verbose = verbose

    state_db = StateDB(global_config.state_db)
    lock_path = _get_pid_lock_path(global_config.state_db)
    _acquire_pid_lock(lock_path)

    poll_interval = global_config.poll_interval_seconds
    total_repos = len(global_config.repos)

    for repo_config in global_config.repos:
        cleanup_stale_worktrees(repo_config.path)

    _boot_summary(global_config, state_db, review_existing)

    logger.info('Polling every %ds (dry_run=%s)', poll_interval, dry_run)

    import signal

    def _handle_shutdown(_signum, _frame):
        _status('', clear=True)
        logger.info('Shutting down')
        _release_pid_lock(lock_path)
        state_db.close()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    try:
        while True:
            now = datetime.now().strftime('%H:%M:%S')
            for i, repo_config in enumerate(global_config.repos, 1):
                _status(f'[{now}] Checking {repo_config.name} ({i}/{total_repos})')
                for attempt in range(3):
                    try:
                        _process_repo(repo_config, global_config, state_db, dry_run=dry_run)
                        break
                    except SystemExit:
                        raise
                    except httpx.TransportError:
                        if attempt < 2:
                            logger.warning('Network error for %s, retrying (%d/2)...', repo_config.name, attempt + 1)
                            time.sleep(2)
                        else:
                            logger.exception('Network error processing repo %s', repo_config.name)
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code >= 500:
                            logger.warning(
                                'Transient %s for %s, will retry next cycle',
                                e.response.status_code,
                                repo_config.name,
                            )
                        else:
                            logger.exception('HTTP error processing repo %s', repo_config.name)
                        break
                    except Exception:
                        logger.exception('Error processing repo %s', repo_config.name)
                        break

            next_check = datetime.now().timestamp() + poll_interval
            while time.time() < next_check:
                remaining = int(next_check - time.time())
                now = datetime.now().strftime('%H:%M:%S')
                _status(f'[{now}] Next check in {remaining}s')
                time.sleep(min(remaining, 5))
    finally:
        _status('', clear=True)
        state_db.close()


def review_single_pr(
    global_config: GlobalConfig,
    repo_name: str,
    pr_id: int,
    dry_run: bool = False,
    force: bool = False,
):
    import signal

    def _handle_shutdown(_signum, _frame):
        logger.info('Shutting down')
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    repo_config = next((r for r in global_config.repos if r.name == repo_name), None)
    if repo_config is None:
        raise ValueError(f'Repo "{repo_name}" not found in config')

    cleanup_stale_worktrees(repo_config.path)

    provider = get_provider(global_config, repo_config)
    project_config = load_project_config(repo_config.path, global_config)
    state_db = StateDB(global_config.state_db)

    try:
        pr = provider.get_pr(repo_config.slug, pr_id)
        _process_pr(pr, repo_config, project_config, global_config, provider, state_db, dry_run=dry_run, force=force)
    finally:
        state_db.close()
