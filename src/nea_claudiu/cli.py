from __future__ import annotations

import logging
import sys

import click

from nea_claudiu.config import load_global_config
from nea_claudiu.daemon import review_single_pr, run_poll_loop
from nea_claudiu.state import StateDB


def _setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)-8s %(name)s — %(message)s',
        datefmt='%H:%M:%S',
        stream=sys.stderr,
    )
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)


@click.group()
@click.option('--config', 'config_path', default=None, help='Path to global config file')
@click.pass_context
def main(ctx, config_path: str | None):
    ctx.ensure_object(dict)
    ctx.obj['config_path'] = config_path


@main.command()
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose logging')
@click.option('--dry-run', is_flag=True, help='Print reviews without posting')
@click.option('--review-existing', is_flag=True, help='Review already-open PRs on startup (default: only new PRs)')
@click.pass_context
def watch(ctx, verbose: bool, dry_run: bool, review_existing: bool):
    """Start the daemon — polls for new PRs and reviews them."""
    _setup_logging(verbose)
    config = load_global_config(ctx.obj['config_path'])
    run_poll_loop(config, dry_run=dry_run, review_existing=review_existing)


@main.command()
@click.argument('repo')
@click.option('--pr', 'pr_id', type=int, help='PR number to review')
@click.option('--branch', help='Branch name to find and review')
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose logging')
@click.option('--dry-run', is_flag=True, help='Print review without posting')
@click.option('--force', is_flag=True, help='Review even if already reviewed')
@click.pass_context
def review(ctx, repo: str, pr_id: int | None, branch: str | None, verbose: bool, dry_run: bool, force: bool):
    """One-shot review of a specific PR."""
    _setup_logging(verbose)
    if pr_id is None and branch is None:
        raise click.UsageError('Either --pr or --branch must be specified')
    config = load_global_config(ctx.obj['config_path'])
    review_single_pr(config, repo, pr_id=pr_id, branch=branch, dry_run=dry_run, force=force)


@main.command()
@click.argument('repo')
@click.option('-v', '--verbose', is_flag=True, help='Enable verbose logging')
@click.option('--limit', default=20, help='Number of recent reviews to show')
@click.pass_context
def status(ctx, repo: str, verbose: bool, limit: int):
    """Show review history for a repo."""
    _setup_logging(verbose)
    config = load_global_config(ctx.obj['config_path'])
    state_db = StateDB(config.state_db)
    try:
        history = state_db.get_review_history(repo, limit=limit)
        if not history:
            click.echo(f'No review history for {repo}')
            return
        for row in history:
            status_str = row['status']
            pr = row['pr_id']
            commit = row['source_commit'][:8]
            ts = row['created_at']
            err = row.get('error_message', '')
            line = f'PR #{pr}  {commit}  {status_str:<10}  {ts}'
            if err:
                line += f'  error: {err}'
            click.echo(line)
    finally:
        state_db.close()
