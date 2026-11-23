"""Local v2 runner integration helper used by Rust's real-listener test."""

import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import runner
import turns
from gitfixture import Upstream
from mirror import Mirror
from relay import RunnerV2


def prepare(root):
    """Create a real local Git source for the native turn."""
    root.mkdir(parents=True)
    upstream = Upstream(root / "upstream.git")
    sha = upstream.commit(
        {"docs/guide.md": "Guide\n", "src/lib.rs": "fn main() {}\n", ".gitignore": "target/\n"},
        branch="main",
    )
    retained = root / "session.txt"
    retained.write_text("previous turn\n")
    with tarfile.open(root / "retained-input.tar.gz", "w:gz") as bundle:
        bundle.add(retained, arcname="session.txt")
    print(sha)


def serve(root, base, session, oidc):
    """Claim over real Rust HTTP and execute one turn with a canned native agent."""
    client = RunnerV2(base, session)
    claimed = client.claim(oidc)
    grant = client.key(claimed["engine"]["sha"])
    mirror = Mirror(root / "mirrors", "owner/target", url="file://" + str(root / "upstream.git"))
    script = """
import json, os, pathlib, sys, urllib.request
request = json.load(sys.stdin)
key = os.environ['CONCURRO_GATEWAY_KEY']
assert key.startswith('sk-concurro-')
url = request['gateway']['url'] + '/canned'
with urllib.request.urlopen(urllib.request.Request(url, data=json.dumps({'model': request['model']}).encode(),
    headers={'Authorization': 'Bearer ' + key}), timeout=5) as reply:
    assert json.load(reply)['message'] == 'fake inference'
work = pathlib.Path(request['workspace'])
assert (pathlib.Path(request['retained_root']) / 'session.txt').read_text() == 'previous turn\\n'
(work / 'docs/guide.md').write_text('Guide\\nEdited\\n')
if request['stage'] == 'intake':
    print(json.dumps({'decision':'proceed','headline':'Ready','rationale':'Fixture accepted',
        'risk':'low','questions':[],'concerns':[],
        'scope':{'goal_ids':['g1'],'fit':'Matches the fixture goal','conflicts':[]}}))
else:
    print(json.dumps({'summary': 'done', 'checks': [], 'design_issue': None}))
"""
    with (
        patch.object(turns, "command", return_value=[sys.executable, "-c", script]),
        patch.object(runner, "HEARTBEAT", 1),
    ):
        runner.Loop(
            client,
            grant["key"],
            mirror,
            root / "turns",
            idle=-1,
            task=grant["binding"],
            inference_url=grant["inference_base_url"],
        ).serve()
    client.end()


if __name__ == "__main__":
    command, root, *args = sys.argv[1:]
    if command == "prepare":
        prepare(Path(root))
    elif command == "serve":
        serve(Path(root), *args)
    else:
        raise ValueError("unknown canned-runner command")
