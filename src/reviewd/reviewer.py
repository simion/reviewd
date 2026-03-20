from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
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
_GIT_ENV = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_LFS_SKIP_SMUDGE': '1'}

_repo_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
_active_procs: set[subprocess.Popen] = set()
_active_procs_lock = threading.Lock()


def terminate_all():
    with _active_procs_lock:
        procs = list(_active_procs)
    for proc in procs:
        with contextlib.suppress(OSError):
            # Kill entire process group (subprocess is session leader)
            os.killpg(proc.pid, signal.SIGTERM)
    # Give processes a moment to die, then force-kill survivors
    for proc in procs:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)


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
            timeout=30,
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
            timeout=120,
        )
        if fetch_result.returncode != 0:
            logger.warning('Source branch fetch failed: %s', fetch_result.stderr.decode().strip())
            dest_result = subprocess.run(
                ['git', 'fetch', 'origin', pr.destination_branch],
                cwd=repo_path,
                capture_output=True,
                env=_GIT_ENV,
                timeout=120,
            )
            if dest_result.returncode != 0:
                raise RuntimeError(
                    f'Cannot fetch destination branch {pr.destination_branch}: '
                    f'{dest_result.stderr.decode().strip()}'
                )

        # Use branch ref if available, otherwise the commit hash (must exist locally)
        checkout_ref = f'origin/{pr.source_branch}'
        ref_check = subprocess.run(
            ['git', 'rev-parse', '--verify', checkout_ref],
            cwd=repo_path,
            capture_output=True,
            env=_GIT_ENV,
            timeout=10,
        )
        if ref_check.returncode != 0:
            checkout_ref = pr.source_commit

        wt_result = subprocess.run(
            [
                'git',
                '-c',
                'filter.git-crypt.smudge=cat',
                '-c',
                'filter.git-crypt.required=false',
                'worktree',
                'add',
                str(worktree_dir),
                checkout_ref,
                '--detach',
            ],
            cwd=repo_path,
            capture_output=True,
            env=_GIT_ENV,
            timeout=30,
        )
        if wt_result.returncode != 0:
            stderr = wt_result.stderr.decode().strip()
            # Stale worktree reference — prune and retry once
            if 'already registered' in stderr or 'already exists' in stderr:
                logger.warning('Stale worktree for PR #%d, pruning and retrying', pr.pr_id)
                subprocess.run(
                    ['git', 'worktree', 'prune'],
                    cwd=repo_path,
                    capture_output=True,
                    env=_GIT_ENV,
                    timeout=30,
                )
                if worktree_dir.exists():
                    shutil.rmtree(worktree_dir, ignore_errors=True)
                subprocess.run(
                    [
                        'git',
                        '-c',
                        'filter.git-crypt.smudge=cat',
                        '-c',
                        'filter.git-crypt.required=false',
                        'worktree',
                        'add',
                        str(worktree_dir),
                        checkout_ref,
                        '--detach',
                    ],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                    env=_GIT_ENV,
                    timeout=30,
                )
            else:
                raise RuntimeError(f'git worktree add failed for PR #{pr.pr_id}: {stderr}')

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
                timeout=30,
            )
        logger.info('Cleaned up worktree at %s', worktree_dir)


def get_diff_lines(repo_path: str, pr: PRInfo) -> int:
    with _repo_locks[repo_path]:
        subprocess.run(
            ['git', 'fetch', 'origin', pr.source_branch, pr.destination_branch],
            cwd=repo_path,
            capture_output=True,
            env=_GIT_ENV,
            timeout=120,
        )
    result = subprocess.run(
        ['git', 'diff', '--shortstat', f'origin/{pr.destination_branch}...origin/{pr.source_branch}'],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=30,
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


REVIEW_SCHEMA: dict = {
    'type': 'object',
    'properties': {
        'overview': {'type': 'string'},
        'findings': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'severity': {'type': 'string', 'enum': [s.value for s in Severity]},
                    'category': {'type': 'string'},
                    'title': {'type': 'string'},
                    'file': {'type': 'string'},
                    'line': {'type': ['integer', 'null']},
                    'issue': {'type': 'string'},
                    'fix': {'type': ['string', 'null']},
                },
                'required': ['severity', 'category', 'title', 'file', 'line', 'issue', 'fix'],
                'additionalProperties': False,
            },
        },
        'summary': {'type': 'string'},
        'tests_passed': {'type': ['boolean', 'null']},
        'approve': {'type': 'boolean'},
        'approve_reason': {'type': ['string', 'null']},
    },
    'required': ['overview', 'findings', 'summary', 'tests_passed', 'approve', 'approve_reason'],
    'additionalProperties': False,
}


CLI_DEFAULTS: dict[CLI, list[str]] = {
    CLI.CLAUDE: [
        'claude',
        '--print',
        '--disallowedTools',
        'Write,Edit',
        '--mcp-config',
        '{"mcpServers":{}}',
        '--strict-mcp-config',
    ],
    CLI.GEMINI: ['gemini', '--approval-mode', 'yolo', '-e', 'none'],
    CLI.CODEX: ['codex', 'exec', '--sandbox', 'workspace-write'],
}

_CLI_PROMPT_MODE: dict[CLI, str] = {
    CLI.CLAUDE: 'flag',
    CLI.GEMINI: 'flag',
    CLI.CODEX: 'stdin',
}

