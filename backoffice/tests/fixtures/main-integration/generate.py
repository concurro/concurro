"""Produce nonsecret forward-upgrade fixtures with one exact parent checkout's actual writers."""

import hashlib
import json
import sys
import unittest
from pathlib import Path

root = Path(sys.argv[1])
out = Path(sys.argv[2])
out.mkdir(exist_ok=False)
sys.path[:0] = [
    str(root / "service/tests"),
    str(root / "gateway/tests"),
    str(root / "gateway/exchange"),
    str(root / "backoffice/tests"),
    str(root),
]
# isort: split
import fixtures  # noqa: E402 -- use this exact parent's test writer

from support import setUpModule, tearDownModule  # noqa: E402

from checkpoints import MARKER, Checkpoints  # noqa: E402
from fakes import FakeGitHub  # noqa: E402

setUpModule()


class Case(fixtures.ServiceFixture, unittest.TestCase):
    """Own the parent fixture resources without running its unrelated test suite."""

    pass


case = Case()
case.setUp()
try:
    case.registry.enqueue(fixtures.KEY, "parent-fixture", 2000)
    if hasattr(case.registry, "confirm_sponsorship"):
        case.registry.confirm_sponsorship(
            fixtures.KEY,
            0,
            {
                "status": "confirmed",
                "revision": "parent-source",
                "facts": [{"actor_id": "202", "provider": "openai", "command": "sponsor", "grant_id": "a" * 64}],
                "slots": {"openai": {"sponsor_id": "202", "epoch": 3}},
            },
            [],
        )
    if hasattr(case.registry, "activate_funding"):
        case.registry.activate_funding(fixtures.KEY[0], "d" * 64, "contributed")
    (out / "registry.sql").write_text("\n".join(case.registry.db.iterdump()) + "\n")
    states = {}
    for version in [7, 8, 9] if hasattr(__import__("report_store"), "ADMISSION_FORMAT") else [7]:
        fake = FakeGitHub(case.config)
        state = {
            "version": 2,
            "issue": 7,
            "mode": "manual",
            "base": "a" * 40,
            "stage": "planned",
            "plan_id": "parent-plan",
            "plan": {"summary": "Parent plan " * 2000, "steps": ["one"], "files": [], "tests": [], "questions": []},
            "reviews": {"reviewer": {"verdict": "approve", "summary": "Parent review " * 2000}},
            "reports": [],
            "calls": 5,
            "pr": 9,
        }
        if version >= 8:
            state["sponsorships"] = {
                "openai": {"sponsor_id": "202", "status": "active", "epoch": 3, "grant_id": "a" * 64}
            }
        if version >= 9:
            from test_exchange_funding import funded_turn

            turn, binding, payload = funded_turn()
            state["admission_version"] = 1
            state["operation"] = {
                "id": "parent-op",
                "attempt": 1,
                "admission": payload["request"]["admission"],
                "turn": turn,
                "binding": binding,
            }
        cp = Checkpoints(fake, case.config, 7)
        cp.save(state)
        fake.comments = []
        fake.comment(MARKER + "\n" + "\n".join(cp.links()), case.config["bot_login"])
        restored = Checkpoints(fake, case.config, 7).load(fake.comments)
        states[str(version)] = {
            "config": case.config,
            "comments": fake.comments,
            "artifacts": fake.artifacts,
            "blobs": fake.blobs,
            "restored": restored,
        }
    (out / "checkpoints.json").write_text(json.dumps(states, indent=2) + "\n")
    if (root / "backoffice/read_cache.py").exists():
        from backoffice.read_cache import ReadCache

        cache = ReadCache(out / "cache", clock=lambda: 1000)
        snapshot = {
            "issue": {"number": 7, "updated_at": "2026-11-13T10:00:00Z"},
            "comments": states["7"]["comments"],
            "state": states["7"]["restored"],
        }
        cache.read("parent-scope", fixtures.KEY[0], 7, lambda: snapshot)
        cache.invalidate(fixtures.KEY[0], 7)
        (out / "cache.sql").write_text("\n".join(cache.db.iterdump()) + "\n")
        cache.close()
    manifest = {
        "parent": sys.argv[3],
        "schema": dict(case.registry.db.execute("SELECT name,value FROM meta"))["schema"],
        "versions": list(states),
        "files": {
            str(p.relative_to(out)): hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob("*") if p.is_file()
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))
finally:
    case.doCleanups()
    tearDownModule()
