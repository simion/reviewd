from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from reviewd.models import (
    CLI,
    Finding,
    PRInfo,
    ProjectConfig,
    ReviewResult,
    Severity,
)
from reviewd.prompt import build_review_prompt

logger = logging.getLogger(__name__)

JSON_BLOCK_PATTERN = re.compile(r'```json\s*\n(.*?)\n\s*```', re.DOTALL)
DEFAULT_TIMEOUT = 600
_GIT_ENV = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}

_repo_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
_active_procs: set[subprocess.Popen] = set()
_active_procs_lock = threading.Lock()


def terminate_all():
    with _active_procs_lock:
        for proc in _active_procs:
            with contextlib.suppress(OSError):
                proc.terminate()


def cleanup_stale_worktrees(repo_path: str):
    worktree_root = Path(repo_path) / '.reviewd-worktrees'
    if not worktree_root.exists():
        return
    # Check for any running claude/gemini processes using this worktree
    for entry in worktree_root.iterdir():
        if not entry.is_dir():
            continue
        lock_file = entry / '.git'
        if not lock_file.exists():
            # Not a valid worktree, just remove the directory
            import shutil

            shutil.rmtree(entry, ignore_errors=True)
            logger.info('Removed orphan directory: %s', entry.name)
            continue
        result = subprocess.run(
            ['git', 'worktree', 'remove', str(entry), '--force'],
            cwd=repo_path,
            capture_output=True,
            env=_GIT_ENV,
        )
        if result.returncode == 0:
            logger.info('Cleaned up stale worktree: %s', entry.name)
        else:
            logger.warning('Failed to clean worktree %s: %s', entry.name, result.stderr.decode().strip())


def create_worktree(repo_path: str, pr: PRInfo) -> str:
    worktree_dir = Path(repo_path) / '.reviewd-worktrees' / f'pr-{pr.pr_id}'
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    if worktree_dir.exists():
        cleanup_worktree(repo_path, pr)

    with _repo_locks[repo_path]:
        # Try fetching branch by name first; fall back to destination only
        # (source branch may have been deleted after merge)
        fetch_result = subprocess.run(
            ['git', 'fetch', 'origin', pr.source_branch, pr.destination_branch],
            cwd=repo_path,
            capture_output=True,
            env=_GIT_ENV,
        )
        if fetch_result.returncode != 0:
            logger.warning('Source branch fetch failed, fetching destination only')
            subprocess.run(
                ['git', 'fetch', 'origin', pr.destination_branch],
                cwd=repo_path,
                check=True,
                capture_output=True,
                env=_GIT_ENV,
            )

        # Use branch ref if available, otherwise the commit hash (must exist locally)
        checkout_ref = f'origin/{pr.source_branch}'
        ref_check = subprocess.run(
            ['git', 'rev-parse', '--verify', checkout_ref],
            cwd=repo_path,
            capture_output=True,
            env=_GIT_ENV,
        )
        if ref_check.returncode != 0:
            checkout_ref = pr.source_commit

        subprocess.run(
            ['git', 'worktree', 'add', str(worktree_dir), checkout_ref, '--detach'],
            cwd=repo_path,
            check=True,
            capture_output=True,
            env=_GIT_ENV,
        )

    if not worktree_dir.exists():
        raise RuntimeError(f'Worktree creation succeeded but directory does not exist: {worktree_dir}')
    logger.info('Created worktree at %s', worktree_dir)
    return str(worktree_dir)


def cleanup_worktree(repo_path: str, pr: PRInfo):
    worktree_dir = Path(repo_path) / '.reviewd-worktrees' / f'pr-{pr.pr_id}'
    if worktree_dir.exists():
        with _repo_locks[repo_path]:
            subprocess.run(
                ['git', 'worktree', 'remove', str(worktree_dir), '--force'],
                cwd=repo_path,
                capture_output=True,
                env=_GIT_ENV,
            )
        logger.info('Cleaned up worktree at %s', worktree_dir)


