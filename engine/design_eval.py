"""Operator-requested design evaluation, executed through ordinary fresh runner role sessions."""

import json
import subprocess
import uuid
from copy import deepcopy
from pathlib import Path

from design import criteria, report_limit, validate_evidence
from mirror import Mirror, git_environment
from model_config import resolve
from policy import digest, validate_qc

SOURCE = {
    "docs/workflow.md": """# Contribution workflow
Only repository writers enroll issues with the concurro label.
The controller opens a draft PR after implementation.
Independent agent reviews and passing CI are needed before marking it ready.
Humans decide whether to merge; the controller never merges.
"""
}
REQUIREMENTS = {
    "title": "Document the contribution workflow",
    "body": """Acceptance criteria:
- Explain writer enrollment with the concurro label.
- Explain that implementation opens a draft PR.
- Require independent agent reviews and passing CI before readiness.
- State that only humans merge.
- Change only docs/guide.md, and compare claims against docs/workflow.md.
""",
    "discussion": [],
}


def cases():
    """A valid plan and independent semantic defects; expected labels never enter model context."""
    plan = {
        "summary": "Document the contribution workflow.",
        "questions": [],
        "files": ["docs/guide.md"],
        "steps": [
            "Create docs/guide.md: writers enroll with concurro; implementation opens a draft PR; "
            "independent agent reviews and passing CI are required for readiness; only humans merge."
        ],
        "tests": ["Compare every workflow claim with docs/workflow.md."],
        "responses": [],
        "publication": {
            "changes": ["Document contribution stages."],
            "review_focus": ["Verify readiness and merge policy."],
        },
    }
    result = [{"id": "valid", "expected": "approve", "plan": plan}]
    for name, old, new in (
        ("automatic-merge", "only humans merge", "the controller merges automatically"),
        ("no-ci", "independent agent reviews and passing CI are required", "only agent reviews are required"),
        ("ready-immediately", "implementation opens a draft PR", "implementation opens a ready PR"),
    ):
        changed = deepcopy(plan)
        changed["steps"] = [plan["steps"][0].replace(old, new)]
        result.append({"id": name, "expected": "revise", "plan": changed})
    return result


def fixture_mirror(root, repository):
    """A mirror holding one commit of the evaluation's source fixture, and nothing to fetch."""
    mirror = Mirror(root, repository, url="")
    mirror.initialize()
    env = {
        **git_environment(),
        "GIT_INDEX_FILE": str(Path(root) / "fixture.index"),
        "GIT_AUTHOR_NAME": "Concurro",
        "GIT_AUTHOR_EMAIL": "concurro@invalid",
        "GIT_COMMITTER_NAME": "Concurro",
        "GIT_COMMITTER_EMAIL": "concurro@invalid",
    }

    def git(*args, data=None):
        return (
            subprocess.run(
                ["git", "--git-dir", str(mirror.path), *args], input=data, env=env, capture_output=True, check=True
            )
            .stdout.decode()
            .strip()
        )

    for name, text in SOURCE.items():
        blob = git("hash-object", "-w", "--no-filters", "--stdin", data=text.encode())
        git("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    commit = git("commit-tree", git("write-tree"), "-m", "Evaluation fixture")
    git("update-ref", "refs/heads/fixture", commit)
    return mirror, commit


def request(case, provider, config, bundle, base):
    """A fresh design review; the runner substitutes the engine-owned fixture checkout."""
    agent = resolve(config, provider, "review")
    return {
        "issue": REQUIREMENTS,
        "base": base,
        "repository_commit": base,
        "repository": {"refs": {config["base"]: base}, "head": config["base"]},
        "bundle_id": bundle.identity,
        "charter": bundle.charter,
        "review_phase": "fresh",
        "controller_evaluation": uuid.uuid4().hex,
        "reviewer_identity": {
            "id": digest(agent.record()) if "agents" in config else digest([provider, agent.profile.model]),
            "provider": provider,
            "model": agent.profile.model,
            "roles": ["review"],
            **({"agent": agent.record()} if "agents" in config else {}),
        },
        "review_criteria": criteria(REQUIREMENTS),
        "review_output_budget": {"max_json_bytes": report_limit()},
        "plan": {k: v for k, v in case["plan"].items() if k not in ("summary", "publication", "responses")},
    }


def score(results):
    """Separate missed defects, false alarms and invalid responses."""
    expected = {case["id"]: case["expected"] for case in cases()}
    if {r["id"] for r in results} != set(expected) or len(results) != len(expected):
        raise ValueError("evaluation must contain every case exactly once")
    return {
        "cases": len(results),
        "invalid": sum(bool(r.get("error")) for r in results),
        "missed_defects": sum(
            not r.get("error") and expected[r["id"]] == "revise" and r["decision"] == "approve" for r in results
        ),
        "false_alarms": sum(
            not r.get("error") and expected[r["id"]] == "approve" and r["decision"] != "approve" for r in results
        ),
        "correct": sum(not r.get("error") and r["decision"] == expected[r["id"]] for r in results),
    }


def evaluate(agents, config, provider, base, output):
    """Run four isolated, budgeted turns; persist reports without publishing task state or GitHub reviews."""
    output = Path(output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    results = []
    agent = resolve(config, provider, "review")
    for case in cases():
        item = {
            "id": case["id"],
            "provider": provider,
            "model": agent.profile.model,
            **({"agent": agent.record()} if "agents" in config else {}),
            "bundle_id": agents.bundle.identity,
            "fixture_digest": digest([SOURCE, REQUIREMENTS, case["plan"]]),
        }
        try:
            report = agents.ask(provider, "plan_qc", request(case, provider, config, agents.bundle, base))
            item["report"] = report
            validate_qc(report, "plan_qc", agents.bundle.charter)
            validate_evidence(report, REQUIREMENTS, files=SOURCE)
            item["decision"] = report["decision"]
        except Exception as error:
            item["error"] = type(error).__name__
        results.append(item)
        (output / (case["id"] + ".json")).write_text(json.dumps(item, indent=2))
    summary = score(results)
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    return {"directory": str(output), **summary}
