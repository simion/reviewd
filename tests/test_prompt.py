from nea_claudiu.models import PRInfo, ProjectConfig
from nea_claudiu.prompt import build_review_prompt


def _make_pr(**overrides) -> PRInfo:
    defaults = {
        'repo_slug': 'test-repo',
        'pr_id': 42,
        'title': 'Add feature X',
        'author': 'alice',
        'source_branch': 'feature/x',
        'destination_branch': 'master',
        'source_commit': 'abc123',
        'url': 'https://bitbucket.org/ws/test-repo/pull-requests/42',
    }
    defaults.update(overrides)
    return PRInfo(**defaults)


def test_basic_prompt():
    pr = _make_pr()
    config = ProjectConfig()
    prompt = build_review_prompt(pr, config)

    assert 'pull request #42' in prompt
    assert '"Add feature X"' in prompt
    assert 'alice' in prompt
    assert 'origin/master' in prompt
    assert 'Source commit: abc123' in prompt
    assert '```json' in prompt


def test_instructions_injected():
    pr = _make_pr()
    config = ProjectConfig(instructions='Use single quotes\nRead AGENTS.md first')
    prompt = build_review_prompt(pr, config)

    assert '## Project Instructions' in prompt
    assert 'Use single quotes' in prompt
    assert 'Read AGENTS.md first' in prompt


def test_test_commands_injected():
    pr = _make_pr()
    config = ProjectConfig(test_commands=['uv run ruff check .'])
    prompt = build_review_prompt(pr, config)

    assert 'Run validation' in prompt
    assert 'uv run ruff check .' in prompt


def test_changed_files_substitution():
    pr = _make_pr()
    config = ProjectConfig(test_commands=['python -m py_compile {changed_files}'])
    prompt = build_review_prompt(pr, config, changed_files=['a.py', 'b.py'])

    assert 'python -m py_compile a.py b.py' in prompt


def test_empty_config_no_extra_sections():
    pr = _make_pr()
    config = ProjectConfig()
    prompt = build_review_prompt(pr, config)

    assert '## Project Instructions' not in prompt
    assert 'Run validation' not in prompt
