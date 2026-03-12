from __future__ import annotations

import functools
import logging
import os
import signal
import sys
import termios
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import click
import httpx

from reviewd.colors import BOLD_WHITE, CLEAR_LINE, CYAN, DIM, GREEN, RESET, WHITE, YELLOW
from reviewd.commenter import post_review
from reviewd.config import get_provider, load_project_config
from reviewd.models import GlobalConfig, PRInfo, ProjectConfig, RepoConfig
from reviewd.reviewer import DEFAULT_TIMEOUT, cleanup_stale_worktrees, get_diff_lines, review_pr, terminate_all
from reviewd.state import StateDB

logger = logging.getLogger(__name__)

_shutdown_event = threading.Event()

# Active reviews registry: {(repo_slug, pr_id): (repo_name, start_time)}
_active_reviews: dict[tuple[str, int], tuple[str, float]] = {}
_active_reviews_lock = threading.Lock()

_SPINNER_FRAMES = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
_spinner_idx = 0


def _retry_on_network_error(retries=2, delay=5):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(retries + 1):
                try:
                    return fn(*args, **kwargs)
                except httpx.ConnectError:
                    if attempt < retries:
                        logger.warning('Network unavailable, retrying (%d/%d)...', attempt + 1, retries)
                        time.sleep(delay)
                    else:
                        logger.warning('Network unavailable, will retry next cycle')
                except httpx.TransportError:
                    if attempt < retries:
                        logger.warning('Network error, retrying (%d/%d)...', attempt + 1, retries)
                        time.sleep(delay)
                    else:
                        raise

        return wrapper

    return decorator


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


def _format_elapsed(elapsed: int, timeout: int = DEFAULT_TIMEOUT) -> str:
    text = f'{elapsed}s'
    if elapsed >= timeout * 0.8:
        return f'{YELLOW}{text}{RESET}'
    return text


def _format_progress_bar(elapsed: int, timeout: int = DEFAULT_TIMEOUT, width: int = 10) -> str:
    ratio = min(elapsed / timeout, 1.0)
    filled = int(ratio * width)
    remaining = width - filled
    color = YELLOW if ratio >= 0.8 else GREEN
    return f'{color}{"━" * filled}{RESET}{DIM}{"─" * remaining}{RESET}'


def _build_review_status() -> str:
    global _spinner_idx
    with _active_reviews_lock:
        if not _active_reviews:
            return ''
        _spinner_idx = (_spinner_idx + 1) % len(_SPINNER_FRAMES)
        spinner = _SPINNER_FRAMES[_spinner_idx]
        now = time.monotonic()
        parts = []
        for (_slug, pr_id), (repo_name, start) in _active_reviews.items():
            elapsed = int(now - start)
            bar = _format_progress_bar(elapsed)
            parts.append(f'#{pr_id} {repo_name} {bar} {_format_elapsed(elapsed)}')
        return f'{CYAN}{spinner}{RESET} ' + ' · '.join(parts)


def _status(msg: str, *, clear: bool = True):
    review_status = _build_review_status()
    if review_status:
        line = f'{CLEAR_LINE}{msg} {DIM}|{RESET} {review_status}'
    else:
        line = f'{CLEAR_LINE}{msg}'
    if clear:
        sys.stderr.write(line)
    else:
        sys.stderr.write(f'{line}\n')
    sys.stderr.flush()


_REVIEW_TAGS = {'[ask]', '[bot review]', '[review]', '[claudiu]'}


def _has_review_tag(title: str) -> bool:
    title_lower = title.lower()
    return any(tag in title_lower for tag in _REVIEW_TAGS)


