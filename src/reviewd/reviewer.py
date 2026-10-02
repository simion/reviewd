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
# Interactive PTY children (pexpect spawns its own session leader, not a Popen)
_active_pty_pids: set[int] = set()
_active_pty_pids_lock = threading.Lock()


def terminate_all():
    with _active_procs_lock:
        procs = list(_active_procs)
    with _active_pty_pids_lock:
        pty_pids = list(_active_pty_pids)
    for proc in procs:
        with contextlib.suppress(OSError):
            # Kill entire process group (subprocess is session leader)
            os.killpg(proc.pid, signal.SIGTERM)
    for pid in pty_pids:
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGTERM)
    # Give processes a moment to die, then force-kill survivors
    for proc in procs:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
    for pid in pty_pids:
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGKILL)


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
            # Corrupted worktree (e.g. .git is a dir instead of a file after a killed review)
            # Force-remove the directory and prune the worktree list
            logger.warning('git worktree remove failed for %s, force-cleaning', entry.name)
            shutil.rmtree(entry, ignore_errors=True)
            subprocess.run(
                ['git', 'worktree', 'prune'],
                cwd=repo_path,
                capture_output=True,
                env=_GIT_ENV,
                timeout=30,
            )
            logger.info('Force-cleaned worktree: %s', entry.name)


def create_worktree(repo_path: str, pr: PRInfo) -> str:
    worktree_dir = Path(repo_path) / '.reviewd-worktrees' / f'pr-{pr.pr_id}'
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    if worktree_dir.exists():
        cleanup_worktree(repo_path, pr)

    with _repo_locks[repo_path]:
        # Try fetching branch by name first; fall back to PR ref or commit hash
        # (source branch may live on a fork or have been deleted after merge)
        pr_ref_fetched = False
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
            # Try PR refs (works for forks and deleted branches)
            # GitHub: refs/pull/<id>/head, BitBucket: refs/pull-requests/<id>/from,
            # GitLab: refs/merge-requests/<iid>/head
            for pr_ref in [
                f'pull/{pr.pr_id}/head',
                f'pull-requests/{pr.pr_id}/from',
                f'merge-requests/{pr.pr_id}/head',
            ]:
                ref_result = subprocess.run(
                    ['git', 'fetch', 'origin', pr_ref],
                    cwd=repo_path,
                    capture_output=True,
                    env=_GIT_ENV,
                    timeout=120,
                )
                if ref_result.returncode == 0:
                    logger.info('Fetched PR via ref: %s', pr_ref)
                    pr_ref_fetched = True
                    break

        # Use branch ref if available, then FETCH_HEAD (from PR ref), then commit hash
        checkout_ref = f'origin/{pr.source_branch}'
        ref_check = subprocess.run(
            ['git', 'rev-parse', '--verify', checkout_ref],
            cwd=repo_path,
            capture_output=True,
            env=_GIT_ENV,
            timeout=10,
        )
        if ref_check.returncode != 0:
            checkout_ref = 'FETCH_HEAD' if pr_ref_fetched else pr.source_commit

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


PTY_POLL_INTERVAL = 2
# The dialog title ("Do you trust the files in this folder?") arrives with ANSI/box
# styling interleaved, so it never matches as a contiguous substring — match on the
# plain-text body/option lines, which stream through clean.
_PTY_TRUST_RE = (
    r'(?i)('
    r'do you trust|'
    r'trust the files in this|'
    r'yes, i trust this folder|'
    r"what's in this folder first"
    r')'
)
# Specific CLI error banners only — bare phrases like "rate limit" would false-match
# against Claude's own review prose / code it quotes, aborting valid reviews.
_PTY_ERROR_RE = (
    r'(?i)(' r'usage limit reached|' r'5-hour limit reached|' r'credit balance is too low|' r'please run /login' r')'
)


def _terminate_pty(child) -> None:
    with contextlib.suppress(Exception):
        os.killpg(child.pid, signal.SIGTERM)
    with contextlib.suppress(Exception):
        child.terminate(force=True)


