from __future__ import annotations

from nea_claudiu.models import PRInfo, ProjectConfig

REVIEW_TEMPLATE = '''\
You are reviewing pull request #{pr_id}: "{pr_title}" by {pr_author}.
Branch: {branch} → {destination}
Source commit: {source_commit}

## Your Task
Perform a thorough code review of this pull request.

1. Compute the diff: run `git merge-base origin/{destination} HEAD`, then `git diff <merge-base>..HEAD`
2. Read the changed files in full to understand surrounding context
3. Explore related code (how changed functions are used, related models/views/utilities)
{validation_section}\
4. Review the changes for correctness, security, performance, architecture, and maintainability

## Severity Definitions
- critical: Must fix before merge. Bugs, security issues, data loss, crashes.
- suggestion: Should fix. Performance, maintainability, convention violations.
- nitpick: Optional. Minor style, alternative approaches.
- good: Praise. Well-written code, good patterns worth highlighting.
{instructions_section}\

## Output
After completing your review, output EXACTLY this JSON block as the last thing in your response:
```json
{{
  "overview": "2-3 sentence high-level assessment",
  "findings": [
    {{
      "severity": "critical|suggestion|nitpick|good",
      "category": "Security|Performance|Logic|Style|Architecture|...",
      "title": "brief title",
      "file": "path/to/file.py",
      "line": 42,
      "issue": "explanation",
      "fix": "suggested code fix or null"
    }}
  ],
  "summary": "prioritized recommendations",
  "tests_passed": true|false|null
}}
```\
'''


def build_review_prompt(
    pr: PRInfo,
    project_config: ProjectConfig,
    changed_files: list[str] | None = None,
) -> str:
    step = 5
    validation_section = ''
    if project_config.test_commands:
        commands = project_config.test_commands
        if changed_files:
            files_str = ' '.join(changed_files)
            commands = [cmd.replace('{changed_files}', files_str) for cmd in commands]
        commands_str = '\n   '.join(commands)
        validation_section = f'{step}. Run validation:\n   {commands_str}\n'

    instructions_section = ''
    if project_config.instructions:
        instructions_section = f'\n## Project Instructions\n{project_config.instructions}\n'

    return REVIEW_TEMPLATE.format(
        pr_id=pr.pr_id,
        pr_title=pr.title,
        pr_author=pr.author,
        branch=pr.source_branch,
        destination=pr.destination_branch,
        source_commit=pr.source_commit,
        validation_section=validation_section,
        instructions_section=instructions_section,
    )
