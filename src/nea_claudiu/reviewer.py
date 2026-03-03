from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from nea_claudiu.models import (
    AICli,
    Finding,
    PRInfo,
    ProjectConfig,
    ReviewResult,
    Severity,
)
from nea_claudiu.prompt import build_review_prompt

logger = logging.getLogger(__name__)

JSON_BLOCK_PATTERN = re.compile(r'```json\s*\n(.*?)\n\s*```', re.DOTALL)
DEFAULT_TIMEOUT = 600


def cleanup_stale_worktrees(repo_path: str):
    worktree_root = Path(repo_path) / '.nea-claudiu-worktrees'
    if not worktree_root.exists():
        return
    for entry in worktree_root.iterdir():
        if entry.is_dir():
            result = subprocess.run(
                ['git', 'worktree', 'remove', str(entry), '--force'],
                cwd=repo_path,
                capture_output=True,
            )
            if result.returncode == 0:
                logger.info('Cleaned up stale worktree: %s', entry.name)
            else:
                logger.warning('Failed to clean worktree %s: %s', entry.name, result.stderr.decode().strip())


def create_worktree(repo_path: str, pr: PRInfo) -> str:
    worktree_dir = Path(repo_path) / '.nea-claudiu-worktrees' / f'pr-{pr.pr_id}'
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    if worktree_dir.exists():
        cleanup_worktree(repo_path, pr)

    # Try fetching branch by name first; fall back to destination only
    # (source branch may have been deleted after merge)
    fetch_result = subprocess.run(
        ['git', 'fetch', 'origin', pr.source_branch, pr.destination_branch],
        cwd=repo_path,
        capture_output=True,
    )
    if fetch_result.returncode != 0:
        logger.warning('Source branch fetch failed, fetching destination only')
        subprocess.run(
            ['git', 'fetch', 'origin', pr.destination_branch],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

    # Use branch ref if available, otherwise the commit hash (must exist locally)
    checkout_ref = f'origin/{pr.source_branch}'
    ref_check = subprocess.run(
        ['git', 'rev-parse', '--verify', checkout_ref],
        cwd=repo_path,
        capture_output=True,
    )
    if ref_check.returncode != 0:
        checkout_ref = pr.source_commit

    subprocess.run(
        ['git', 'worktree', 'add', str(worktree_dir), checkout_ref, '--detach'],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    logger.info('Created worktree at %s', worktree_dir)
    return str(worktree_dir)


def cleanup_worktree(repo_path: str, pr: PRInfo):
    worktree_dir = Path(repo_path) / '.nea-claudiu-worktrees' / f'pr-{pr.pr_id}'
    if worktree_dir.exists():
        subprocess.run(
            ['git', 'worktree', 'remove', str(worktree_dir), '--force'],
            cwd=repo_path,
            capture_output=True,
        )
        logger.info('Cleaned up worktree at %s', worktree_dir)


def _build_cli_command(ai_cli: AICli, prompt_file: str) -> list[str]:
    prompt_arg = Path(prompt_file).read_text()
    if ai_cli == AICli.CLAUDE:
        return ['claude', '--print', '-p', prompt_arg]
    if ai_cli == AICli.GEMINI:
        return ['gemini', '-p', prompt_arg]
    raise ValueError(f'Unknown AI CLI: {ai_cli}')


def invoke_ai_cli(
    prompt: str,
    cwd: str,
    ai_cli: AICli = AICli.CLAUDE,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write(prompt)
        prompt_file = f.name

    try:
        cmd = _build_cli_command(ai_cli, prompt_file)
        logger.info('Running %s review in %s (timeout=%ds)', ai_cli.value, cwd, timeout)
        env = {**os.environ}
        env.pop('CLAUDECODE', None)
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise
        if proc.returncode != 0:
            logger.error('%s stderr: %s', ai_cli.value, stderr)
            raise RuntimeError(f'{ai_cli.value} exited with code {proc.returncode}: {stderr}')
        return stdout
    finally:
        Path(prompt_file).unlink(missing_ok=True)


def extract_json(output: str) -> dict:
    matches = JSON_BLOCK_PATTERN.findall(output)
    if not matches:
        raise ValueError('No JSON block found in AI output')
    raw = matches[-1]
    return json.loads(raw)


def parse_review_result(data: dict) -> ReviewResult:
    findings = []
    for f in data.get('findings', []):
        findings.append(Finding(
            severity=Severity(f['severity']),
            category=f.get('category', 'General'),
            title=f.get('title', ''),
            file=f.get('file', ''),
            line=f.get('line'),
            issue=f.get('issue', ''),
            fix=f.get('fix'),
        ))
    return ReviewResult(
        overview=data.get('overview', ''),
        findings=findings,
        summary=data.get('summary', ''),
        tests_passed=data.get('tests_passed'),
    )


def review_pr(
    repo_path: str,
    pr: PRInfo,
    project_config: ProjectConfig,
    ai_cli: AICli = AICli.CLAUDE,
    timeout: int = DEFAULT_TIMEOUT,
) -> ReviewResult:
    worktree_path = create_worktree(repo_path, pr)
    try:
        prompt = build_review_prompt(pr, project_config)
        output = invoke_ai_cli(prompt, worktree_path, ai_cli=ai_cli, timeout=timeout)
        data = extract_json(output)
        return parse_review_result(data)
    finally:
        cleanup_worktree(repo_path, pr)
