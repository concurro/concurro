"""Pure validation rules shared by the controller and offline tests."""

import hashlib
import json
import re
from pathlib import PurePosixPath
from checks import (MAX_PLAN_FILES, build_checked, contract_errors, glob_matches as instruction_matches, in_scope,
                    mandatory, protected as profile_protected, scope_errors, scope_of, triggered)

IMAGE = re.compile(r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[a-f0-9]{64}")
PROVIDER_CREDENTIALS = {
    "codex": ("CODEX_API_KEY",),
    "claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "copilot": ("COPILOT_GITHUB_TOKEN",),
}
SECRET_NAMES = frozenset(name for names in PROVIDER_CREDENTIALS.values() for name in names)


PROTECTED_NAMES = frozenset({"AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "SECURITY.md"})


class Blocked(ValueError):
    """An input cannot safely advance the pipeline."""


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


def selected_model(config, provider, stage):
    """Allow a separate QC model while retaining provider defaults for other roles."""
    if stage in ("intake", "plan_qc") and config["qc_model"]:
        return config["qc_model"]
    if stage == "local_review" and config["local_review_model"]:
        return config["local_review_model"]
    return config["models"][provider]


def approved_profile_ids(state):
    """The plan's approved profiles; a plan approved before envelopes has those its files trigger."""
    profiles = state.get("approved_profiles")
    if profiles is None:
        return [p["id"] for p in state["execution"]["profiles"] if triggered(p, state["plan"]["files"])]
    return profiles


def approved_checks(state):
    """The mandatory checks of the plan's approved profiles."""
    return mandatory(approved_profile_ids(state), state['execution'])


class OutsideScope(Blocked):
    """An implementation proposal edits paths outside the approved envelope; refused whole, as feedback."""

    def __init__(self, paths):
        self.paths = sorted(paths)
        super().__init__("edit outside the approved scope: " + ", ".join(self.paths))


def local_evidence_valid(state):
    """Require passing execution and local review evidence for the commit being published."""
    evidence = state.get("local_evidence", {})
    head = state.get("pending", {}).get("head", state.get("head"))
    if not head or evidence.get("head") != head or not evidence.get("tree") or not evidence.get("local_commit"):
        return False
    if evidence.get("image") != state.get("execution_image"):
        return False
    checks = evidence.get("checks", [])
    names = [c.get("name") for c in checks]
    required = set(approved_checks(state)) | set(state['plan']['validation']['checks'])
    if len(names) != len(set(names)) or not required.issubset(names):
        return False
    if any(c.get("exit_code") != 0 or c.get("timed_out") or c.get("output_limited")
           or c.get("tree") != evidence["tree"] for c in checks):
        return False
    review = evidence.get("review", {})
    try:
        validate_review({k: v for k, v in review.items() if k not in ("head", "model")})
    except (Blocked, TypeError):
        return False
    return review["verdict"] == "approve"


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
    if stage == 'plan_qc':
        from design import report_limit
        limit = report_limit(criteria_count, objection_count)
    else:
        limit = 6000
    if len(json.dumps(report).encode()) > limit:
        raise Blocked("QC report too large")
    scope = report["scope"]
    if (not isinstance(scope, dict) or set(scope) != {"goal_ids", "fit", "conflicts"}
            or not isinstance(scope["goal_ids"], list) or not isinstance(scope["conflicts"], list)
            or not all(isinstance(v, str) and v.strip() for v in scope["goal_ids"] + scope["conflicts"])
            or not isinstance(scope["fit"], str) or not scope["fit"].strip()
            or len(scope["goal_ids"]) != len(set(scope["goal_ids"]))):
        raise Blocked("incomplete charter scope judgment")
    if charter is not None and not set(scope["goal_ids"]).issubset(charter["goals"]):
        raise Blocked("scope cites an unknown charter goal")
    decision = report["decision"]
    if decision in ("proceed", "approve") and (not scope["goal_ids"] or scope["conflicts"]):
        raise Blocked("acceptance contradicts charter scope")
    if decision in ("proceed", "approve") and (report["questions"] or report["concerns"]):
        raise Blocked("QC acceptance contradicts outstanding questions or concerns")
    if decision == "clarify" and not report["questions"]:
        raise Blocked("QC clarification requires questions")
    if decision == "revise" and (not report["concerns"] or report["questions"]):
        raise Blocked("QC revision requires concerns and no unanswered questions")


def design_roster(config):
    """Deduplicate every participating provider/model identity, retaining role provenance."""
    members = [(config["planner"], "plan"), (config["implementer"], "develop"),
               (config["qc"], "intake"), (config["local_reviewer"], "local_review")]
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
                   "image": state["agent_image"], "execution_image": state["execution_image"],
                   "bundle": state.get("bundle_id"), "charter": state.get("charter_id"),
                   "generation": state.get("design_generation"), "phase": state.get("design_phase"), "roster": design_roster(config),
                   "parent": state.get("design_parent"), "changed_files": state.get("approved_accumulated_files", [])})


def consensus_valid(state, config, require_fresh=True):
    """Require complete, fresh unanimous judgments; code cannot prove semantic alignment."""
    if require_fresh and state.get("design_phase") != "fresh":
        return False
    roster = design_roster(config)
    votes = state.get("design_votes", {})
    if not state.get("bundle_id") or not state.get("charter_id") or set(votes) != {r["id"] for r in roster}:
        return False
    binding = approval_binding(state, config)
    if not state.get("execution") or contract_errors(state['plan'], state['execution'], state.get("approved_profiles")):
        return False
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
            or not all(isinstance(v, str) and v.strip() for v in issue["evidence"])
            or len(json.dumps(issue)) > 4000):
        raise Blocked("design reopening requires bounded structured evidence")


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
    if not path_safe(path) or profile_protected(path, execution):
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


