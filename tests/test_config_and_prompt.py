"""Config loading → prompt assembly: real YAML files, real prompt builder."""

from __future__ import annotations

import pytest

from reviewd.config import _merge_auto_approve, load_global_config, load_project_config
from reviewd.models import AutoApproveConfig, GlobalConfig, ProjectConfig
from reviewd.prompt import build_review_prompt

# ---------------------------------------------------------------------------
# Config loading from real YAML
# ---------------------------------------------------------------------------


def test_load_global_config_valid(tmp_path):
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(
        """
bitbucket:
  myteam: fake-token-123
cli: claude
poll_interval_seconds: 30
repos:
  - name: my-repo
    path: /tmp/repo
    provider: bitbucket
    workspace: myteam
"""
    )
    config = load_global_config(cfg)
    assert config.cli.value == 'claude'
    assert config.poll_interval_seconds == 30
    assert len(config.repos) == 1
    assert config.repos[0].name == 'my-repo'
    assert config.bitbucket['myteam'] == 'fake-token-123'


def test_load_global_config_malformed_yaml(tmp_path):
    cfg = tmp_path / 'bad.yaml'
    cfg.write_text('  bad:\n yaml: [')
    with pytest.raises(SystemExit, match='Invalid YAML'):
        load_global_config(cfg)


def test_load_global_config_not_a_dict(tmp_path):
    cfg = tmp_path / 'list.yaml'
    cfg.write_text('- item1\n- item2')
    with pytest.raises(SystemExit, match='expected a YAML mapping'):
        load_global_config(cfg)


def test_load_global_config_missing_repo_field(tmp_path):
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(
        """
repos:
  - name: oops
    path: /tmp/repo
"""
    )
    with pytest.raises(SystemExit, match='missing required field "provider"'):
        load_global_config(cfg)


def test_load_project_config_merges_instructions(tmp_path):
    project_yaml = tmp_path / '.reviewd.yaml'
    project_yaml.write_text(
        """
instructions: "Be strict about types."
test_commands:
  - uv run pytest
inline_comments_for: [critical, suggestion]
"""
    )
    global_config = GlobalConfig(
        repos=[],
        instructions='Always be constructive.',
    )
    config = load_project_config(tmp_path, global_config)
    assert 'Always be constructive' in config.instructions
    assert 'Be strict about types' in config.instructions
    assert config.test_commands == ['uv run pytest']
    assert 'suggestion' in config.inline_comments_for


def test_auto_approve_merge_stricter_wins():
    global_aa = AutoApproveConfig(enabled=True, max_diff_lines=100, max_severity='suggestion', max_findings=5)
    project_aa = AutoApproveConfig(enabled=True, max_diff_lines=50, max_severity='nitpick', max_findings=10)
    merged = _merge_auto_approve(global_aa, project_aa)
    assert merged.enabled is True
    assert merged.max_diff_lines == 50  # min
    assert merged.max_severity == 'nitpick'  # stricter
    assert merged.max_findings == 5  # min


def test_auto_approve_legacy_compat():
    merged = _merge_auto_approve(None, None, legacy_approve_if_no_critical=True)
    assert merged.enabled is True
    assert merged.max_severity == 'suggestion'


# ---------------------------------------------------------------------------
# Config → prompt assembly
# ---------------------------------------------------------------------------


def test_prompt_includes_pr_metadata(pr, project_config):
    prompt = build_review_prompt(pr, project_config)
    assert f'#{pr.pr_id}' in prompt
    assert pr.title in prompt
    assert pr.author in prompt
    assert pr.source_branch in prompt
    assert pr.destination_branch in prompt


def test_prompt_includes_test_commands(pr):
    config = ProjectConfig(test_commands=['uv run pytest', 'uv run ruff check .'])
    prompt = build_review_prompt(pr, config)
    assert 'uv run pytest' in prompt
    assert 'uv run ruff check .' in prompt


def test_prompt_includes_approve_section_with_rules(pr):
    config = ProjectConfig(
        auto_approve=AutoApproveConfig(enabled=True, rules='Only approve docs changes.'),
    )
    prompt = build_review_prompt(pr, config)
    assert 'Auto-Approve Decision' in prompt
    assert 'Only approve docs changes.' in prompt


def test_prompt_no_approve_section_when_disabled(pr, project_config):
    prompt = build_review_prompt(pr, project_config)
    assert 'Auto-Approve Decision' not in prompt


def test_prompt_skips_severities(pr):
    config = ProjectConfig(skip_severities=['nitpick', 'good'])
    prompt = build_review_prompt(pr, config)
    assert 'nitpick' in prompt.lower()  # mentioned in "Do NOT include"
    assert 'good' in prompt.lower()
    # But their definitions should not appear
    assert 'Optional. Minor style' not in prompt
    assert 'Praise. Well-written' not in prompt


def test_prompt_includes_instructions(pr):
    config = ProjectConfig(instructions='Focus on security above all else.')
    prompt = build_review_prompt(pr, config)
    assert 'Focus on security above all else.' in prompt