def _pretrust_directory(cwd: str) -> None:
    """Mark cwd as trusted in ~/.claude.json so the interactive first-run trust dialog
    never fires. Best-effort: the broadened PTY matcher is the fallback if this fails."""
    config_path = Path.home() / '.claude.json'
    try:
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(config, dict):
        return
    projects = config.setdefault('projects', {})
    if not isinstance(projects, dict):
        return
    entry = projects.setdefault(os.path.abspath(cwd), {})
    if not isinstance(entry, dict):
        return
    entry['hasTrustDialogAccepted'] = True
    entry['hasCompletedProjectOnboarding'] = True
    tmp = config_path.with_suffix('.json.tmp')
    try:
        tmp.write_text(json.dumps(config, indent=2))
        tmp.replace(config_path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _read_review_file(output_path: str) -> str | None:
    """Return the review file's contents once it exists and is non-empty, else None."""
    path = Path(output_path)
    if not path.exists():
        return None
    content = path.read_text()
    return content if content.strip() else None


def _invoke_claude_interactive(
    prompt: str,
    cwd: str,
    output_path: str,
    timeout: int = DEFAULT_TIMEOUT,
    model: str | None = None,
    cli_args: list[str] | None = None,
) -> str:
    import pexpect

    # Hermetic, hardened interactive session:
    # - skip-permissions: tools run unattended (no one to answer prompts)
    # - disallow Edit: the reviewer only creates the output file, never edits existing ones
    #   (Write + Bash must stay: Write produces the JSON, Bash runs git/tests + the mv)
    # - empty + strict MCP config: ignore the user's global MCP servers for a clean env
    args = [
        '--dangerously-skip-permissions',
        '--disallowedTools',
        'Edit',
        '--mcp-config',
        '{"mcpServers":{}}',
        '--strict-mcp-config',
    ]
    if model:
        args += ['--model', model]
    if cli_args:
        args += cli_args
    # Full prompt as a single positional argv element (no shell, like `-p`); interactive
    # Claude accepts a multi-line positional prompt and submits it as one message.
    args.append(prompt)

    env = {**os.environ}
    env.pop('CLAUDECODE', None)

    _pretrust_directory(cwd)

    display_args = [*args[:-1], '<prompt>']
    logger.info('Running interactive: claude %s (cwd=%s, timeout=%ds)', ' '.join(display_args), cwd, timeout)
    logger.debug('Prompt:\n%s', prompt)

    child = None
    try:
        try:
            child = pexpect.spawn('claude', args, cwd=cwd, env=env, encoding='utf-8', dimensions=(40, 140))
        except pexpect.ExceptionPexpect as e:
            raise RuntimeError(f'"claude" CLI not found. {_CLI_NOT_FOUND_HINTS[CLI.CLAUDE]}') from e
        pid = child.pid
        if pid is not None:
            with _active_pty_pids_lock:
                _active_pty_pids.add(pid)

        patterns = [_PTY_TRUST_RE, _PTY_ERROR_RE, pexpect.EOF, pexpect.TIMEOUT]
        deadline = time.monotonic() + timeout
        result = None
        while result is None:
            result = _read_review_file(output_path)
            if result is not None:
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f'claude_interactive timed out after {timeout}s with no output file')
            idx = child.expect(patterns, timeout=PTY_POLL_INTERVAL)
            if idx == 0:
                logger.debug('[claude_interactive] trust dialog, accepting default')
                child.sendline('')
            elif idx == 1:
                after = child.after if isinstance(child.after, str) else ''
                tail = (child.before or '')[-500:] + after
                raise RuntimeError(f'claude_interactive error: {tail.strip()}')
            elif idx == 2:
                result = _read_review_file(output_path)
                if result is not None:
                    break
                tail = (child.before or '')[-500:]
                raise RuntimeError(f'claude exited before writing output: {tail.strip()}')

        logger.info('Read output from PTY review file (%d chars)', len(result))
        return result
    finally:
        if child is not None and child.pid is not None:
            with _active_pty_pids_lock:
                _active_pty_pids.discard(child.pid)
            _terminate_pty(child)
        Path(output_path).unlink(missing_ok=True)
        Path(output_path + '.tmp').unlink(missing_ok=True)


def invoke_cli(
    prompt: str,
    cwd: str,
    cli: CLI = CLI.CLAUDE,
    timeout: int = DEFAULT_TIMEOUT,
    model: str | None = None,
    cli_args: list[str] | None = None,
    cli_defaults: dict[CLI, list[str]] | None = None,
    output_path: str | None = None,
) -> str:
    if cli == CLI.CLAUDE_INTERACTIVE:
        if not output_path:
            raise ValueError('output_path is required for claude_interactive mode')
        return _invoke_claude_interactive(prompt, cwd, output_path, timeout=timeout, model=model, cli_args=cli_args)

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
            stdout = proc.stdout.read() if proc.stdout else ''
            proc.wait(timeout=timeout)
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
            if stdout.strip():
                logger.error('%s stdout: %s', cli.value, stdout[:500])
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
    # strict=False permits literal control characters (newlines, tabs, CR) inside
    # string values — a common LLM failure mode when emitting multi-line `issue`
    # or `fix` fields without escaping.
    try:
        return json.loads(raw, strict=False)
    except json.JSONDecodeError:
        # Strip trailing commas before } or ] (common LLM JSON error) and retry
        fixed = re.sub(r',\s*([}\]])', r'\1', raw)
        try:
            logger.warning('Fixed trailing commas in AI JSON output')
            return json.loads(fixed, strict=False)
        except json.JSONDecodeError as e:
            # Dump the full AI output so the review can be salvaged manually
            # rather than throwing away the entire (paid-for) call.
            dump_path = Path(tempfile.gettempdir()) / f'reviewd-failed-{int(time.time())}.txt'
            try:
                dump_path.write_text(output)
                logger.error('Malformed JSON in AI output: %s. Full output saved to %s', e, dump_path)
            except OSError:
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
    output_path = None
    output_name = None
    if cli == CLI.CLAUDE_INTERACTIVE:
        # The prompt references the file by basename (cwd-relative) so the mv/Write
        # instructions carry no user-controlled path; reviewd polls the absolute path.
        output_name = '.reviewd-review.json'
        output_path = os.path.join(worktree_path, output_name)
        Path(output_path).unlink(missing_ok=True)
    try:
        prompt = build_review_prompt(pr, project_config, output_file=output_name)
        t0 = time.monotonic()
        output = invoke_cli(
            prompt,
            worktree_path,
            cli=cli,
            timeout=timeout,
            model=model,
            cli_args=cli_args,
            cli_defaults=cli_defaults,
            output_path=output_path,
        )
        elapsed = time.monotonic() - t0
        logger.info('AI review completed in %.1fs', elapsed)
        logger.debug('Extracting JSON from AI output (%d chars)', len(output))
        data = extract_json(output)
        logger.debug('Parsed %d findings', len(data.get('findings', [])))
        result = parse_review_result(data)
        result.duration_seconds = elapsed
        logger.info('Review has %d findings', len(result.findings))
        return result
    finally:
        logger.debug('Cleaning up worktree for PR #%d', pr.pr_id)
        cleanup_worktree(repo_path, pr)
        logger.debug('Worktree cleanup done')
