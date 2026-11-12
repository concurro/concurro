"""Pure validation rules shared by the controller and offline tests."""

import hashlib
import json
import re
from pathlib import PurePosixPath

import model_config
import protocol
from checks import (
    file_count_error,
    glob_matches as instruction_matches,
    in_scope,
    limits,
    scope_errors,
)
from report_core import core as report_core

IMAGE = re.compile(r"(?:[a-zA-Z0-9./:_-]+@)?sha256:" + protocol.DIGEST_PATTERN)


PROTECTED_NAMES = frozenset({"AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "SECURITY.md"})


class Blocked(ValueError):
    """An input cannot safely advance the pipeline."""


class RunnerTerminated(Blocked):
    """Implementation stopped without a result; preserve uncertainty but require a writer retry."""

    result_uncertain = True


class SessionFailure(Blocked):
    """A provider turn or its local transcript needs a new isolated session."""

    def __init__(self, message, uncertain=False):
        """Record whether the failed turn may still have completed upstream."""
        super().__init__(message)
        self.uncertain = uncertain


KEY_VARIABLE = "CONCURRO_GATEWAY_KEY"

# Exit statuses meaning a provider call may have completed upstream: the agent entry point's
# own uncertainty signal and a container killed by a signal. Timeouts remain distinct.
UNCERTAIN_EXIT = 75
TIMEOUT_EXIT = 76
KILLED = (-9, 137)
SIGNALLED = (-2, -15, 130, 143)


def runner_bound(stage):
    """Stages whose execution lifetime belongs to a fresh Actions job."""
    return stage in ("develop", "review")


def turn_label(stage):
    """Fixed public role names, never model-supplied diagnostic text."""
    return {"develop": "Implementation", "review": "PR review"}.get(stage, "Agent")


class UncertainOutcome(Blocked):
    """A provider call may have completed upstream, but its result was not received."""

    uncertain = True


def digest(value):
    """Bind approvals to a canonical JSON value."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def task_branch(repository, issue):
    """A stable fork branch whose suffix distinguishes same-numbered issues in different repositories."""
    return f"concurro/issue-{issue}-{digest([repository, issue])[:12]}"


def selected_model(config, provider, stage):
    """Allow a separate QC model while retaining provider defaults for other roles."""
    if "agents" in config:
        return model_config.resolve(config, provider, stage).profile.model
    if stage in ("intake", "plan_qc") and config["qc_model"]:
        return config["qc_model"]
    return config["models"][provider]


def agent_identity(config, provider, stage, reviewer=None):
    """Record the configured reviewer identity, including legacy Copilot attribution."""
    if provider == "copilot" and "agents" not in config:
        return {
            "profile": provider,
            "harness": provider,
            "provider": None,
            "model": (reviewer or {}).get("model") or selected_model(config, provider, stage),
            "effort": None,
        }
    return model_config.resolve(config, provider, stage, reviewer).record()


class OutsideScope(Blocked):
    """An implementation proposal edits paths outside the approved envelope; refused whole, as feedback."""

    def __init__(self, paths):
        self.paths = sorted(paths)
        super().__init__("edit outside the approved scope or of a protected path: " + ", ".join(self.paths))


def presentation(report, fields, optional):
    """`fields` plus whichever `optional` presentation fields the report has, each a string.

    Presentation fields are normalized when rendered, never a reason to reject; reports
    recorded before they existed have none.
    """
    present = {key for key in optional if isinstance(report, dict) and key in report}
    if not all(isinstance(report[key], str) for key in present):
        raise Blocked("invalid presentation field")
    return fields | present


def validate_qc(report, stage, charter=None):
    """Reject incomplete or contradictory decisions before they can authorize work."""
    report = report_core(report)
    fields = {"decision", "rationale", "risk", "questions", "concerns", "scope"}
    if stage == "plan_qc":
        fields |= {"sources", "assessments", "objection_updates"}
    if not isinstance(report, dict) or set(report) != presentation(report, fields, {"headline"}):
        raise Blocked("invalid QC fields")
    decisions = {"clarify", "reject", "escalate"} | ({"proceed"} if stage == "intake" else {"approve", "revise"})
    if report["decision"] not in decisions or report["risk"] not in ("low", "medium", "high"):
        raise Blocked("invalid QC decision or risk")
    if not isinstance(report["rationale"], str) or not report["rationale"].strip():
        raise Blocked("QC rationale missing")
    for key in ("questions", "concerns"):
        if not isinstance(report[key], list) or not all(isinstance(v, str) and v.strip() for v in report[key]):
            raise Blocked("invalid QC questions or concerns")
    scope = report["scope"]
    if (
        not isinstance(scope, dict)
        or set(scope) != {"goal_ids", "fit", "conflicts"}
        or not isinstance(scope["goal_ids"], list)
        or not isinstance(scope["conflicts"], list)
        or not all(isinstance(v, str) and v.strip() for v in scope["goal_ids"] + scope["conflicts"])
        or not isinstance(scope["fit"], str)
        or not scope["fit"].strip()
        or len(scope["goal_ids"]) != len(set(scope["goal_ids"]))
    ):
        raise Blocked("incomplete charter scope judgment")
    if charter is not None and not set(scope["goal_ids"]).issubset(charter["goals"]):
        raise Blocked(
            "scope cites an unknown charter goal; copy exact IDs from "
            + json.dumps(sorted(charter["goals"]))
            + "; put explanations in scope.fit"
        )
    decision = report["decision"]
    if decision in ("proceed", "approve") and (not scope["goal_ids"] or scope["conflicts"]):
        raise Blocked("acceptance contradicts charter scope")
    if decision in ("proceed", "approve") and (report["questions"] or stage != "intake" and report["concerns"]):
        raise Blocked("QC acceptance contradicts outstanding questions or concerns")
    if decision == "clarify" and not report["questions"]:
        raise Blocked("QC clarification requires questions")
    if decision == "revise" and (not report["concerns"] or report["questions"]):
        raise Blocked("QC revision requires concerns and no unanswered questions")


def design_roster(config):
    """The explicit plan reviewers, or the original deduplicated roster for legacy tasks."""
    if "agents" in config:
        parsed = model_config.parse(config)
        roster = []
        for name in parsed.agents.plan_reviewers:
            agent = parsed.resolve(name)
            identity = agent.record()
            roster.append(
                {
                    "id": digest(identity),
                    "provider": name,
                    "model": agent.profile.model,
                    "effort": agent.profile.effort,
                    "agent": identity,
                    "roles": ["plan_qc"],
                }
            )
        return roster
    members = [(config["planner"], "plan"), (config["implementer"], "develop"), (config["qc"], "intake")]
    members += [(provider, "review") for provider in config["reviewers"]]
    roster = {}
    for provider, stage in members:
        model = selected_model(config, provider, stage)
        key = digest([provider, model])
        entry = roster.setdefault(key, {"id": key, "provider": provider, "model": model, "roles": []})
        if stage not in entry["roles"]:
            entry["roles"].append(stage)
    return list(roster.values())


def bound_design_roster(state, config):
    """Keep completed design approval attributed to its original reviewers after resume."""
    receipt = state.get("bundle_resume", {})
    if (
        receipt.get("approval_roster")
        and receipt.get("plan") == state.get("plan_id")
        and receipt.get("generation") == state.get("design_generation")
        and receipt.get("phase") == state.get("design_phase")
    ):
        return receipt["approval_roster"]
    return design_roster(config)


def approval_binding(state, config):
    """Bind every vote to the exact design generation, governance and participating identities."""
    return digest(
        {
            "plan": state["plan_id"],
            "plan_content": state["plan"],
            "base": state["base"],
            "requirements": state["requirements"],
            "config": state["config_digest"],
            "bundle": state.get("bundle_id"),
            "charter": state.get("charter_id"),
            "generation": state.get("design_generation"),
            "phase": state.get("design_phase"),
            "roster": bound_design_roster(state, config),
            "parent": state.get("design_parent"),
            "changed_files": state.get("approved_accumulated_files", []),
        }
    )


def consensus_valid(state, config, require_final=True):
    """Require bound unanimity, with first-pass or separate fresh approval for implementation."""
    roster = bound_design_roster(state, config)
    votes = state.get("design_votes", {})
    if not state.get("bundle_id") or not state.get("charter_id") or set(votes) != {r["id"] for r in roster}:
        return False
    if require_final and state.get("design_phase") != "fresh":
        if state.get("design_phase") != "cooperative" or not all(v.get("first_pass") is True for v in votes.values()):
            return False
    binding = approval_binding(state, config)
    for member in roster:
        vote = votes[member["id"]]
        if vote.get("identity") != member or vote.get("binding") != binding:
            return False
        report = vote.get("report")
        try:
            # design imports policy; defer the reverse edge until validation.
            from design import validate_evidence

            validate_qc(report, "plan_qc", state["charter"])
            validate_evidence(report, state["requirements"], [{"id": v} for v in vote.get("objection_ids", [])])
        except (Blocked, TypeError, KeyError):
            return False
        if report["decision"] != "approve" or report["risk"] == "high":
            return False
    return True


def approval_valid(state, config):
    """Manual commands add writer approval; they cannot waive the unanimous design gate."""
    approval = state.get("approval", {})
    if (
        approval.get("plan") != state.get("plan_id")
        or not consensus_valid(state, config)
        or approval.get("binding") != approval_binding(state, config)
    ):
        return False
    return approval.get("kind") == ("human" if state.get("mode") == "manual" else "consensus")


def validate_design_issue(issue):
    """Require concrete evidence before reopening requirements or design."""
    if (
        not isinstance(issue, dict)
        or set(issue) != {"kind", "explanation", "evidence"}
        or issue["kind"] not in ("requirements", "plan")
        or not isinstance(issue["explanation"], str)
        or not issue["explanation"].strip()
        or not isinstance(issue["evidence"], list)
        or not issue["evidence"]
        or not all(isinstance(v, str) and v.strip() for v in issue["evidence"])
    ):
        raise Blocked("design reopening requires structured evidence")


def path_safe(path):
    """Keep candidate paths relative and outside Git and pinned Concurro metadata."""
    if not isinstance(path, str) or not path:
        return False
    parts = path.split("/")
    return (
        not PurePosixPath(path).is_absolute()
        and all(p not in ("", ".", "..") and p.casefold() not in (".git", ".concurro") for p in parts)
        and "\\" not in path
        and all(c.isalnum() or c in "/_.-" for c in path)
    )


def path_rules(config):
    """Resolve default-open policy while preserving the meaning of legacy pinned policies."""
    policy = config["paths"]
    if "editable_extensions" in policy:
        return policy
    return {
        "protected_extensions": [],
        "protected_names": [],
        "protected_name_prefixes": [],
        "protected_prefixes": [".concurro/"],
        "protected_files": [],
        "protected": [],
        "generated": [],
        **policy,
    }


def path_allowed(path, config):
    """Apply operator policy after structural path validation; legacy policies retain their denials."""
    policy = path_rules(config)
    if not path_safe(path) or any(instruction_matches(path, g) for g in policy.get("protected", [])):
        return False
    if "editable_extensions" in policy:
        if (
            not path.endswith(tuple(policy["editable_extensions"]))
            or any(p.startswith(".") or p in PROTECTED_NAMES for p in path.split("/"))
            or path in config["policy_documents"]
        ):
            return False
    elif path.endswith(tuple(policy["protected_extensions"])):
        return False
    return (
        not any(
            p in policy["protected_names"] or p.startswith(tuple(policy["protected_name_prefixes"]))
            for p in path.split("/")
        )
        and path not in policy["protected_files"]
        and not path.startswith(tuple(policy["protected_prefixes"]))
    )


def plan_output_contract(config):
    """Expose enforced plan limits and target policy before the first planner attempt."""
    return {
        "limits": limits(config),
        "publication_items_min": 1,
        "edit_policy": {
            "paths": path_rules(config),
            "policy_documents": config["policy_documents"],
            "protected_instruction_names": sorted(PROTECTED_NAMES) if "editable_extensions" in config["paths"] else [],
            "reserved_components": [".git", ".concurro"],
        },
        "instruction": (
            "files names primary files, not every anticipated edit. scope.paths may use narrow directories/globs "
            "to include related tests and follow-on edits. Null limits mean no configured cap. "
            "Paths must be repository-relative, without empty, . or .. components. "
            "Neither scope nor an issue reply overrides edit_policy. If required work is forbidden, ask for a "
            "target-policy decision in questions; do not omit required work or claim that a reply grants permission. "
            "Keep publication lists concise; the full design belongs in summary, "
            "steps and tests. Genuine questions may leave files/scope incomplete and stop implementation."
        ),
    }


def validate_plan(plan, config):
    """Report independent plan violations together, retaining the clarification escape hatch."""
    fields = {"summary", "questions", "files", "steps", "tests", "responses", "publication"}
    if not isinstance(plan, dict) or set(plan) - {"scope"} != presentation(plan, fields, {"headline"}):
        raise Blocked("invalid plan fields")
    problems = []
    publication = plan["publication"]
    if not isinstance(publication, dict) or set(publication) != {"changes", "review_focus"}:
        problems.append("plan publication needs changes and review_focus arrays")
    else:
        for key in ("changes", "review_focus"):
            values = publication[key]
            if not isinstance(values, list) or not values:
                problems.append(f"publication.{key} must contain non-empty strings")
            if isinstance(values, list):
                for index, value in enumerate(values):
                    if not isinstance(value, str) or not value.strip():
                        problems.append(f"publication.{key}[{index}] must be a non-empty string")
                        break
    if not isinstance(plan["responses"], list) or any(
        not isinstance(v, dict)
        or set(v) != {"id", "response"}
        or not all(isinstance(t, str) and t.strip() for t in v.values())
        for v in plan["responses"]
    ):
        problems.append("invalid planner objection responses")
    lists_valid = True
    for key in ("questions", "files", "steps", "tests"):
        if not isinstance(plan[key], list) or not all(isinstance(v, str) for v in plan[key]):
            problems.append(f"plan {key} must be an array of strings")
            lists_valid = False
    if not isinstance(plan["summary"], str) or not plan["summary"].strip():
        problems.append("plan summary missing")
    if lists_valid and not plan["questions"]:
        if not plan["files"] or len(set(plan["files"])) != len(plan["files"]):
            problems.append("plan must name distinct primary files; put related follow-on edits in scope")
        maximum = limits(config)["primary_files"]
        if maximum is not None and len(plan["files"]) > maximum:
            problems.append(f"plan files exceed limits.primary_files ({maximum})")
        for index, path in enumerate(plan["files"]):
            if not path_allowed(path, config):
                problems.append(
                    f"files[{index}] is protected or unsupported by the target edit_policy; "
                    "a scope or issue reply cannot override it"
                )
                break
        problem = scope_errors(plan, config)
        if problem:
            problems.append(problem)
        if not plan["steps"] or not plan["tests"]:
            problems.append("plan needs steps and validation criteria")
    if problems:
        raise Blocked("; ".join(problems))


def validate_edits(result, plan, originals, config):
    """Accept complete text replacements only within the approved file set."""
    if set(result) != {"summary", "edits"} or not isinstance(result["summary"], str):
        raise Blocked("invalid implementation response")
    edits = result["edits"]
    if not isinstance(edits, list) or not edits:
        raise Blocked("implementation must contain edits")
    problem = file_count_error(len(edits), plan, config)
    if problem:
        raise Blocked(problem)
    if not all(isinstance(edit, dict) and set(edit) == {"path", "content"} for edit in edits):
        raise Blocked("invalid edit fields")
    # A proposal reaching outside the envelope, or touching a path no edit may change, is refused
    # whole before anything is applied; the implementer gets the paths back as feedback.
    outside = {
        e["path"]
        for e in edits
        if isinstance(e["path"], str) and (not path_allowed(e["path"], config) or not in_scope(e["path"], plan))
    }
    if outside:
        raise OutsideScope(outside)
    seen = set()
    for edit in edits:
        path, content = edit["path"], edit["content"]
        if not path_allowed(path, config) or path in seen:
            raise Blocked("edit outside approved scope or duplicate path")
        if content is None:
            if originals.get(path) is None:
                raise Blocked("a deletion must name an existing regular text file")
        elif not isinstance(content, str) or "\x00" in content:
            raise Blocked("invalid text replacement")
        if originals.get(path) == content:
            raise Blocked("unchanged replacement")
        seen.add(path)
    return edits


def manifest_fields(review, manifests):
    """`{"manifests"}` when a review assesses manifests; every changed one needs a bounded assessment.

    Reviews recorded before assessments existed have none, which is valid when no manifest changed.
    """
    entries = review.get("manifests", []) if isinstance(review, dict) else []
    if (
        not isinstance(entries, list)
        or not all(
            isinstance(e, dict)
            and set(e) == {"path", "assessment"}
            and isinstance(e["path"], str)
            and isinstance(e["assessment"], str)
            and bool(e["assessment"].strip())
            for e in entries
        )
        or len({e["path"] for e in entries}) != len(entries)
    ):
        raise Blocked("invalid manifest assessments")
    missing = set(manifests) - {e["path"] for e in entries}
    if missing:
        raise Blocked("review must assess every changed manifest: " + ", ".join(sorted(missing)))
    return {"manifests"} if isinstance(review, dict) and "manifests" in review else set()


REVIEW_VERDICTS = ("approve", "changes_requested", "reopen")


def _check_review_verdict(review):
    """Require a known verdict before interpreting review content."""
    if review["verdict"] not in REVIEW_VERDICTS:
        raise Blocked("invalid review verdict")


def _check_review_design_issue(review):
    """Non-reopening verdicts cannot carry a design issue."""
    if review["design_issue"] is not None:
        raise Blocked("review verdict contradicts design issue")


def validate_review(review, manifests=()):
    """Reject contradictory approvals and incomplete structured findings."""
    if set(review) != presentation(
        review, {"verdict", "summary", "findings", "design_issue"}, {"headline"}
    ) | manifest_fields(review, manifests):
        raise Blocked("invalid review fields")
    _check_review_verdict(review)
    if not isinstance(review["summary"], str) or not isinstance(review["findings"], list):
        raise Blocked("invalid review content")
    if review["verdict"] == "reopen":
        validate_design_issue(review["design_issue"])
        if review["findings"]:
            raise Blocked("design reopening cannot also request implementation fixes")
        return
    _check_review_design_issue(review)
    if (review["verdict"] == "approve") != (not review["findings"]):
        raise Blocked("review verdict contradicts findings")
    for finding in review["findings"]:
        if set(finding) != {"path", "detail"} or not all(isinstance(v, str) for v in finding.values()):
            raise Blocked("invalid finding")


PR_REVIEW_FIELDS = {"verdict", "summary", "findings", "dispositions", "design_issue"}
PR_FINDING_FIELDS = {"path", "start_line", "line", "detail"}
SEVERITIES = ("critical", "high", "medium", "low")
MODEL_RESPONSE_LIMIT = 300000


def line_number(value):
    """A 1-based line number or None."""
    return value is None or type(value) is int and value >= 1


def validate_pr_finding(finding):
    """One new finding: an optional repository path and line range on the reviewed head."""
    if not isinstance(finding, dict) or set(finding) != presentation(finding, PR_FINDING_FIELDS, {"severity", "title"}):
        raise Blocked("invalid finding")
    if "severity" in finding and finding["severity"] not in SEVERITIES:
        raise Blocked("invalid finding severity")
    path, start, line, detail = (finding[k] for k in ("path", "start_line", "line", "detail"))
    if path is not None and (
        not isinstance(path, str) or not path.strip() or PurePosixPath(path).is_absolute() or ".." in path.split("/")
    ):
        raise Blocked("finding path must be a relative repository path")
    if not line_number(start) or not line_number(line) or start is not None and (line is None or start > line):
        raise Blocked("finding lines must be 1-based with start_line ≤ line")
    if path is None and line is not None:
        raise Blocked("a finding without a path cannot name lines")
    if not isinstance(detail, str) or not detail.strip():
        raise Blocked("finding detail must be non-empty text")


def validate_pr_review(review, open_ids, withdrawn_ids, manifests=()):
    """Reject inconsistent PR reviews; dispositions answer exactly the reviewer's open and withdrawn findings.

    Local review keeps `validate_review`; only PR review carries line anchors and dispositions.
    """
    review = report_core(review)
    if not isinstance(review, dict) or set(review) != presentation(
        review, PR_REVIEW_FIELDS, {"headline"}
    ) | manifest_fields(review, manifests):
        raise Blocked("invalid review fields")
    _check_review_verdict(review)
    if (
        not isinstance(review["summary"], str)
        or not isinstance(review["findings"], list)
        or not isinstance(review["dispositions"], list)
    ):
        raise Blocked("invalid review content")
    if review["verdict"] == "reopen":
        validate_design_issue(review["design_issue"])
        if review["findings"] or review["dispositions"]:
            raise Blocked("design reopening cannot also report or answer findings")
        return
    _check_review_design_issue(review)
    for finding in review["findings"]:
        validate_pr_finding(finding)
    answered = []
    for disposition in review["dispositions"]:
        if (
            not isinstance(disposition, dict)
            or set(disposition) != {"id", "status", "evidence"}
            or not isinstance(disposition["id"], str)
            or not isinstance(disposition["evidence"], str)
            or not disposition["evidence"].strip()
        ):
            raise Blocked("invalid disposition")
        allowed = ("fixed", "open", "obsolete") if disposition["id"] in withdrawn_ids else ("fixed", "open")
        if disposition["status"] not in allowed:
            raise Blocked("invalid disposition status")
        answered.append(disposition["id"])
    if sorted(answered) != sorted(set(open_ids) | set(withdrawn_ids)):
        raise Blocked("dispositions must answer each of the reviewer's open and withdrawn findings once")
    still_open = review["findings"] or any(d["status"] == "open" for d in review["dispositions"])
    if (review["verdict"] == "approve") == bool(still_open):
        raise Blocked("review verdict contradicts findings")