def _should_skip(pr: PRInfo, global_config: GlobalConfig, *, ignore_draft: bool = False) -> bool:
    if not ignore_draft and pr.draft and not _has_review_tag(pr.title):
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
    state_db: StateDB,
    dry_run: bool = False,
    force: bool = False,
    ignore_draft: bool = False,
):
    if _shutdown_event.is_set():
        return

    if not pr.source_commit:
        logger.warning('PR #%d has no source commit (branch deleted?), skipping', pr.pr_id)
        return

    if not force and _should_skip(pr, global_config, ignore_draft=ignore_draft):
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

    diff_lines = None
    if not force:
        is_update = state_db.has_any_review(pr.repo_slug, pr.pr_id)
        threshold = project_config.min_diff_lines_update if is_update else project_config.min_diff_lines
        needs_diff = threshold > 0 or project_config.auto_approve.max_diff_lines is not None
        if needs_diff:
            diff_lines = get_diff_lines(repo_config.path, pr)
            if threshold > 0 and 0 <= diff_lines < threshold:
                logger.info('PR #%d diff too small (%d lines < %d), skipping', pr.pr_id, diff_lines, threshold)
                return

    logger.log(
        22,
        f'\nReviewing PR #%d by {BOLD_WHITE}%s{RESET} - {GREEN}%s{RESET} - {CYAN}%s{RESET}',
        pr.pr_id,
        pr.author,
        repo_config.name,
        pr.title,
    )
    provider = get_provider(global_config, repo_config)
    state_db.start_review(pr.repo_slug, pr.pr_id, pr.source_commit)
    review_key = (pr.repo_slug, pr.pr_id)

    with _active_reviews_lock:
        _active_reviews[review_key] = (repo_config.name, time.monotonic())

    try:
        result = review_pr(
            repo_config.path,
            pr,
            project_config,
            cli=repo_config.cli,
            model=repo_config.model or global_config.model,
            cli_args=global_config.cli_args,
            cli_defaults=global_config.cli_defaults,
        )
        if _shutdown_event.is_set():
            state_db.finish_review(pr.repo_slug, pr.pr_id, pr.source_commit, error='shutdown')
            return
        post_review(
            provider,
            state_db,
            pr,
            result,
            project_config,
            global_config,
            cli=repo_config.cli,
            dry_run=dry_run,
            diff_lines=diff_lines,
        )
        state_db.finish_review(pr.repo_slug, pr.pr_id, pr.source_commit)
        logger.log(25, 'Finished review of PR #%d (%d findings)', pr.pr_id, len(result.findings))
    except Exception as e:
        state_db.finish_review(pr.repo_slug, pr.pr_id, pr.source_commit, error=str(e))
        logger.exception('Failed to review PR #%d', pr.pr_id)
    finally:
        with _active_reviews_lock:
            _active_reviews.pop(review_key, None)


@_retry_on_network_error()
def _collect_eligible_prs(
    repo_config: RepoConfig,
    global_config: GlobalConfig,
    state_db: StateDB,
) -> list[tuple[PRInfo, RepoConfig, ProjectConfig, GlobalConfig]]:
    provider = get_provider(global_config, repo_config)
    project_config = load_project_config(repo_config.path, global_config)

    logger.debug('Checking repo: %s', repo_config.name)
    prs = provider.list_open_prs(repo_config.slug)
    logger.debug('Found %d open PRs in %s', len(prs), repo_config.name)

    eligible = []
    for pr in prs:
        if not pr.source_commit:
            logger.debug('PR #%d has no source commit (branch deleted?), skipping', pr.pr_id)
            continue
        if _should_skip(pr, global_config):
            continue
        if state_db.has_review(pr.repo_slug, pr.pr_id, pr.source_commit):
            logger.debug('PR #%d@%s already reviewed, skipping', pr.pr_id, pr.source_commit[:8])
            continue
        if project_config.review_cooldown_minutes > 0:
            minutes = state_db.minutes_since_last_review(pr.repo_slug, pr.pr_id)
            if minutes is not None and minutes < project_config.review_cooldown_minutes:
                remaining = int(project_config.review_cooldown_minutes - minutes)
                logger.debug('PR #%d in cooldown (%dmin remaining), skipping', pr.pr_id, remaining)
                continue
        eligible.append((pr, repo_config, project_config, global_config))
    return eligible


def _boot_summary(global_config: GlobalConfig, state_db: StateDB, review_existing: bool):
    for repo_config in global_config.repos:
        provider = get_provider(global_config, repo_config)
        prs = provider.list_open_prs(repo_config.slug)
        logger.info(
            f'Watching {GREEN}%s{RESET} (%s, %s) — %d open PRs',
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
                logger.info(f'  {DIM}⏸ #%d  %s  (%s) — draft{RESET}', pr.pr_id, pr.title, pr.author)
                drafts += 1
                continue
            already_reviewed = state_db.has_review(pr.repo_slug, pr.pr_id, pr.source_commit)
            if already_reviewed:
                logger.info(f'  ✓ #%d  {WHITE}%s{RESET}  (%s)', pr.pr_id, pr.title, pr.author)
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
                f'  {YELLOW}%d not yet reviewed — use --review-existing to include them{RESET}',
                skipped,
            )


