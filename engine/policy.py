"""Pure validation rules shared by the controller and offline tests."""

import classification
import hashlib
import model_config
import json
import re
from pathlib import PurePosixPath
from checks import (MAX_PLAN_FILES, glob_matches as instruction_matches,
                    in_scope, scope_errors, scope_of)

IMAGE = re.compile(r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[a-f0-9]{64}")


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


# Exit statuses meaning a provider call may have completed upstream: the agent entry point's
# own signal for a provider timeout or kill, and a container killed by a signal.
UNCERTAIN_EXIT = 75
KILLED = (-9, 137)


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


def validate_qc(report, stage, charter=None, criteria_count=32, objection_count=0):
    """Reject incomplete or contradictory decisions before they can authorize work."""
    report = classification.core(report)
    fields = {"decision", "rationale", "risk", "questions", "concerns", "scope"}
    if stage == 'plan_qc':
        fields |= {'sources', 'assessments', 'objection_updates'}
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
    if stage != 'plan_qc' and len(json.dumps(report).encode()) > 6000:
        raise Blocked("QC report too large")
    scope = report["scope"]
    if (not isinstance(scope, dict) or set(scope) != {"goal_ids", "fit", "conflicts"}
            or not isinstance(scope["goal_ids"], list) or not isinstance(scope["conflicts"], list)
            or not all(isinstance(v, str) and v.strip() for v in scope["goal_ids"] + scope["conflicts"])
            or not isinstance(scope["fit"], str) or not scope["fit"].strip()
            or len(scope["goal_ids"]) != len(set(scope["goal_ids"]))):
        raise Blocked("incomplete charter scope judgment")
    if charter is not None and not set(scope["goal_ids"]).issubset(charter["goals"]):
        raise Blocked("scope cites an unknown charter goal; copy exact IDs from "
                      + json.dumps(sorted(charter["goals"])) + "; put explanations in scope.fit")
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
        return [{"id": digest(parsed.resolve(name).record()), "provider": name,
                 "model": parsed.resolve(name).profile.model, "effort": parsed.resolve(name).profile.effort,
                 "agent": parsed.resolve(name).record(), "roles": ["plan_qc"]}
                for name in parsed.agents.plan_reviewers]
    members = [(config["planner"], "plan"), (config["implementer"], "develop"),
               (config["qc"], "intake")]
    members += [(provider, "review") for provider in config["reviewers"]]
    roster = {}
    for provider, stage in members:
        model = selected_model(config, provider, stage)
        key = digest([provider, model])
        entry = roster.setdefault(key, {"id": key, "provider": provider, "model": model, "roles": []})
        if stage not in entry["roles"]:
            entry["roles"].append(stage)
    return list(roster.values())


def approval_binding(state, config):
    """Bind every vote to the exact design generation, governance and participating identities."""
    return digest({"plan": state["plan_id"], "plan_content": state["plan"], "base": state["base"],
                   "requirements": state["requirements"], "config": state["config_digest"],
                   "bundle": state.get("bundle_id"), "charter": state.get("charter_id"),
                   "generation": state.get("design_generation"), "phase": state.get("design_phase"), "roster": design_roster(config),
                   "parent": state.get("design_parent"), "changed_files": state.get("approved_accumulated_files", [])})


def consensus_valid(state, config, require_final=True):
    """Require bound unanimity, with first-pass or separate fresh approval for implementation."""
    roster = design_roster(config)
    votes = state.get("design_votes", {})
    if not state.get("bundle_id") or not state.get("charter_id") or set(votes) != {r["id"] for r in roster}:
        return False
    if require_final and state.get("design_phase") != "fresh":
        if (state.get("design_phase") != "cooperative"
                or not all(v.get("first_pass") is True for v in votes.values())):
            return False
    binding = approval_binding(state, config)
    for member in roster:
        vote = votes[member["id"]]
        if vote.get("identity") != member or vote.get("binding") != binding:
            return False
        report = vote.get("report")
        try:
            from design import validate_evidence, criteria
            validate_qc(report, "plan_qc", state["charter"], len(criteria(state['requirements'])),
                        len(vote.get('objection_ids', [])))
            validate_evidence(report, state['requirements'], [{'id': v} for v in vote.get('objection_ids', [])])
        except (Blocked, TypeError, KeyError):
            return False
        if report["decision"] != "approve" or report["risk"] == "high":
            return False
    return True


def approval_valid(state, config):
    """Manual commands add writer approval; they cannot waive the unanimous design gate."""
    approval = state.get("approval", {})
    if (approval.get("plan") != state.get("plan_id") or not consensus_valid(state, config)
            or approval.get("binding") != approval_binding(state, config)):
        return False
    return approval.get("kind") == ("human" if state.get("mode") == "manual" else "consensus")


def validate_design_issue(issue):
    """Require concrete evidence before reopening requirements or design."""
    if (not isinstance(issue, dict) or set(issue) != {"kind", "explanation", "evidence"}
            or issue["kind"] not in ("requirements", "plan")
            or not isinstance(issue["explanation"], str) or not issue["explanation"].strip()
            or not isinstance(issue["evidence"], list) or not issue["evidence"]
            or not all(isinstance(v, str) and v.strip() for v in issue["evidence"])):
        raise Blocked("design reopening requires structured evidence")


def path_safe(path):
    """Engine invariants: a short, relative, visible path outside agent governance files."""
    if not isinstance(path, str) or not path or len(path) > 240:
        return False
    parts = path.split("/")
    return (
        not PurePosixPath(path).is_absolute()
        and all(p not in ("", ".", "..") and not p.startswith(".") for p in parts)
        and not any(p in PROTECTED_NAMES for p in parts)
        and "\\" not in path
        and all(c.isalnum() or c in "/_.-" for c in path)
    )


def path_allowed(path, config, execution):
    """Apply the target's editable-path policy and profile protections on top of engine invariants."""
    if not path_safe(path) or any(instruction_matches(path, g) for g in config["paths"].get("protected", [])):
        return False
    policy = config["paths"]
    return (
        path.endswith(tuple(policy["editable_extensions"]))
        and not any(p in policy["protected_names"] or p.startswith(tuple(policy["protected_name_prefixes"]))
                    for p in path.split("/"))
        and path not in policy["protected_files"]
        and path not in config["policy_documents"]
        and not path.startswith(tuple(policy["protected_prefixes"]))
    )


def validate_plan(plan, config, execution):
    """Require a bounded, reviewable plan or explicit clarification questions."""
    fields = {"summary", "questions", "files", "steps", "tests", "responses", "publication"}
    if not isinstance(plan, dict) or set(plan) - {"scope"} != presentation(plan, fields, {"headline"}):
        raise Blocked("invalid plan fields")
    publication = plan['publication']
    if (not isinstance(publication, dict) or set(publication) != {'changes', 'review_focus'}
            or any(not isinstance(publication[k], list) or not 1 <= len(publication[k]) <= 5
                   or not all(isinstance(t, str) and 0 < len(t) <= 300 for t in publication[k]) for k in publication)):
        raise Blocked('plan publication needs bounded changes and review focus')
    if not isinstance(plan["responses"], list) or any(
            not isinstance(v, dict) or set(v) != {"id", "response"}
            or not all(isinstance(t, str) and t.strip() for t in v.values()) for v in plan["responses"]):
        raise Blocked("invalid planner objection responses")
    for key in ("questions", "files", "steps", "tests"):
        if not isinstance(plan[key], list) or not all(isinstance(v, str) for v in plan[key]):
            raise Blocked("invalid plan list")
    if not isinstance(plan["summary"], str) or not plan["summary"].strip():
        raise Blocked("plan summary missing")
    if len(json.dumps(plan)) > 20000:
        raise Blocked("plan too large")
    if plan["questions"]:
        return
    if not 1 <= len(plan["files"]) <= MAX_PLAN_FILES or len(set(plan["files"])) != len(plan["files"]):
        raise Blocked("plan must name 1–8 distinct files")
    if not all(path_allowed(p, config, execution) for p in plan["files"]):
        raise Blocked("plan includes protected or unsupported files")
    problem = scope_errors(plan)
    if problem:
        raise Blocked(problem)
    if not plan["steps"] or not plan["tests"]:
        raise Blocked("plan needs steps and validation criteria")


def validate_edits(result, plan, originals, config, execution):
    """Accept complete text replacements only within the approved file set."""
    if set(result) != {"summary", "edits"} or not isinstance(result["summary"], str):
        raise Blocked("invalid implementation response")
    edits = result["edits"]
    limit = scope_of(plan)["max_files"]
    if not isinstance(edits, list) or not 1 <= len(edits) <= limit:
        raise Blocked(f"implementation must contain 1–{limit} edits")
    if not all(isinstance(edit, dict) and set(edit) == {"path", "content"} for edit in edits):
        raise Blocked("invalid edit fields")
    # A proposal reaching outside the envelope, or touching a path no edit may change, is refused
    # whole before anything is applied; the implementer gets the paths back as feedback.
    outside = {e["path"] for e in edits if isinstance(e["path"], str)
               and (not path_allowed(e["path"], config, execution) or not in_scope(e["path"], plan))}
    if outside:
        raise OutsideScope(outside)
    seen = set()
    for edit in edits:
        path, content = edit["path"], edit["content"]
        if not path_allowed(path, config, execution) or path in seen:
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
    if (not isinstance(entries, list) or len(entries) > 16
            or not all(isinstance(e, dict) and set(e) == {"path", "assessment"} and isinstance(e["path"], str)
                       and isinstance(e["assessment"], str) and 1 <= len(e["assessment"].strip()) <= 2000
                       for e in entries)
            or len({e["path"] for e in entries}) != len(entries)):
        raise Blocked("invalid manifest assessments")
    missing = set(manifests) - {e["path"] for e in entries}
    if missing:
        raise Blocked("review must assess every changed manifest: " + ", ".join(sorted(missing)))
    return {"manifests"} if isinstance(review, dict) and "manifests" in review else set()


def validate_review(review, manifests=()):
    """Reject contradictory approvals and incomplete structured findings."""
    if set(review) != presentation(review, {"verdict", "summary", "findings", "design_issue"},
                                   {"headline"}) | manifest_fields(review, manifests):
        raise Blocked("invalid review fields")
    if review["verdict"] not in ("approve", "changes_requested", "reopen"):
        raise Blocked("invalid review verdict")
    if not isinstance(review["summary"], str) or not isinstance(review["findings"], list):
        raise Blocked("invalid review content")
    if review["verdict"] == "reopen":
        validate_design_issue(review["design_issue"])
        if review["findings"]:
            raise Blocked("design reopening cannot also request implementation fixes")
        return
    if review["design_issue"] is not None:
        raise Blocked("review verdict contradicts design issue")
    if (review["verdict"] == "approve") != (not review["findings"]):
        raise Blocked("review verdict contradicts findings")
    if len(json.dumps(review)) > 16000:
        raise Blocked("review too large")
    for finding in review["findings"]:
        if set(finding) != {"path", "detail"} or not all(isinstance(v, str) for v in finding.values()):
            raise Blocked("invalid finding")


PR_REVIEW_FIELDS = {"verdict", "summary", "findings", "dispositions", "design_issue"}
PR_FINDING_FIELDS = {"path", "start_line", "line", "detail"}
SEVERITIES = ("critical", "high", "medium", "low")
MAX_PR_FINDINGS = 20
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
    if path is not None and (not isinstance(path, str) or not path.strip() or len(path) > 240
                             or PurePosixPath(path).is_absolute() or ".." in path.split("/")):
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
    review = classification.core(review)
    if (not isinstance(review, dict)
            or set(review) != presentation(review, PR_REVIEW_FIELDS, {"headline"}) | manifest_fields(review, manifests)):
        raise Blocked("invalid review fields")
    if review["verdict"] not in ("approve", "changes_requested", "reopen"):
        raise Blocked("invalid review verdict")
    if (not isinstance(review["summary"], str) or not isinstance(review["findings"], list)
            or not isinstance(review["dispositions"], list)):
        raise Blocked("invalid review content")
    if len(review["findings"]) > MAX_PR_FINDINGS:
        raise Blocked("review too large")
    if review["verdict"] == "reopen":
        validate_design_issue(review["design_issue"])
        if review["findings"] or review["dispositions"]:
            raise Blocked("design reopening cannot also report or answer findings")
        return
    if review["design_issue"] is not None:
        raise Blocked("review verdict contradicts design issue")
    for finding in review["findings"]:
        validate_pr_finding(finding)
    answered = []
    for disposition in review["dispositions"]:
        if (not isinstance(disposition, dict) or set(disposition) != {"id", "status", "evidence"}
                or not isinstance(disposition["id"], str) or not isinstance(disposition["evidence"], str) or not disposition["evidence"].strip()):
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