def merge_path_allowed(path, config, execution):
    """Autonomous merge accepts only configured low-risk paths that trigger no execution profile."""
    return (path.startswith(tuple(config["paths"]["autonomous_merge_prefixes"]))
            and not build_checked(path, execution))


def validate_plan(plan, config, execution):
    """Require a bounded, reviewable plan or explicit clarification questions."""
    fields = {"summary", "questions", "files", "steps", "tests", "responses", "validation", "publication"}
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
    # A proposal reaching outside the envelope is refused whole, before anything is applied.
    outside = {e["path"] for e in edits if isinstance(e["path"], str) and path_allowed(e["path"], config, execution)
               and not in_scope(e["path"], plan)}
    if outside:
        raise OutsideScope(outside)
    seen = set()
    for edit in edits:
        path, content = edit["path"], edit["content"]
        if not path_allowed(path, config, execution) or path in seen:
            raise Blocked("edit outside approved scope or duplicate path")
        if not isinstance(content, str) or "\x00" in content or len(content.encode()) > 128000:
            raise Blocked("invalid text replacement")
        if originals.get(path) == content:
            raise Blocked("unchanged replacement")
        seen.add(path)
    if len(json.dumps(result).encode()) > 256000:
        raise Blocked("implementation too large")
    return edits


def validate_review(review):
    """Reject contradictory approvals and incomplete structured findings."""
    if set(review) != presentation(review, {"verdict", "summary", "findings", "design_issue"}, {"headline"}):
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
MAX_PR_TEXT = 4000
PR_REVIEW_LIMIT = 24000


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
    if not isinstance(detail, str) or not detail.strip() or len(detail) > MAX_PR_TEXT:
        raise Blocked("finding detail must be bounded non-empty text")


def validate_pr_review(review, open_ids, withdrawn_ids):
    """Reject inconsistent PR reviews; dispositions answer exactly the reviewer's open and withdrawn findings.

    Local review keeps `validate_review`; only PR review carries line anchors and dispositions.
    """
    if not isinstance(review, dict) or set(review) != presentation(review, PR_REVIEW_FIELDS, {"headline"}):
        raise Blocked("invalid review fields")
    if review["verdict"] not in ("approve", "changes_requested", "reopen"):
        raise Blocked("invalid review verdict")
    if (not isinstance(review["summary"], str) or not isinstance(review["findings"], list)
            or not isinstance(review["dispositions"], list)):
        raise Blocked("invalid review content")
    if len(json.dumps(review)) > PR_REVIEW_LIMIT or len(review["findings"]) > MAX_PR_FINDINGS:
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
                or not isinstance(disposition["id"], str) or not isinstance(disposition["evidence"], str) or not disposition["evidence"].strip()
                or len(disposition["evidence"]) > MAX_PR_TEXT):
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