def run_poll_loop(
    global_config: GlobalConfig,
    dry_run: bool = False,
    review_existing: bool = False,
    verbose: bool = False,
):
    _shutdown_event.clear()

    state_db = StateDB(global_config.state_db)
    lock_path = _get_pid_lock_path(global_config.state_db)
    _acquire_pid_lock(lock_path)

    poll_interval = global_config.poll_interval_seconds
    total_repos = len(global_config.repos)
    max_workers = global_config.max_concurrent_reviews

    for repo_config in global_config.repos:
        cleanup_stale_worktrees(repo_config.path)

    _boot_summary(global_config, state_db, review_existing)

    logger.info('Polling every %ds, max %d concurrent reviews (dry_run=%s)', poll_interval, max_workers, dry_run)

    # Save terminal settings so we can restore after subprocesses corrupt them
    _saved_termios = None
    try:
        _saved_termios = termios.tcgetattr(sys.stdin.fileno())
    except (termios.error, ValueError, OSError):
        pass

    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='review')
    futures: dict[Future, PRInfo] = {}

    _force_quit = False

    def _restore_terminal():
        if _saved_termios is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _saved_termios)
            except (termios.error, ValueError, OSError):
                pass

    def _handle_shutdown(_signum, _frame):
        nonlocal _force_quit
        _restore_terminal()
        if _shutdown_event.is_set():
            _force_quit = True
            sys.stderr.write(f'\n{YELLOW}Force quit — killing all reviews...{RESET}\n')
            sys.stderr.flush()
            terminate_all()
            raise SystemExit(1)
        _shutdown_event.set()
        terminate_all()
        with _active_reviews_lock:
            active = list(_active_reviews.items())
        if active:
            prs = ', '.join(f'#{pr_id}' for (_slug, pr_id), _ in active)
            sys.stderr.write(
                f'\n{YELLOW}Shutting down gracefully, waiting for {prs} to finish...{RESET}'
                f'\n{DIM}Press Ctrl+C again to force quit{RESET}\n'
            )
        else:
            sys.stderr.write(f'\n{YELLOW}Shutting down...{RESET}\n')
        sys.stderr.flush()

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    try:
        while not _shutdown_event.is_set():
            now = datetime.now().strftime('%H:%M:%S')

            # Collect eligible PRs from all repos
            all_eligible = []
            for i, repo_config in enumerate(global_config.repos, 1):
                _status(f'[{now}] Checking {repo_config.name} ({i}/{total_repos})')
                try:
                    eligible = _collect_eligible_prs(repo_config, global_config, state_db)
                    all_eligible.extend(eligible)
                except SystemExit:
                    raise
                except httpx.HTTPStatusError as e:
                    if e.response.status_code >= 500:
                        logger.warning(
                            'Transient %s for %s, will retry next cycle',
                            e.response.status_code,
                            repo_config.name,
                        )
                    else:
                        logger.exception('HTTP error processing repo %s', repo_config.name)
                except Exception:
                    logger.exception('Error processing repo %s', repo_config.name)

            # Harvest completed futures
            done = [f for f in futures if f.done()]
            for f in done:
                pr_info = futures.pop(f)
                try:
                    f.result()
                except Exception:
                    logger.exception('Review thread failed for PR #%d', pr_info.pr_id)

            # Submit new reviews, skip PRs already in-flight
            in_flight = {(p.repo_slug, p.pr_id) for p in futures.values()}
            for pr, repo_cfg, proj_cfg, glob_cfg in all_eligible:
                if (pr.repo_slug, pr.pr_id) in in_flight:
                    continue
                future = executor.submit(
                    _process_pr,
                    pr,
                    repo_cfg,
                    proj_cfg,
                    glob_cfg,
                    state_db,
                    dry_run=dry_run,
                )
                futures[future] = pr
                in_flight.add((pr.repo_slug, pr.pr_id))

            # Sleep until next poll
            next_check = time.time() + poll_interval
            while time.time() < next_check and not _shutdown_event.is_set():
                # Harvest completed futures during sleep to log errors promptly
                done = [f for f in futures if f.done()]
                for f in done:
                    pr_info = futures.pop(f)
                    try:
                        f.result()
                    except Exception:
                        logger.exception('Review thread failed for PR #%d', pr_info.pr_id)

                remaining = max(0, int(next_check - time.time()))
                now = datetime.now().strftime('%H:%M:%S')
                _status(f'[{now}] Next check in {remaining}s')
                time.sleep(min(remaining, 2))
    finally:
        _shutdown_event.set()
        terminate_all()
        if not _force_quit:
            executor.shutdown(wait=True, cancel_futures=True)
        else:
            executor.shutdown(wait=False, cancel_futures=True)
        _status('', clear=True)
        _restore_terminal()
        _release_pid_lock(lock_path)
        state_db.close()


def review_single_pr(
    global_config: GlobalConfig,
    repo_name: str,
    pr_id: int,
    dry_run: bool = False,
    force: bool = False,
):
    def _handle_shutdown(_signum, _frame):
        logger.info('Shutting down')
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    repo_config = next((r for r in global_config.repos if r.name == repo_name), None)
    if repo_config is None:
        available = ', '.join(r.name for r in global_config.repos) or '(none)'
        raise click.ClickException(f'Repo "{repo_name}" not found in config. Available: {available}')

    cleanup_stale_worktrees(repo_config.path)

    provider = get_provider(global_config, repo_config)
    project_config = load_project_config(repo_config.path, global_config)
    state_db = StateDB(global_config.state_db)

    try:
        pr = provider.get_pr(repo_config.slug, pr_id)
        _process_pr(
            pr,
            repo_config,
            project_config,
            global_config,
            state_db,
            dry_run=dry_run,
            force=force,
            ignore_draft=True,
        )
    finally:
        state_db.close()
