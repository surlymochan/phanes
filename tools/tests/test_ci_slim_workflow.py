import itertools
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def test_phanes_matrix_and_concurrency():
    text = (ROOT / '.github/workflows/phanes.yml').read_text()
    block = text.split('  swrm:\n', 1)[1].split('  volto:\n', 1)[0]
    matrices = re.findall(r"fromJSON\('([^']+)'\)", block)
    pr, other = map(json.loads, matrices)
    assert {(x['os'], x['python']) for x in pr['include']} == {('macos-latest', '3.9'), ('ubuntu-24.04', '3.12')}
    assert len(pr['include']) == 2
    assert set(itertools.product(other['os'], other['python'])) == set(itertools.product(['ubuntu-24.04', 'macos-latest'], ['3.9', '3.12', '3.14']))
    assert 'phanes-${{ github.event.pull_request.number || github.ref }}' in text
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in text


def test_nightly():
    text = (ROOT / '.github/workflows/nightly.yml').read_text()
    assert 'schedule:' in text and '20 * * *' in text and 'workflow_dispatch:' in text
    assert 'os: [ubuntu-24.04, macos-latest]' in text
    assert 'python: ["3.9", "3.12", "3.14"]' in text
    assert 'if: failure()' in text and 'issues: write' in text
    assert 'needs: swrm' in text and 'listJobsForWorkflowRun' in text
    assert 'createComment' in text and 'issues.create' in text


def test_parallel_pytest():
    text = (ROOT / 'tools/ci-swrm.sh').read_text()
    assert 'pytest-xdist' in text and '-n auto' in text


# ---- behaviour of the failure-report script (run under node with a fake GitHub client) ----

HARNESS = """
const body = require('fs').readFileSync(0, 'utf8');
const fx = JSON.parse(body);
const calls = {created: [], commented: []};
const pages = {listJobsForWorkflowRun: fx.jobPages, listForRepo: fx.issuePages};
const github = {
  rest: {
    actions: {listJobsForWorkflowRun: 'listJobsForWorkflowRun'},
    issues: {
      listForRepo: 'listForRepo',
      createComment: async (a) => { calls.commented.push(a); },
      create: async (a) => { calls.created.push(a); },
    },
  },
  // like octokit: walk every page (empty pages included) and concatenate
  paginate: async (fn, params) => (pages[fn] || []).flat(),
};
const context = {repo: {owner: 'o', repo: 'r'}, runId: 42, serverUrl: 'https://example.invalid'};
const run = new (Object.getPrototypeOf(async function () {}).constructor)('github', 'context', SCRIPT);
run(github, context).then(() => console.log(JSON.stringify(calls)));
"""


def _script():
    text = (ROOT / '.github/workflows/nightly.yml').read_text()
    block = text.split('script: |\n', 1)[1]
    lines = block.splitlines()
    indent = len(lines[0]) - len(lines[0].lstrip())
    return textwrap.dedent('\n'.join(l for l in lines if l.strip() == '' or len(l) - len(l.lstrip()) >= indent))


def _run(job_pages, issue_pages):
    node = shutil.which('node')
    if not node:
        pytest.skip('node not available')
    code = HARNESS.replace('SCRIPT', json.dumps(_script()))
    out = subprocess.run([node, '-e', code], input=json.dumps({'jobPages': job_pages, 'issuePages': issue_pages}),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _title(calls_or_body):
    return calls_or_body


def _today_title():
    import datetime
    return 'Nightly 全矩阵失败 ' + datetime.datetime.utcnow().strftime('%Y-%m-%d')


def job(name, conclusion):
    return {'name': name, 'conclusion': conclusion}


def test_report_collects_failed_jobs_across_pages_and_skips_empty_page():
    calls = _run([[job('a', 'failure'), job('ok', 'success')], [], [job('b', 'failure'), job('c', 'cancelled')]], [[]])
    assert len(calls['created']) == 1 and not calls['commented']
    body = calls['created'][0]['body']
    assert '- a' in body and '- b' in body and '- ok' not in body and '- c' not in body


def test_report_with_no_failed_jobs_still_reports_without_crash():
    calls = _run([[], [job('ok', 'success')]], [[]])
    assert len(calls['created']) == 1 and '- ' not in calls['created'][0]['body'].split('失败的 job:')[1]


def test_existing_issue_on_later_page_gets_comment_not_duplicate():
    t = _today_title()
    calls = _run([[job('x', 'failure')]], [[{'number': 1, 'title': 'other'}], [], [{'number': 7, 'title': t}]])
    assert not calls['created'] and [c['issue_number'] for c in calls['commented']] == [7]


def test_pull_request_with_same_title_is_not_treated_as_the_issue():
    t = _today_title()
    calls = _run([[job('x', 'failure')]], [[{'number': 9, 'title': t, 'pull_request': {}}]])
    assert len(calls['created']) == 1 and not calls['commented']