def protection_errors(classic, rules, rulesets, required_checks):
    """Return why server-side protection the App cannot bypass would not guard a merge race.

    Classic protection counts only when enforced for administrators. Ruleset rules count only
    from active rulesets reporting that the requesting App can never bypass them.
    """
    names, resolution = set(), False
    if classic and classic.get("enforce_admins", {}).get("enabled"):
        required = classic.get("required_status_checks") or {}
        if required.get("strict"):
            names |= set(required.get("contexts", [])) | {c["context"] for c in required.get("checks", [])}
        resolution = bool(classic.get("required_conversation_resolution", {}).get("enabled"))
    for rule in rules:
        ruleset = rulesets.get(rule.get("ruleset_id")) or {}
        if ruleset.get("enforcement") != "active" or ruleset.get("current_user_can_bypass") != "never":
            continue
        parameters = rule.get("parameters") or {}
        if rule.get("type") == "required_status_checks" and parameters.get("strict_required_status_checks_policy"):
            names |= {c["context"] for c in parameters.get("required_status_checks", [])}
        elif rule.get("type") == "pull_request" and parameters.get("required_review_thread_resolution"):
            resolution = True
    errors = []
    missing = sorted(set(required_checks) - names)
    if missing:
        errors.append("strict branch protection the App cannot bypass must require: " + ", ".join(missing))
    if not resolution:
        errors.append("branch protection the App cannot bypass must require conversation resolution")
    return errors


def merge_reasons(state, pr, files, checks, threads, reviews, config):
    """Return every failed merge gate; missing evidence always blocks."""
    reasons = []
    head = pr["head"]["sha"]
    if not config["merge_enabled"]:
        reasons.append("autonomous merge is disabled")
    if pr["state"] != "open" or pr["draft"] or pr.get("mergeable") is not True:
        reasons.append("PR is not open, ready and mergeable")
    if pr.get("mergeable_state") != "clean":
        reasons.append("GitHub mergeability is not clean")
    if pr["base"]["ref"] != config["base"] or pr["base"]["sha"] != state["base"]:
        reasons.append("base moved; a new plan is required")
    if pr["head"]["repo"]["full_name"] != config["repository"]:
        reasons.append("foreign head repository")
    if pr["head"]["ref"] != state["branch"] or head != state.get("head"):
        reasons.append("head changed outside the controller")
    if not approval_valid(state, config):
        reasons.append("approved plan missing or stale")
    if not local_evidence_valid(state):
        reasons.append("local test or review evidence missing or stale")
    reports = state.get("reviews", {})
    for provider in config["reviewers"]:
        report = reports.get(provider, {})
        if (report.get("head") != head or report.get("verdict") != "approve" or report.get("findings")
                or report.get("input_revision") != state.get("input_revision", 0)):
            reasons.append(provider + " approval missing or stale")
    ledger = state.get("findings", [])
    if state.get("pr_outbox"):
        reasons.append("PR review publication incomplete")
    if any(f["status"] == "open" for f in ledger):
        reasons.append("open review findings")
    withdrawn = [f["id"] for f in ledger if f["status"] == "withdrawn"]
    if withdrawn:
        reasons.append("withdrawn findings awaiting retirement: " + ", ".join(withdrawn))
    if not files or set(files) != set(state.get("changed_files", [])):
        reasons.append("PR file set differs from controller evidence")
    if not all(path_allowed(p, config, state["execution"]) and in_scope(p, state["plan"]) for p in files):
        reasons.append("unapproved path")
    if not all(merge_path_allowed(p, config, state["execution"]) for p in files):
        reasons.append("autonomous merge is limited to configured low-risk paths")
    for name in config["required_checks"]:
        candidates = [c for c in checks if c["name"] == name]
        if len(candidates) != 1:
            reasons.append("required check missing or ambiguous: " + name)
        else:
            check = candidates[0]
            if (check.get("head_sha") != head or check.get("status") != "completed"
                    or check.get("conclusion") != "success"
                    or check.get("app", {}).get("slug") != "github-actions"):
                reasons.append("required check unsuccessful or untrusted: " + name)
    if any(not t["isResolved"] for t in threads):
        reasons.append("unresolved review threads")
    latest = {}
    for review in reviews:
        if review["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[review["user"]["login"]] = review["state"]
    if "CHANGES_REQUESTED" in latest.values():
        reasons.append("outstanding human changes requested")
    return reasons
