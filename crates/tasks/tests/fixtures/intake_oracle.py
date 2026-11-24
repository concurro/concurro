"""Compare a Rust intake turn with the current Python payload builder."""

import json
import sys
import tempfile
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(root / "engine"))

import feedback  # noqa: E402
import prompts  # noqa: E402
from agents import Agents  # noqa: E402
from bundle import Bundle  # noqa: E402
from checks import limits  # noqa: E402
from policy import path_allowed, plan_output_contract  # noqa: E402

sys.path.insert(0, str(root / "service"))
from diagnostics import prompt as diagnostic_prompt  # noqa: E402

sys.path.insert(0, str(root / "gateway" / "exchange"))
from exchange import staffing  # noqa: E402

sys.path.insert(0, str(root / "gateway" / "tests"))
from exchange_fixtures import AUDIENCE  # noqa: E402

from exchange import Exchange, Refused  # noqa: E402
from test_exchange_funding import FundedGateway, MetadataVault  # noqa: E402

case = json.load(sys.stdin)
snapshot = case["snapshot"]
submission = case["submission"]
config = snapshot["configuration"]
bundle = Bundle(
    root=root,
    charter_text=snapshot["charter"],
    instruction_texts=snapshot["instruction_texts"],
)
assert bundle.identity == snapshot["bundle_id"]
base = snapshot["pins"]["base_sha"]
branch = config["base"]
requirements = snapshot["requirements"]
context = {
    "issue": {
        "title": requirements["title"],
        "body": requirements["body"],
        "discussion": [
            {"author": item["author"]["login"], "body": item["body"]} for item in requirements["discussion"]
        ],
    },
    "policy": {
        path: body
        for path, body in snapshot["policy_documents"].items()
        if body and not path.startswith(".github/instructions/")
    },
    "edit_policy": plan_output_contract(config)["edit_policy"],
    "limits": limits(config),
    "plan": None,
    "base": base,
    "bundle_id": bundle.identity,
    "charter": bundle.charter,
    "prior_outcomes_untrusted": [],
    "editable_files": [path for path in snapshot["base_files"] if path_allowed(path, config)],
    "repository_commit": base,
    "repository": {"refs": {branch: base}, "head": branch},
}
if "correction" in case:
    correction = case["correction"]
    context["intake_validation_feedback"] = {
        "output": json.dumps(correction["report"], indent=2, ensure_ascii=True),
        "truncated": False,
        "reason": correction["reason"],
        "attempt": correction["attempt"],
        "instruction": (
            "Correct the intake report against its schema and the rejection reason. "
            "Use clarify with questions when a human answer is needed; do not remove real questions to proceed. "
            "Proceed requires empty questions and scope conflicts; advisory remarks may accompany it. "
            "Preserve genuine blockers with an appropriate non-proceed decision. "
            "This feedback concerns output validity, not a request to approve the issue."
        ),
    }
    context = feedback.prepare(context)
gateway = submission["payload"]["request"]["gateway"]
actual = Agents(config, SimpleNamespace(gateway=gateway), bundle=bundle).prepare(config["qc"], "intake", context)
print(
    json.dumps(
        {
            "size": prompts.size("intake", context, bundle),
            "prompt": diagnostic_prompt(submission["payload"]["request"], submission["payload"].get("feedback")),
            "staffing_group": staffing(
                submission["expected"],
                submission["binding"],
                submission["models"],
                submission["budget"],
                submission["lease"],
            ),
        }
    )
)
expected = deepcopy(submission["payload"])
expected["request"].pop("admission", None)
expected["request"].pop("attempt", None)
assert expected["request"].pop("engine_resources") == {
    p: text for p, text in bundle.resources.items() if p.startswith("bundle/")
}
if actual != expected:
    print("Python intake payload:", json.dumps(actual, indent=2, sort_keys=True), file=sys.stderr)
    print("Rust intake payload:", json.dumps(expected, indent=2, sort_keys=True), file=sys.stderr)
    sys.exit(1)

if "transitions" in case:
    # Prove the persisted Rust body against the retained Exchange's real admission path.
    vault = MetadataVault()
    vault.records = [
        {
            "user_id": "2",
            "route": "openai-api-key",
            "record_id": "e" * 64,
            "version": 1,
            "kind": "api_key",
            "status": "valid",
            "models": ["gpt-test"],
            "expiry_known": False,
            "expires_at": None,
        }
    ]
    with tempfile.TemporaryDirectory() as directory:
        exchange = Exchange(directory + "/exchange.db", FundedGateway(), None, AUDIENCE, vault=vault)
        try:
            assert len(case["transitions"]) == 3
            for transition in case["transitions"][:2]:
                exchange.sponsorship(transition)
            body = case["submission"]
            values = [body[k] for k in ("turn", "expected", "models", "budget", "lease", "payload")]
            first = exchange.submit(*values, pin=body["pin"], binding=body["binding"])
            assert first["status"] == "queued"
            assert exchange.submit(*values, pin=body["pin"], binding=body["binding"]) == first
            assert exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0] == 1
            withdraw = case["transitions"][2]
            assert withdraw["reason"] == "withdraw" and withdraw["cutoff"] == body["binding"]["epoch"]
            exchange.sponsorship(withdraw)
            later = deepcopy(body)
            later["turn"] = str(uuid.uuid4())
            later["payload"]["request"]["attempt"] = later["turn"]
            later["payload"]["request"]["admission"]["call"] = 2
            later_values = [later[k] for k in ("turn", "expected", "models", "budget", "lease", "payload")]
            try:
                exchange.submit(*later_values, pin=later["pin"], binding=later["binding"])
            except Refused as error:
                assert error.status == 409, str(error)
                assert str(error) == "provider grant differs from current sponsorship fence"
            else:
                raise AssertionError("withdrawal allowed another paid turn")
            assert exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0] == 1
        finally:
            exchange.db.close()
