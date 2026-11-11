"""Measure real target instructions with offline lifecycle contexts; no model or GitHub writes."""

import argparse
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / 'engine'), str(ROOT / 'engine/tests')]

from agent_entry import prompt_for
from bundle import Bundle
from controller import Controller
from support import transport_size
from fakes import FINDING, FakeAgents, FakeGitHub
from model_config import resolve
from service.target_config import read_local


class MeasurementAgents(FakeAgents):
    """Capture real controller contexts through one synthetic implementation repair."""

    def __init__(self, source):
        """Use the pinned target's actual source line for the fixture citation."""
        super().__init__()
        self.source, self.requested_fix = source, False

    def ask(self, provider, stage, context):
        """Return deterministic fixture reports with no inference or external publication."""
        report = super().ask(provider, stage, context)
        if stage == 'plan_qc':
            report['sources'][0]['quote'] = self.source
        if stage == 'review' and not self.requested_fix:
            self.requested_fix = True
            report.update(verdict='changes_requested', findings=[deepcopy(FINDING)])
        return report


def measure(target):
    """Replay a small task and a near-capacity context for every configured stage."""
    config = read_local(target / '.concurro/config.yaml')
    bundle = Bundle(ROOT, target)
    github = FakeGitHub(config)
    source = (target / 'CONTRIBUTING.md').read_text()
    github.trees[github.base].update({path: (target / path).read_text() for path in config['policy_documents']})
    agents = MeasurementAgents(source.splitlines()[0])
    for _ in range(40):
        controller = Controller(github, agents, config, ROOT, target)
        controller.advance(7)
        if controller.state['stage'] in ('gates', 'blocked', 'escalated'):
            break
    if controller.state['stage'] != 'gates':
        raise RuntimeError(controller.state.get('reason', controller.state['stage']))
    rows = []
    for provider, stage, context in agents.calls:
        context = {k: v for k, v in context.items() if not k.startswith('controller_')}
        request = {
            'provider': resolve(config, provider, stage).profile.harness,
            'stage': stage,
            'context': context,
            'schema': bundle.schema(stage),
            'workspace': '/runner/workspace',
        }
        if any(v.get('feedback_file') for v in context.values() if isinstance(v, dict)):
            request['feedback_directory'] = '/runner/feedback'
        for scenario in ('lifecycle', 'near-limit'):
            current = deepcopy(request)
            if scenario == 'near-limit':
                current['context']['measurement_padding'] = ''
                current['context']['measurement_padding'] = 'x' * max(0, config['max_context_bytes'] - transport_size(current['context']))
            old = transport_size(current['context'])
            full = len(prompt_for(current, bundle).encode())
            rows.append({'stage': stage, 'profile': provider, 'scenario': scenario, 'context_bytes': old,
                         'prompt_bytes': full, 'additional_bytes': full-old,
                         'headroom_bytes': config['max_context_bytes']-full})
    files = sorted(p for p in target.rglob('*') if p.is_file())
    return {'target_commit': 'e296939a775a9934f4da1a1f11d566b938aa8c25',
            'target_repository': 'concurro-demo-fork/stacks-core', 'limit': config['max_context_bytes'],
            'method': 'Real pinned target authority; synthetic guide task, review finding and repair. Near-limit padding is synthetic, not observed live traffic. CLI-added framing and retained history are not measured.',
            'files': {str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
            'bundle': bundle.identity, 'measurements': rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(measure(args.target), indent=2) + '\n')
