from __future__ import annotations

import json

from reviewd.models import Finding, ReviewResult, Severity


def make_finding(
    severity='suggestion',
    title='Test finding',
    file='src/main.py',
    line=10,
    issue='Something is wrong',
    fix=None,
):
    return Finding(
        severity=Severity(severity),
        category='General',
        title=title,
        file=file,
        line=line,
        end_line=None,
        issue=issue,
        fix=fix,
    )


def make_result(findings=None, approve=False, approve_reason=None, summary='All good'):
    return ReviewResult(
        overview='Overview',
        findings=findings or [],
        summary=summary,
        approve=approve,
        approve_reason=approve_reason,
    )


AI_JSON_OUTPUT = json.dumps(
    {
        'overview': 'Clean PR with minor issues',
        'findings': [
            {
                'severity': 'suggestion',
                'category': 'Style',
                'title': 'Use f-string',
                'file': 'src/main.py',
                'line': 10,
                'issue': 'Consider using f-string instead of format()',
                'fix': "name = f'hello {world}'",
            },
            {
                'severity': 'critical',
                'category': 'Security',
                'title': 'SQL injection',
                'file': 'src/db.py',
                'line': 25,
                'issue': 'Raw string interpolation in query',
                'fix': None,
            },
        ],
        'summary': 'Fix the SQL injection, rest is fine',
        'tests_passed': True,
        'approve': False,
        'approve_reason': 'Critical security issue found',
    }
)
