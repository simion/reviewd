from nea_claudiu.providers.github import GithubProvider
from nea_claudiu.models import GithubConfig

def test_github_pr_from_data():
    config = GithubConfig(token='fake-token')
    provider = GithubProvider(config)
    
    data = {
        'number': 42,
        'title': 'Test PR',
        'user': {'login': 'alice'},
        'head': {'ref': 'feature', 'sha': 'abc123'},
        'base': {'ref': 'main'},
        'html_url': 'https://github.com/owner/repo/pull/42'
    }
    
    pr = provider._pr_from_data('owner/repo', data)
    
    assert pr.pr_id == 42
    assert pr.title == 'Test PR'
    assert pr.author == 'alice'
    assert pr.source_branch == 'feature'
    assert pr.destination_branch == 'main'
    assert pr.source_commit == 'abc123'
    assert pr.repo_slug == 'owner/repo'