def get_diff_lines(repo_path: str, pr: PRInfo) -> int:
    with _repo_locks[repo_path]:
        subprocess.run(
            ['git', 'fetch', 'origin', pr.source_branch, pr.destination_branch],
            cwd=repo_path,
            capture_output=True,
            env=_GIT_ENV,
        )
    result = subprocess.run(
        ['git', 'diff', '--shortstat', f'origin/{pr.destination_branch}...origin/{pr.source_branch}'],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning('Could not compute diff size for PR #%d, proceeding anyway', pr.pr_id)
        return -1
    # "3 files changed, 10 insertions(+), 2 deletions(-)"
    stat = result.stdout.strip()
    if not stat:
        return 0
    total = 0
    for part in stat.split(','):
        part = part.strip()
        if 'insertion' in part or 'deletion' in part:
            total += int(part.split()[0])
    return total


def _build_cli_command(
    cli: CLI,
    prompt_file: str,
    model: str | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    prompt_arg = Path(prompt_file).read_text()
    extra = extra_args or []
    model_args = ['--model', model] if model else []
    if cli == CLI.CLAUDE:
        return [
            'claude',
            '--print',
            '--disallowedTools',
            'Write,Edit',
            '--mcp-config',
            '{"mcpServers":{}}',
            '--strict-mcp-config',
            *model_args,
            *extra,
            '-p',
            prompt_arg,
        ]
    if cli == CLI.GEMINI:
        return ['gemini', '--approval-mode', 'yolo', '-e', 'none', *model_args, *extra, '-p', prompt_arg]
    raise ValueError(f'Unknown AI CLI: {cli}')


def invoke_cli(
    prompt: str,
    cwd: str,
    cli: CLI = CLI.CLAUDE,
    timeout: int = DEFAULT_TIMEOUT,
    model: str | None = None,
    cli_args: list[str] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> str:
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write(prompt)
        prompt_file = f.name

    try:
        cmd = _build_cli_command(cli, prompt_file, model=model, extra_args=cli_args)
        display_cmd = [c if c != cmd[-1] else '<prompt>' for c in cmd]
        logger.info('Running: %s (cwd=%s, timeout=%ds)', ' '.join(display_cmd), cwd, timeout)
        logger.debug('Prompt:\n%s', prompt)
        env = {**os.environ}
        env.pop('CLAUDECODE', None)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            with _active_procs_lock:
                _active_procs.add(proc)
        except FileNotFoundError as e:
            raise RuntimeError(
                f'"{cli.value}" CLI not found. Install it first: https://github.com/anthropics/claude-code'
                if cli.value == 'claude'
                else f'"{cli.value}" CLI not found. Make sure it is installed and on your PATH.'
            ) from e

        stderr_lines: list[str] = []
        stop_event = threading.Event()

        def _stream_stderr():
            for line in proc.stderr or []:
                line = line.rstrip('\n')
                stderr_lines.append(line)
                logger.info('[%s] %s', cli.value, line)

        def _progress_ticker():
            t0 = time.monotonic()
            while not stop_event.wait(30):
                elapsed = int(time.monotonic() - t0)
                msg = f'{cli.value} review in progress... ({elapsed}s elapsed)'
                if progress_callback:
                    progress_callback(msg)
                else:
                    logger.info('%s', msg)

        stderr_thread = threading.Thread(target=_stream_stderr, daemon=True)
        stderr_thread.start()
        ticker_thread = threading.Thread(target=_progress_ticker, daemon=True)
        ticker_thread.start()

        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt, SystemExit):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise
        finally:
            with _active_procs_lock:
                _active_procs.discard(proc)
            stop_event.set()
            stderr_thread.join(timeout=5)
            ticker_thread.join(timeout=5)
            if progress_callback:
                progress_callback('')

        stderr = '\n'.join(stderr_lines)
        if proc.returncode != 0:
            logger.error('%s stderr: %s', cli.value, stderr)
            raise RuntimeError(f'{cli.value} exited with code {proc.returncode}: {stderr}')
        return stdout
    finally:
        Path(prompt_file).unlink(missing_ok=True)


def extract_json(output: str) -> dict:
    matches = JSON_BLOCK_PATTERN.findall(output)
    if not matches:
        tail = output[-500:] if len(output) > 500 else output
        logger.error('No JSON block found in AI output. Last 500 chars:\n%s', tail)
        raise ValueError('No JSON block found in AI output')
    raw = matches[-1]
    # Strip trailing commas before } or ] (common LLM JSON error)
    raw = re.sub(r',\s*([}\]])', r'\1', raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error('Malformed JSON in AI output: %s\nRaw JSON:\n%s', e, raw[:1000])
        raise ValueError(f'Malformed JSON in AI output: {e}') from e


def parse_review_result(data: dict) -> ReviewResult:
    findings = []
    for f in data.get('findings', []):
        try:
            severity = Severity(f.get('severity', 'suggestion'))
        except ValueError:
            logger.warning('Unknown severity %r in finding, defaulting to suggestion', f.get('severity'))
            severity = Severity.SUGGESTION
        findings.append(
            Finding(
                severity=severity,
                category=f.get('category', 'General'),
                title=f.get('title', ''),
                file=f.get('file', ''),
                line=f.get('line'),
                end_line=f.get('end_line'),
                issue=f.get('issue', ''),
                fix=f.get('fix'),
            )
        )
    return ReviewResult(
        overview=data.get('overview', ''),
        findings=findings,
        summary=data.get('summary', ''),
        tests_passed=data.get('tests_passed'),
        approve=bool(data.get('approve', False)),
        approve_reason=data.get('approve_reason'),
    )


def review_pr(
    repo_path: str,
    pr: PRInfo,
    project_config: ProjectConfig,
    cli: CLI = CLI.CLAUDE,
    timeout: int = DEFAULT_TIMEOUT,
    model: str | None = None,
    cli_args: list[str] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> ReviewResult:
    worktree_path = create_worktree(repo_path, pr)
    try:
        prompt = build_review_prompt(pr, project_config)
        t0 = time.monotonic()
        output = invoke_cli(
            prompt,
            worktree_path,
            cli=cli,
            timeout=timeout,
            model=model,
            cli_args=cli_args,
            progress_callback=progress_callback,
        )
        elapsed = time.monotonic() - t0
        logger.info('AI review completed in %.1fs', elapsed)
        logger.debug('Extracting JSON from AI output (%d chars)', len(output))
        data = extract_json(output)
        logger.debug('Parsed %d findings', len(data.get('findings', [])))
        result = parse_review_result(data)
        logger.info('Review has %d findings', len(result.findings))
        return result
    finally:
        logger.debug('Cleaning up worktree for PR #%d', pr.pr_id)
        cleanup_worktree(repo_path, pr)
        logger.debug('Worktree cleanup done')
