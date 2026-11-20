"""Compare a Rust intake turn with the current Python payload builder."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(root / "engine"))

from agents import Agents  # noqa: E402
from bundle import Bundle  # noqa: E402
from checks import limits  # noqa: E402
import feedback  # noqa: E402
import prompts  # noqa: E402
from policy import path_allowed, plan_output_contract  # noqa: E402
sys.path.insert(0, str(root / "service"))
from diagnostics import prompt as diagnostic_prompt  # noqa: E402
sys.path.insert(0, str(root / "gateway" / "exchange"))
from exchange import staffing  # noqa: E402

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
            {"author": item["author"]["login"], "body": item["body"]}
            for item in requirements["discussion"]
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
            "Proceed requires empty questions and scope conflicts; advisory concerns may accompany it. "
            "Preserve genuine blockers with an appropriate non-proceed decision. "
            "This feedback concerns output validity, not a request to approve the issue."
        ),
    }
    context = feedback.prepare(context)
gateway = submission["payload"]["request"]["gateway"]
actual = Agents(config, SimpleNamespace(gateway=gateway), bundle=bundle).prepare(
    config["qc"], "intake", context
)
print(json.dumps({
    "size": prompts.size("intake", context, bundle),
    "prompt": diagnostic_prompt(
        submission["payload"]["request"], submission["payload"].get("feedback")
    ),
    "staffing_group": staffing(
        submission["expected"], submission["binding"], submission["models"],
        submission["budget"], submission["lease"]
    ),
}))
expected = submission["payload"]
if actual != expected:
    print("Python intake payload:", json.dumps(actual, indent=2, sort_keys=True), file=sys.stderr)
    print("Rust intake payload:", json.dumps(expected, indent=2, sort_keys=True), file=sys.stderr)
    sys.exit(1)
