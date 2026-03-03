from __future__ import annotations

import logging
import time

from nea_claudiu.commenter import post_review
from nea_claudiu.config import load_project_config, resolve_bitbucket_config
from nea_claudiu.models import GlobalConfig, PRInfo, ProjectConfig, RepoConfig
from nea_claudiu.providers.bitbucket import BitbucketProvider
from nea_claudiu.reviewer import cleanup_stale_worktrees, review_pr
from nea_claudiu.state import StateDB

logger = logging.getLogger(__name__)


def _should_skip(pr: PRInfo, project_config: ProjectConfig) -> bool:
    title_lower = pr.title.lower()
    for pattern in project_config.skip_title_patterns:
        if pattern.lower() in title_lower:
            logger.info('Skipping PR #%d: title matches "%s"', pr.pr_id, pattern)
            return True
    if pr.author in project_config.skip_authors:
        logger.info('Skipping PR #%d: author "%s" is excluded', pr.pr_id, pr.author)
        return True
    return False


def _process_pr(
    pr: PRInfo,
    repo_config: RepoConfig,
    project_config: ProjectConfig,
    provider: BitbucketProvider,
    state_db: StateDB,
    dry_run: bool = False,
    force: bool = False,
):
    if _should_skip(pr, project_config):
        return

    if not force and state_db.has_review(pr.repo_slug, pr.pr_id, pr.source_commit):
        logger.warning('PR #%d@%s already reviewed (use --force to re-review)', pr.pr_id, pr.source_commit[:8])
        return

    logger.info('Reviewing PR #%d: %s (commit %s)', pr.pr_id, pr.title, pr.source_commit[:8])
    state_db.start_review(pr.repo_slug, pr.pr_id, pr.source_commit)

    try:
        result = review_pr(
            repo_config.path, pr, project_config,
            ai_cli=repo_config.ai_cli,
        )
        post_review(
            provider, state_db, pr.repo_slug, pr.pr_id,
            result, project_config, dry_run=dry_run,
        )
        state_db.finish_review(pr.repo_slug, pr.pr_id, pr.source_commit)
        logger.info('Finished review of PR #%d (%d findings)', pr.pr_id, len(result.findings))
    except Exception as e:
        state_db.finish_review(pr.repo_slug, pr.pr_id, pr.source_commit, error=str(e))
        logger.exception('Failed to review PR #%d', pr.pr_id)


def _process_repo(
    repo_config: RepoConfig,
    global_config: GlobalConfig,
    state_db: StateDB,
    dry_run: bool = False,
):
    bb_config = resolve_bitbucket_config(global_config, repo_config)
    provider = BitbucketProvider(bb_config)
    project_config = load_project_config(repo_config.path, global_config)

    logger.debug('Checking repo: %s', repo_config.name)
    prs = provider.list_open_prs(repo_config.name)
    logger.debug('Found %d open PRs in %s', len(prs), repo_config.name)

    for pr in prs:
        _process_pr(pr, repo_config, project_config, provider, state_db, dry_run=dry_run)


def _mark_existing_prs(global_config: GlobalConfig, state_db: StateDB):
    for repo_config in global_config.repos:
        bb_config = resolve_bitbucket_config(global_config, repo_config)
        provider = BitbucketProvider(bb_config)
        prs = provider.list_open_prs(repo_config.name)
        skipped = 0
        for pr in prs:
            if not state_db.has_review(pr.repo_slug, pr.pr_id, pr.source_commit):
                state_db.start_review(pr.repo_slug, pr.pr_id, pr.source_commit)
                state_db.finish_review(pr.repo_slug, pr.pr_id, pr.source_commit)
                skipped += 1
        if skipped:
            logger.warning(
                '%s: skipped %d existing open PR(s) — use --review-existing to review them',
                repo_config.name, skipped,
            )


def run_poll_loop(global_config: GlobalConfig, dry_run: bool = False, review_existing: bool = False):
    state_db = StateDB(global_config.state_db)
    poll_interval = global_config.bitbucket.poll_interval_seconds

    for repo_config in global_config.repos:
        cleanup_stale_worktrees(repo_config.path)

    if not review_existing:
        _mark_existing_prs(global_config, state_db)

    logger.info('Starting poll loop (interval=%ds, repos=%d, dry_run=%s)',
                poll_interval, len(global_config.repos), dry_run)

    import signal

    def _handle_shutdown(_signum, _frame):
        logger.info('Shutting down')
        state_db.close()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    try:
        while True:
            for repo_config in global_config.repos:
                try:
                    _process_repo(repo_config, global_config, state_db, dry_run=dry_run)
                except SystemExit:
                    raise
                except Exception:
                    logger.exception('Error processing repo %s', repo_config.name)

            logger.debug('Sleeping %ds until next poll', poll_interval)
            time.sleep(poll_interval)
    finally:
        state_db.close()


def review_single_pr(
    global_config: GlobalConfig,
    repo_name: str,
    pr_id: int | None = None,
    branch: str | None = None,
    dry_run: bool = False,
    force: bool = False,
):
    repo_config = next((r for r in global_config.repos if r.name == repo_name), None)
    if repo_config is None:
        raise ValueError(f'Repo "{repo_name}" not found in config')

    cleanup_stale_worktrees(repo_config.path)

    bb_config = resolve_bitbucket_config(global_config, repo_config)
    provider = BitbucketProvider(bb_config)
    project_config = load_project_config(repo_config.path, global_config)
    state_db = StateDB(global_config.state_db)

    try:
        if pr_id is not None:
            pr = provider.get_pr(repo_name, pr_id)
        elif branch is not None:
            prs = provider.list_open_prs(repo_name)
            pr = next((p for p in prs if p.source_branch == branch), None)
            if pr is None:
                raise ValueError(f'No open PR found for branch "{branch}"')
        else:
            raise ValueError('Either --pr or --branch must be specified')

        _process_pr(pr, repo_config, project_config, provider, state_db, dry_run=dry_run, force=force)
    finally:
        state_db.close()
