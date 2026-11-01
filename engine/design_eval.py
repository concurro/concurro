"""Evaluate fresh design reviewers against isolated fixtures, without GitHub writes."""

import argparse
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tarfile

from agents import Agents
from bundle import Bundle
from checks import catalog, contract_errors
from design import criteria, validate_evidence, report_limit
from policy import digest, validate_qc

SOURCE = {
    'docs/workflow.md': '# Workflow\n    Only repository writers enroll issues with the concurro label.\n'
    'Pilot invocations are manual; applying the label does not start execution.\n'
    'Autonomous merging is disabled throughout this supervised pilot.\n'
    'Local Git checkpoints precede checks; remote publication waits for checks and review.\n'
    'Acceptance criteria may require runtime tests.\n' + ('Unrelated fixture context.\n' * 6000)
}
REQUIREMENTS = {'title': 'Document the supervised workflow', 'body': 'Acceptance criteria:\n\n' + '\n'.join([
    '- Explain that only repository writers enroll issues.',
    '- Name the concurro enrollment label.',
    '- Explain that pilot invocations are manual.',
    '- Explain that applying the label does not start execution.',
    '- State that autonomous merging stays disabled.',
    '- Put local Git checkpoints before checks.',
    '- Put remote publication after checks.',
    '- Put remote publication after review.',
    '- Change only docs/guide.md.',
    '- Create docs/guide.md as a new file.',
    '- Keep the guide to at most 99 lines.',
    '- Compare every workflow claim against docs/workflow.md during review.',
]), 'discussion': [{'body': 'Thanks!'}]}



def cases():
    """Return a valid design and single-defect mutations with labels kept outside model context."""
    plan = {'summary': 'Document the supervised workflow.', 'questions': [], 'files': ['docs/guide.md'],
            'steps': ['Create docs/guide.md explaining that only writers enroll issues with the concurro label; applying the label does not start execution; pilot invocations are manual; '
                      'automatic merging stays disabled; local checkpoints precede checks, and remote publication waits for checks and review.'],
            'tests': ['Compare every workflow claim against docs/workflow.md.'], 'responses': [],
            'validation': {'checks': ['diff', 'scope', 'text'], 'files': [
                {'path': 'docs/guide.md', 'change': 'add', 'max_lines': 99, 'required_text': []}]},
            'publication': {'changes': ['Add the requested workflow guide.'], 'review_focus': ['Check workflow accuracy.']}}
    items = [{'id': 'valid', 'expected': 'approve', 'plan': plan}]
    defects = {
        'automatic-intake': ('applying the label does not start execution', 'applying the label automatically starts intake'),
        'automatic-merge': ('automatic merging stays disabled', 'the controller automatically merges pilot PRs after checks'),
        'commit-order': ('local checkpoints precede checks', 'no local commit is created until checks pass'),
    }
    for name, (before, after) in defects.items():
        candidate = deepcopy(plan)
        candidate['steps'] = [plan['steps'][0].replace(before, after)]
        items.append({'id': name, 'expected': 'revise', 'plan': candidate})
    return items


def archive():
    """Expose only the evaluation's source fixture through normal repository inspection tools."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as handle:
        for name, text in SOURCE.items():
            data = text.encode()
            item = tarfile.TarInfo('fixture/' + name)
            item.size = len(data)
            handle.addfile(item, io.BytesIO(data))
    return output.getvalue()


def request(case, provider, config):
    """Build a fresh review input without expected labels, peer votes or prior findings."""
    bundle = Bundle(profiles=config['execution_profiles'])
    return {'issue': REQUIREMENTS, 'base': 'e' * 40, 'repository_commit': 'e' * 40,
            'bundle_id': bundle.identity, 'charter': bundle.charter, 'review_phase': 'fresh',
            'reviewer_identity': {'id': digest([provider, config['models'][provider]]),
                                  'provider': provider, 'model': config['models'][provider], 'roles': ['review']},
            'review_criteria': criteria(REQUIREMENTS),
            'review_output_budget': {'max_json_bytes': report_limit(len(criteria(REQUIREMENTS)))},
            'validation_capabilities': catalog(case['plan']['files'], bundle.execution),
            'plan': {k: v for k, v in case['plan'].items() if k not in ('summary', 'publication', 'responses')}}


def score(results):
    """Report missed defects and false alarms separately from invalid or failed responses."""
    expected = {case['id']: case['expected'] for case in cases()}
    if {r['id'] for r in results} != set(expected) or len(results) != len(expected):
        raise ValueError('evaluation must contain every case exactly once')
    return {'cases': len(results), 'invalid': sum(bool(r.get('error')) for r in results),
            'missed_defects': sum(not r.get('error') and expected[r['id']] == 'revise' and r['decision'] == 'approve' for r in results),
            'false_alarms': sum(not r.get('error') and expected[r['id']] == 'approve' and r['decision'] != 'approve' for r in results),
            'correct': sum(not r.get('error') and r['decision'] == expected[r['id']] for r in results)}


def main():
    """Run explicitly selected model evaluations with existing inference-only credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=('claude', 'codex', 'copilot'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    config = json.loads(Path('.concurro/config.json').read_text())
    agents = Agents(config, archive_loader=lambda _: archive())
    results = []
    for case in cases():
        if contract_errors(case['plan'], agents.bundle.execution):
            raise ValueError('evaluation fixture has an invalid executable contract')
        item = {'id': case['id'], 'provider': args.provider, 'model': config['models'][args.provider],
                'bundle_id': agents.bundle.identity, 'agent_image': os.environ.get('CONCURRO_AGENT_IMAGE'),
                'fixture_digest': digest([SOURCE, REQUIREMENTS, case['plan']])}
        try:
            report = agents.ask(args.provider, 'plan_qc', request(case, args.provider, config))
            item['report'] = report
            validate_qc(report, 'plan_qc', agents.bundle.charter, len(criteria(REQUIREMENTS)))
            validate_evidence(report, REQUIREMENTS, files=SOURCE)
            item.update(decision=report['decision'], report=report)
        except Exception as error:
            item['error'] = type(error).__name__
        results.append(item)
        (args.output / (case['id'] + '.json')).write_text(json.dumps(item, indent=2))
        print(json.dumps({k: v for k, v in item.items() if k != 'report'}), flush=True)
    summary = score(results)
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary))
    return int(summary['correct'] != summary['cases'])


if __name__ == '__main__':
    raise SystemExit(main())