_CLI_NOT_FOUND_HINTS: dict[CLI, str] = {
    CLI.CLAUDE: 'Install it first: https://github.com/anthropics/claude-code',
    CLI.GEMINI: 'Make sure it is installed and on your PATH.',
    CLI.CODEX: 'Install with: npm install -g @openai/codex',
}


def _build_cli_command(
    cli: CLI,
    prompt_file: str,
    model: str | None = None,
    extra_args: list[str] | None = None,
    cli_defaults: dict[CLI, list[str]] | None = None,
) -> tuple[list[str], str | None]:
    """Returns (command, stdin_input). stdin_input is None when prompt is passed via flag."""
    prompt_text = Path(prompt_file).read_text()
    extra = extra_args or []
    model_args = ['--model', model] if model else []

    if cli_defaults and cli in cli_defaults:
        base = list(cli_defaults[cli])
    elif cli in CLI_DEFAULTS:
        base = list(CLI_DEFAULTS[cli])
    else:
        raise ValueError(f'Unknown AI CLI: {cli}')

    prompt_mode = _CLI_PROMPT_MODE[cli]
    if prompt_mode == 'stdin':
        return [*base, *model_args, *extra, '-'], prompt_text
    return [*base, *model_args, *extra, '-p', prompt_text], None


def invoke_cli(
    prompt: str,
    cwd: str,
    cli: CLI = CLI.CLAUDE,
    timeout: int = DEFAULT_TIMEOUT,
    model: str | None = None,
    cli_args: list[str] | None = None,
    cli_defaults: dict[CLI, list[str]] | None = None,
) -> str:
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write(prompt)
        prompt_file = f.name

    schema_file = None
    output_file = None
    try:
        cmd, stdin_input = _build_cli_command(
            cli, prompt_file, model=model, extra_args=cli_args, cli_defaults=cli_defaults
        )

        if cli == CLI.CODEX:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as sf:
                schema_file = sf.name
            Path(schema_file).write_text(json.dumps(REVIEW_SCHEMA))
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as of:
                output_file = of.name
            cmd = [*cmd[:-1], '--output-schema', schema_file, '-o', output_file, cmd[-1]]

        if stdin_input:
            display_cmd = list(cmd)
        else:
            display_cmd = [c if c != cmd[-1] else '<prompt>' for c in cmd]
        logger.info('Running: %s (cwd=%s, timeout=%ds)', ' '.join(display_cmd), cwd, timeout)
        logger.debug('Prompt:\n%s', prompt)
        env = {**os.environ}
        env.pop('CLAUDECODE', None)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.PIPE if stdin_input else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )
            with _active_procs_lock:
                _active_procs.add(proc)
        except FileNotFoundError as e:
            hint = _CLI_NOT_FOUND_HINTS.get(cli, 'Make sure it is installed and on your PATH.')
            raise RuntimeError(f'"{cli.value}" CLI not found. {hint}') from e

        if stdin_input and proc.stdin:
            proc.stdin.write(stdin_input)
            proc.stdin.close()
            proc.stdin = None

        stderr_lines: list[str] = []

        def _stream_stderr():
            for line in proc.stderr or []:
                line = line.rstrip('\n')
                stderr_lines.append(line)
                logger.debug('[%s] %s', cli.value, line)

        stderr_thread = threading.Thread(target=_stream_stderr, daemon=True)
        stderr_thread.start()

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
            stderr_thread.join(timeout=5)

        stderr = '\n'.join(stderr_lines)
        if proc.returncode != 0:
            logger.error('%s stderr: %s', cli.value, stderr)
            raise RuntimeError(f'{cli.value} exited with code {proc.returncode}: {stderr}')

        if output_file and Path(output_file).exists():
            result = Path(output_file).read_text()
            if result.strip():
                logger.info('Read output from -o file (%d chars)', len(result))
                return result

        return stdout
    finally:
        Path(prompt_file).unlink(missing_ok=True)
        if schema_file:
            Path(schema_file).unlink(missing_ok=True)
        if output_file:
            Path(output_file).unlink(missing_ok=True)


def _find_last_json_object(output: str) -> str | None:
    """Find the last valid JSON object in output (no code fences)."""
    # Search backwards for each '{' and try to parse from there to the last '}'
    last_brace = output.rfind('}')
    if last_brace == -1:
        return None
    pos = last_brace
    while True:
        pos = output.rfind('{', 0, pos)
        if pos == -1:
            return None
        candidate = output[pos : last_brace + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            continue


def extract_json(output: str) -> dict:
    matches = JSON_BLOCK_PATTERN.findall(output)
    if not matches:
        # Fallback: try to find raw JSON object without code fences (e.g. Codex output)
        raw_json = _find_last_json_object(output)
        if raw_json:
            logger.info('No fenced JSON block found, extracted raw JSON object')
            matches = [raw_json]
    if not matches:
        tail = output[-500:] if len(output) > 500 else output
        logger.error('No JSON block found in AI output. Last 500 chars:\n%s', tail)
        raise ValueError('No JSON block found in AI output')
    raw = matches[-1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Strip trailing commas before } or ] (common LLM JSON error) and retry
        fixed = re.sub(r',\s*([}\]])', r'\1', raw)
        try:
            logger.warning('Fixed trailing commas in AI JSON output')
            return json.loads(fixed)
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
    cli_defaults: dict[CLI, list[str]] | None = None,
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
            cli_defaults=cli_defaults,
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
