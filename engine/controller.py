"""Durable issue-to-PR state machine. Only this trusted process writes to GitHub."""

import difflib
import hashlib
import json
import re
import sys
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

import actions
import candidates
import change_attention
import checks
import classification
import commands
import conversation
import dashboard_runs
import design
import design_batch
import design_eligibility
import feedback
import file_inventory
import implementation_bundle
import markup
import pr_description
import pr_template
import protocol
import readiness
import reason_guidance
import review_state
import review_timing
import reviews as review_rules
import revisions
import sessions
from actions import BUNDLE_REASON
from bundle import Bundle
from checkpoints import LIMIT as CHECKPOINT_LIMIT, MARKER, STATE_PREFIX as STATE_PREFIX, CheckpointError, Checkpoints
from dashboard import SUMMARY_LIMIT as SUMMARY_LIMIT, Dashboard
from identities import Identities
from markup import link, text
from model_config import assigned, parse as parse_models, resolve
from policy import (
    MODEL_RESPONSE_LIMIT,
    Blocked,
    RunnerTerminated,
    SessionFailure,
    approval_binding,
    approval_valid,
    consensus_valid,
    design_roster,
    digest,
    instruction_matches,
    path_allowed,
    plan_output_contract,
    selected_model,
    task_branch,
    validate_design_issue,
    validate_edits,
    validate_plan,
    validate_pr_review,
    validate_qc,
)
from publication import INLINE_BLOB_LIMIT as INLINE_BLOB_LIMIT, PR_TITLE_LIMIT as PR_TITLE_LIMIT, Publication
from repository_reader import RepositoryReader
from vocab import JUDGED_STAGES, MERGED_STAGES, SUSPENDED_STAGES, TERMINAL_STAGES

# Correct malformed intake and planner output without spending a semantic design-revision round.
PLAN_OUTPUT_REPAIRS = 2
INTAKE_OUTPUT_REPAIRS = 2
MAX_UNCERTAIN = 2
UNCERTAIN_MESSAGE = reason_guidance.UNCERTAIN_MODEL
# Context bytes `ask()` adds at dispatch (the repository commit and refs, and the operation contract).
DISPATCH_ALLOWANCE = 2000
# A review context's diff of one file; the reviewer reads the rest in the repository.
MAX_DIFF = 64000
DIFF_TRUNCATED = "[diff truncated; inspect the pinned repository]\n"
ESCALATION_REASON = "High-risk or consequential work requires human intervention."
LEGACY_BUNDLE_REASON = reason_guidance.LEGACY_BUNDLE
PROVIDERS = {"claude", "codex", "copilot"}
PATH_POLICY = {
    "editable_extensions",
    "protected_names",
    "protected_name_prefixes",
    "protected_prefixes",
    "protected_files",
    "protected",
    "generated",
}
REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
COMMIT = re.compile(protocol.SHA_PATTERN)


def strings(value, limit=32):
    """Return whether a value is a bounded list of distinct non-empty strings."""
    return (
        isinstance(value, list)
        and len(value) <= limit
        and all(isinstance(v, str) and v.strip() and len(v) <= 240 for v in value)
        and len(set(value)) == len(value)
    )


def _bounded_int(value, low, high, message):
    """Reject non-integers (including bools) and values outside an existing budget range."""
    if type(value) is not int or not low <= value <= high:
        raise Blocked(message)


def validate_config(config):
    """Reject unsafe or internally inconsistent operator configuration."""
    pr_template.validate(config.get("pr_template_sections"))
    if type(config.get("conventional_commits", False)) is not bool:
        raise Blocked("conventional_commits must be a boolean")
    removed = {
        "execution_profiles",
        "required_checks",
        "merge_enabled",
        "local_reviewer",
        "local_review_model",
        "max_local_fix_rounds",
        "local_timeout_seconds",
        "local_check_timeout_seconds",
        "max_validation_repairs",
    }
    if removed & set(config):
        raise Blocked("obsolete target execution settings; migrate the target configuration for the MVP")
    if (
        not isinstance(config["repository"], str)
        or not REPOSITORY.fullmatch(config["repository"])
        or not isinstance(config["base"], str)
        or not BRANCH.fullmatch(config["base"])
        or ".." in config["base"]
        or config["base"].startswith(("/", "-"))
    ):
        raise Blocked("invalid repository or base branch")
    if not isinstance(config["bot_login"], str) or not config["bot_login"].endswith("[bot]"):
        raise Blocked("bot_login must name the GitHub App's [bot] account")
    if "agents" in config:
        try:
            parse_models(config)
        except (ValueError, TypeError) as error:
            raise Blocked("invalid model configuration: " + str(error)) from None
        if type(config["autonomous"]) is not bool:
            raise Blocked("invalid autonomy configuration")
    else:
        reviewers = config["reviewers"]
        if not {"claude", "codex"}.issubset(reviewers) or len(set(reviewers)) != len(reviewers):
            raise Blocked("independent Claude and Codex reviews are mandatory")
        if not set(reviewers).issubset(PROVIDERS):
            raise Blocked("unknown reviewer")
        if config["planner"] not in PROVIDERS or config["implementer"] not in PROVIDERS:
            raise Blocked("unknown planner or implementer")
        if (
            type(config["autonomous"]) is not bool
            or config["qc"] not in PROVIDERS
            or not isinstance(config["qc_model"], str)
        ):
            raise Blocked("invalid autonomy or QC configuration")
        if config["autonomous"] and config["qc"] == config["planner"]:
            raise Blocked("QC must use a different provider from the planner")
    _bounded_int(config["max_plan_revisions"], 0, 3, "invalid plan revision budget")
    _bounded_int(config.get("max_review_repairs"), 0, 8, "invalid review repair budget")
    if "agents" not in config and (
        set(config["models"]) != PROVIDERS or not all(isinstance(v, str) for v in config["models"].values())
    ):
        raise Blocked("model map must name each provider")
    documents = config["policy_documents"]
    if not strings(documents, 10) or not all(
        not PurePosixPath(p).is_absolute() and ".." not in p.split("/") for p in documents
    ):
        raise Blocked("policy documents must be relative repository paths")
    paths = config["paths"]
    if not isinstance(paths, dict):
        raise Blocked("invalid editable path policy")
    legacy = "editable_extensions" in paths
    allowed = PATH_POLICY if legacy else (PATH_POLICY - {"editable_extensions"}) | {"protected_extensions"}
    if (
        (set(paths) != allowed if legacy else not {"protected_extensions"} <= set(paths) <= allowed)
        or not all(strings(v, 32) for v in paths.values())
        or legacy
        and not paths["editable_extensions"]
        or not all(
            e.startswith(".") and len(e) > 1
            for e in paths.get("editable_extensions", paths.get("protected_extensions", []))
        )
        or not all(p.endswith("/") for p in paths.get("protected_prefixes", []))
        or not all(checks.relative_path(p) for p in paths.get("protected", []) + paths.get("generated", []))
    ):
        raise Blocked("invalid editable path policy")
    bounds = config.get("limits", {})
    if (
        not isinstance(bounds, dict)
        or not set(bounds) <= checks.DEFAULT_LIMITS.keys()
        or any(value is not None and (type(value) is not int or value < 1) for value in bounds.values())
    ):
        raise Blocked("limits must contain known keys with positive integers or null")
    if not 0 <= config["max_fix_rounds"] <= 3 or not 1 <= config["max_model_calls"] <= 64:
        raise Blocked("invalid budget")
    _bounded_int(config["max_design_reopens"], 0, 3, "invalid design reopen budget")
    if not 1 <= config["max_outcome_issues"] <= 10 or not 1 <= config["max_outcome_lessons"] <= 3:
        raise Blocked("invalid outcome retrieval bound")
    if "engine" in config and not (isinstance(config["engine"], str) and COMMIT.fullmatch(config["engine"])):
        raise Blocked("engine must name a full engine commit")


def config_digest(config):
    """Bind tasks to their configuration; the service's `engine` pin is excluded, so upgrades keep tasks valid."""
    return digest({k: v for k, v in config.items() if k != "engine"})


# Tasks without an ownership marker predate it and belong to the Actions controller.
ACTIONS = "actions"
OUTCOMES = ("continue", "wait-until", "await-human", "blocked", "terminal")
# Fallback poll for timed waits; schedulers may also re-enqueue earlier when inputs change.
WAIT_SECONDS = 300
# Request fields derived from current checkpoint size rather than from the intended work.
VOLATILE_REQUEST = ("review_output_budget",)
# Audit room kept free when retaining a completed model result for recovery.
RESULT_MARGIN = 4000


@dataclass(frozen=True)
class Outcome:
    """How a scheduler should treat a task after one tick; truthy when the tick made progress."""

    kind: str
    progressed: bool = False
    retry_after: int | None = None

    def __bool__(self):
        """Keep `if controller.advance(...)` meaning "this tick changed the task"."""
        return self.progressed


def transport_size(context):
    """Bytes a model receives; controller-only keys never reach it."""
    return len(json.dumps({k: v for k, v in context.items() if not k.startswith("controller_")}).encode())


def diff_preview(value, limit):
    """Shorten a diff with exactly one engine-written truncation marker."""
    content = value.removesuffix(DIFF_TRUNCATED)
    return content.encode()[:limit].decode(errors="ignore").rstrip("\n") + "\n" + DIFF_TRUNCATED


def changes(before, after, execution=None, dependencies=None):
    """Every changed file carries a bounded diff; the exact head remains available through repository tools."""
    result = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        left, right = lf_lines(old or ""), lf_lines(new or "")
        expensive = candidates.diff_work(left, right) > candidates.DIFF_WORK_LIMIT
        lines = [] if expensive else list(difflib.unified_diff(left, right, "a/" + path, "b/" + path))
        body = lines[2:]
        diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
        truncated = expensive or len(diff.encode()) > MAX_DIFF
        if expensive:
            diff = "[diff comparison limit reached; inspect the complete file in the repository]\n"
        elif truncated:
            diff = diff_preview(diff, MAX_DIFF)
        result.append(
            {
                "path": path,
                "status": "added" if old is None else "deleted" if new is None else "modified",
                "base_sha256": sha256(old),
                "head_sha256": sha256(new),
                "base_bytes": len((old or "").encode()),
                "head_bytes": len((new or "").encode()),
                "added": len(right) if expensive else sum(line.startswith("+") for line in body),
                "removed": len(left) if expensive else sum(line.startswith("-") for line in body),
                "diff": diff,
                "truncated": truncated,
            }
        )
    return result


def lf_lines(text):
    """Source lines as Git and the repository reader count them: LF-delimited, each keeping its terminator."""
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def sha256(text):
    """SHA-256 of a file's UTF-8 bytes, or None when absent."""
    return None if text is None else hashlib.sha256(text.encode()).hexdigest()


class Controller:
    """Advance at most one bounded stage, preserving intent before remote mutations."""

    STAGE_METHODS = {
        "intake": "quality_control",
        "plan_qc": "quality_control",
        "plan": "plan",
        "implement": "implement",
        "publish": "publish",
        "review": "review",
        "gates": "gates",
    }

    def __init__(
        self,
        github,
        agents,
        config,
        bundle_root=None,
        target_root=None,
        controller_id=ACTIONS,
        bundle=None,
        code=None,
        pulls=None,
        enroll=None,
        report_cache=None,
    ):
        """Inject transports so complete lifecycles can be tested without credentials.

        `bundle`, when given, is the instance the agents also use; otherwise it is built here.
        """
        self.gh, self.agents, self.config = github, agents, config
        self.code, self.pulls = code or github, pulls or github
        self.controller_id = controller_id
        self.enroll = enroll or (lambda: None)
        self.report_cache = report_cache
        self.comment_id = None
        self.checkpoints = None
        self.state = None
        self.issue = None
        self.permissions = {}
        self.reader = RepositoryReader(self.code, self.pulls)
        self.trees = self.reader.trees
        self.identities = Identities(config)
        self.bundle = bundle or Bundle(bundle_root, target_root)
        self.stage_synced = None
        self.foreign = False
        self.consuming = None
        self.stage_sync_warned = False
        self.stage_fallback = False

    def checkpoint(self):
        """Persist authoritative progress independently of conversation delivery."""
        conversation.compact(self.state)
        operation = review_state.operation(self.state)
        if (
            operation
            and operation["status"] == "completed"
            and self.consuming is not None
            and (operation["id"], operation["attempt"]) == self.consuming
        ):
            # The stage holds the result; this save (of whichever state copy it keeps) persists its use.
            operation["status"] = "consumed"
            operation.pop("response", None)
        if operation and "response" in operation:
            self.classification_room(self.state, CHECKPOINT_LIMIT - RESULT_MARGIN)
            review_timing.make_room(
                self.state,
                CHECKPOINT_LIMIT - RESULT_MARGIN,
                lambda value: len(self.checkpoints.audit_body(value).encode()),
            )
        if (
            operation
            and "response" in operation
            and len(self.checkpoints.audit_body(self.state).encode()) > CHECKPOINT_LIMIT - RESULT_MARGIN
        ):
            response = operation["response"]
            if operation.get("stage") in classification.STAGES and isinstance(response, dict):
                response.pop("classification", None)
            if len(self.checkpoints.audit_body(self.state).encode()) > CHECKPOINT_LIMIT - RESULT_MARGIN:
                # Too large to retain: recovery treats the completion as lost, within the allowance.
                del operation["response"]
        self.classification_room(self.state, CHECKPOINT_LIMIT - 1000)
        review_timing.make_room(
            self.state, CHECKPOINT_LIMIT - 1000, lambda value: len(self.checkpoints.audit_body(value).encode())
        )
        self.checkpoints.save(self.state)

    def save(self, publish_events=True, outboxes=True):
        """Persist audit and plan artifacts before publishing their concise comment summary.

        `outboxes=False` publishes the issue conversation only, leaving PR reviews and comment
        edits for a later tick: a failure's own notice must not replay what just failed.
        """
        state = self.state
        self.refresh_classification()
        self.record_outcome()
        if publish_events:
            self.transition(state)
        # Plans are linked from the status comment; an intent saved by an earlier version is spent.
        state.pop("plan_event_intent", None)
        self.checkpoint()
        self.sync_stage()
        if publish_events and conversation.publish(state, self.gh, self.config, self.checkpoints.reports):
            self.refresh_classification()
            self.checkpoint()
        if publish_events and outboxes:
            actions.publish(state, self.gh, self.checkpoint)
            review_rules.publish(state, self.pulls, self.config, self.checkpoint)
        body = self.summary_body()
        if len(body.encode()) > SUMMARY_LIMIT:
            raise Blocked("summary exceeds comment limit")
        if self.comment_id:
            self.checkpoints.request(f"issues/comments/{self.comment_id}", "PATCH", {"body": body})
        else:
            result = self.checkpoints.request(f"issues/{self.issue['number']}/comments", "POST", {"body": body})
            self.comment_id = result["id"]
        self.checkpoints.dirty = False
        self.stage_fallback = False
        if self.stage_sync_warned:
            fallback = "Stage: **" + state["stage"] + "** (label sync unavailable).\n"
            body = body.replace("## Concurro\n", "## Concurro\n\n" + fallback, 1)
            self.checkpoints.request(f"issues/comments/{self.comment_id}", "PATCH", {"body": body})
            self.stage_fallback = True

    def transition(self, state):
        """Post the comment a stage change asks of a human, if any."""
        artifact = self.checkpoints.plan if self.checkpoints else None
        plan_url = (
            protocol.artifact_url(self.config["repository"], state["issue"], artifact["number"]) if artifact else None
        )
        actions.transition(state, self.config, (self.issue or {}).get("user", {}).get("login"), plan_url)

    def answerer(self, login):
        """Whether a login may answer an Input needed request: the issue's author or a writer."""
        return not self.identities.ours(login) and (
            login == self.issue.get("user", {}).get("login") or self.permitted(login)
        )

    def attention_paths(self):
        """Read the current candidate inventory without retaining another derived list."""
        if self.state.get("candidate"):
            return file_inventory.candidate_paths(self.state, self.reader)
        return self.state.get("changed_files", [])

    def _dashboard(self):
        """Create a fresh view so render-only caches never cross controller transitions."""
        return Dashboard(
            self.state, self.config, self.checkpoints, self.pr_url, self.waiting_revision, self.attention_paths
        )

    def summary_body(self):
        """Render from the current task through its read-only dashboard view."""
        return self._dashboard().summary_body()

    def summary_lines(self, compact):
        """Render from the current task through its read-only dashboard view."""
        return self._dashboard().summary_lines(compact)

    def activity_line(self):
        """Render from the current task through its read-only dashboard view."""
        return self._dashboard().activity_line()

    def classification_room(self, state, limit):
        """Prefer valid core evidence over optional archived metadata at checkpoint boundaries."""
        classification.make_room(state, limit, lambda value: len(self.checkpoints.audit_body(value).encode()))

    def classify(self, report, provider, stage, limit, reviewer=None):
        """Allocate advisory metadata only from space left after the core report and its copies."""
        core = classification.core(report)
        core_size = classification.size(core)
        self.classification_room(self.state, CHECKPOINT_LIMIT - 12000 - 4 * core_size)
        remaining = deepcopy(self.state)
        review_state.operation(remaining).pop("response", None)
        room = CHECKPOINT_LIMIT - len(self.checkpoints.audit_body(remaining).encode()) - 12000 - 4 * core_size
        allowance = min(limit - core_size - 50, room // 6)
        if provider == "copilot" and "agents" not in self.config:
            agent = {
                "profile": provider,
                "harness": provider,
                "provider": None,
                "model": (reviewer or {}).get("model") or selected_model(self.config, provider, stage),
                "effort": None,
            }
        else:
            agent = resolve(self.config, provider, stage, reviewer).record()
        return classification.attach(report, self.state, agent, stage, allowance)

    def refresh_classification(self):
        """Keep one small complete estimate, preserving its binding across later incomplete rounds."""
        value = classification.snapshot(self.state, self.config)
        if value is None:
            return
        previous = self.state.get("classification_snapshot")
        self.state["classification_snapshot"] = value
        if len(self.checkpoints.audit_body(self.state).encode()) > CHECKPOINT_LIMIT - RESULT_MARGIN:
            if previous is None:
                self.state.pop("classification_snapshot", None)
            else:
                self.state["classification_snapshot"] = previous

    def classification_row(self):
        """Render from the current task through its read-only dashboard view."""
        return self._dashboard().classification_row()

    def artifact_url(self, number):
        """Render from the current task through its read-only dashboard view."""
        return self._dashboard().artifact_url(number)

    def current_step(self):
        """Render from the current task through its read-only dashboard view."""
        return self._dashboard().current_step()

    def step_row(self, index, name, current):
        """Render from the current task through its read-only dashboard view."""
        return self._dashboard().step_row(index, name, current)

    def action_line(self):
        """Render from the current task through its read-only dashboard view."""
        return self._dashboard().action_line()

    def progress_rows(self, steps, compact=False):
        """Render from the current task through its read-only dashboard view."""
        return self._dashboard().progress_rows(steps, compact)

    def diagnostics_table(self):
        """Render from the current task through its read-only dashboard view."""
        return self._dashboard().diagnostics_table()

    def previous_runs(self):
        """Render from the current task through its read-only dashboard view."""
        return self._dashboard().previous_runs()

    def sync_stage(self):
        """Mirror durable progress to labels without letting display failures authorize or block work."""
        if not self.state or self.stage_synced == self.state["stage"] or self.stage_sync_warned:
            return
        try:
            self.gh.sync_stage(self.state["issue"], self.state["stage"])
            self.stage_synced = self.state["stage"]
        except Exception:
            self.stage_sync_warned = True
            print("Concurro stage label sync failed; durable state retained for retry.", file=sys.stderr)

    def load(self, comments):
        """Load verified artifacts or an unmigrated legacy checkpoint from the configured App."""
        self.checkpoints = Checkpoints(self.gh, self.config, self.issue["number"], self.report_cache, self.reader)
        self.state = self.checkpoints.load(comments)
        if self.state:
            dashboard_runs.retain_boundaries(self.state)
            self.state.get("review_batch", {}).pop("active", None)
        comment = self.checkpoints.comment
        self.comment_id = comment["id"] if comment else None
        self.stage_fallback = bool(comment and comment["body"].startswith(MARKER + "\n## Concurro\n\nStage: **"))

    def source(self, sha):
        """Read the approved base from upstream and published candidates from the fork."""
        return self.reader.source(self.state, sha)

    def tree(self, sha):
        """Fetch a complete tree; never silently treat truncation as missing files."""
        return self.reader.tree(self.state, sha)

    def file_manifest(self, sha, paths):
        """Identify every planned file at a pinned commit; agents read its contents from Git."""
        return self.reader.file_manifest(self.state, sha, paths)

    def files(self, sha, paths, inspection=False):
        """Read regular UTF-8 blobs without imposing a source-file size policy."""
        return self.reader.files(self.state, sha, paths, inspection)

    def context(self, changed=None):
        """Bind issue requirements and trusted repository guidance to the plan base.

        Instructions apply to the primary files and the files the change touches (by default, the PR's).
        """
        changed = self.state.get("changed_files", []) if changed is None else changed
        targets = [*self.state.get("plan", {}).get("files", []), *changed]
        paths = list(self.config["policy_documents"])
        tree = self.tree(self.state["base"])
        paths += [p for p in tree if p.startswith(".github/instructions/") and p.endswith(".md")]
        policies = self.files(self.state["base"], paths)
        # Review instructions are data for review stages; select matching path scopes.
        selected = {}
        for path, content in policies.items():
            if not content:
                continue
            if path.startswith(".github/instructions/"):
                match = re.search(r'^applyTo: "([^"]+)"', content, re.MULTILINE)
                if not match or not any(
                    instruction_matches(p, pattern.strip()) for p in targets for pattern in match[1].split(",")
                ):
                    continue
            selected[path] = content
        intake = self.state.get("qc", {}).get("intake", {})
        assessment = next((v for v in reversed(self.state.get("qc_history", [])) if v["stage"] == "intake"), {})
        concerns = (
            intake.get("concerns", [])
            if intake.get("decision") == "proceed"
            and assessment.get("base") == self.state["base"]
            and assessment.get("requirements_digest") == digest(self.state["requirements"])
            else []
        )
        return {
            "issue": self.state["requirements"],
            "policy": selected,
            "edit_policy": plan_output_contract(self.config)["edit_policy"],
            "limits": checks.limits(self.config),
            **({"intake_concerns": deepcopy(concerns)} if concerns else {}),
            "plan": self.state.get("plan"),
            "base": self.state["base"],
            "bundle_id": self.bundle.identity,
            "charter": self.bundle.charter,
            "prior_outcomes_untrusted": self.state.get("prior_outcomes", []),
        }

    def repository_refs(self, candidate):
        """Real branch names at the commits a call reads; a candidate is committed on the task branch."""
        state = self.state
        refs = {self.config["base"]: state["base"]}
        branch, head = state.get("branch"), state.get("head")
        if head:
            refs[branch] = head
        if candidate:
            refs[branch] = head or state["base"]
            if head:
                refs["concurro/published"] = head
        return {"refs": refs, "head": branch if head or candidate else self.config["base"]}

    def prepare_context(self, provider, stage, context):
        """Freeze and bound request inputs without spending or dispatching a call."""
        self.require_current_bundle()
        if stage in classification.STAGES and "prior_outcomes_untrusted" in context:
            context = {
                **context,
                "prior_outcomes_untrusted": classification.without_hints(context["prior_outcomes_untrusted"]),
            }
        context = {
            **context,
            "repository_commit": self.state.get("head", self.state["base"]),
            "repository": self.repository_refs("controller_candidate" in context),
        }
        if "agents" in self.config:
            context["agent"] = resolve(self.config, provider, stage).record()
        if transport_size(context) > self.config["max_context_bytes"]:
            context = classification.context_core(context)
        context = feedback.prepare(context)
        size = transport_size(context)
        if size > self.config["max_context_bytes"]:
            raise Blocked(
                f"{stage} context exceeds the size limit ({size} of {self.config['max_context_bytes']} bytes); "
                "narrow the change"
            )
        return context

    def ask(self, provider, stage, context):
        """Debit the persistent attempt budget before contacting a model.

        A retried or recovered call keeps its original response contract: recorded volatile
        fields replace recomputed ones in the caller's context, which validates the response.
        """
        caller = context
        context = self.prepare_context(provider, stage, context)
        operation = self.operation(provider, stage, context)
        caller.update(operation["contract"])
        context.update(operation["contract"])
        if stage == "plan_qc":
            session = sessions.prepare(self.state, self.config, provider, stage, context)
            design_eligibility.prepare(self.state, context, session, operation)
        retained = self.recover(operation)
        if retained is not None:
            return retained
        if self.state["calls"] >= self.config["max_model_calls"]:
            raise Blocked("model-call budget exhausted")
        credential = self.agents.credential(
            provider, stage, {"repository": self.config["repository"], "issue": self.state["issue"]}
        )
        for attempt in range(2):
            if self.state["calls"] >= self.config["max_model_calls"]:
                raise Blocked("model-call budget exhausted")
            session = sessions.prepare(self.state, self.config, provider, stage, context)
            if stage == "plan_qc":
                design_eligibility.prepare(self.state, context, session, operation)
            request = {**context, "controller_credential": credential.record()}
            if session:
                request["controller_session"] = session
            self.begin_attempt(operation, context)
            self.charge_call(credential)
            self.save()
            try:
                try:
                    response = self.agents.ask(provider, stage, request)
                finally:
                    observed = review_timing.normalize(getattr(self.agents, "timing", None))
                    if observed:
                        operation["timing"] = observed
            except Exception as error:
                # Timeouts and killed containers may have completed upstream; reported errors did not.
                operation["status"] = (
                    "uncertain"
                    if (getattr(error, "uncertain", False) or getattr(error, "result_uncertain", False))
                    else "failed"
                )
                if isinstance(error, RunnerTerminated):
                    operation["runner_termination"] = str(error)
                    if session:
                        sessions.restart(self.state, session)
                    self.save()
                    raise
                if not isinstance(error, SessionFailure) or not session:
                    raise
                design_eligibility.restart(self.state, session)
                sessions.restart(self.state, session)
                self.save()
                if attempt:
                    raise Blocked("provider session failed twice; a fresh session is prepared for retry") from None
                continue
            if session:
                self.record_session_turn(session, self.state["calls"], context, response)
            self.complete(operation, response)
            return response

    def charge_call(self, credential):
        """Debit the in-memory task and credential ledgers and return the assigned call number."""
        self.state["calls"] += 1
        used = self.state.setdefault("credentials", {}).setdefault(
            credential.id, {k: v for k, v in credential.record().items() if k != "id"} | {"calls": 0}
        )
        used["calls"] += 1
        return self.state["calls"]

    def record_session_turn(self, session, call, request, response):
        """Record one completed turn and its request/response binding without persisting it."""
        current = self.state["sessions"][session["role"]]
        current["turn"] += 1
        current["completed"] = {"call": call, "request": digest(request), "response": digest(response)}

    def release_result(self):
        """Clear the consumption cursor so a later checkpoint retains the selected response."""
        self.consuming = None

    @staticmethod
    def next_uncertain_count(operation):
        """Count an unresolved prior attempt without mutating its operation record."""
        return operation["uncertain"] + (operation["status"] in ("started", "uncertain"))

    @staticmethod
    def require_uncertain_budget(count):
        """Refuse another attempt once the existing uncertainty allowance is exhausted."""
        if count >= MAX_UNCERTAIN:
            raise Blocked(UNCERTAIN_MESSAGE)

    def operation(self, provider, stage, context):
        """Bind a model call to its intended work, so incidental request changes keep its allowance."""
        work = {
            k: v for k, v in classification.context_core(feedback.restore(context)).items() if k not in VOLATILE_REQUEST
        }
        identifier = digest([self.state["issue"], provider, stage, digest(work)])
        current = review_state.operation(self.state)
        if not current or current["id"] != identifier or current["status"] == "consumed":
            current = review_state.slot(self.state)["operation"] = {
                "id": identifier,
                "provider": provider,
                "stage": stage,
                "request": None,
                "attempts": 0,
                "uncertain": 0,
                "attempt": None,
                "status": "new",
                "contract": {k: context[k] for k in VOLATILE_REQUEST if k in context},
            }
            if "agents" in self.config:
                current["agent"] = resolve(self.config, provider, stage).record()
            self.consuming = None
        return current

    def recover(self, operation):
        """Return a durably retained result not yet consumed; a completion without one was lost."""
        if operation.get("runner_termination"):
            raise RunnerTerminated(operation["runner_termination"])
        if operation["status"] != "completed":
            return None
        if "response" in operation:
            self.consuming = (operation["id"], operation["attempt"])
            return deepcopy(operation["response"])
        operation["status"] = "uncertain"
        return None

    def begin_attempt(self, operation, context):
        """Persist each attempt before dispatch; unresolved prior attempts count as uncertain."""
        if operation["status"] in ("started", "uncertain"):
            operation["uncertain"] = self.next_uncertain_count(operation)
        self.require_uncertain_budget(operation["uncertain"])
        operation.pop("timing", None)
        operation.update(
            attempts=operation["attempts"] + 1, attempt=uuid.uuid4().hex, status="started", request=digest(context)
        )

    def complete(self, operation, response):
        """Checkpoint the result itself, when it fits, until the stage durably consumes it."""
        operation["status"] = "completed"
        retained = deepcopy(response)
        if operation.get("stage") in classification.STAGES and isinstance(retained, dict):
            value = classification.normalize(retained.pop("classification", None))
            retained["classification"] = value
        operation["response"] = retained
        self.checkpoint()
        self.consuming = (operation["id"], operation["attempt"])

    def permitted(self, login):
        """Cache fresh writer permissions within this invocation only."""
        if login not in self.permissions:
            self.permissions[login] = self.gh.permitted(login)
        return self.permissions[login]

    def enrollment(self):
        """Require the current opt-in label to have been applied by a current writer."""
        if not any(label["name"] == "concurro" for label in self.issue.get("labels", [])):
            return None
        events = self.gh.pages(f"issues/{self.issue['number']}/events")
        events = [
            e for e in events if e["event"] in ("labeled", "unlabeled") and e.get("label", {}).get("name") == "concurro"
        ]
        event = events[-1] if events else None
        actor = event.get("actor") if event else None
        if not actor or event["event"] != "labeled" or not self.permitted(actor["login"]):
            return None
        return {"actor": actor["login"], "event": event["id"]}

    def issue_digest(self):
        """Bind issue identity to its title and body, excluding discussion and metadata."""
        return digest([self.issue["title"], self.issue["body"]])

    def requirements(self, comments):
        """Snapshot issue text and author/writer replies without treating them as authority."""
        author = self.issue.get("user", {}).get("login")
        discussion = []
        for comment in comments:
            body, login = comment["body"], comment["user"]["login"]
            if body.startswith(MARKER) or body.strip().startswith("/concurro ") or self.identities.ours(login):
                continue
            if login == author or self.permitted(login):
                discussion.append({"author": login, "body": body})
        return {"title": self.issue["title"], "body": self.issue["body"], "discussion": discussion[-20:]}

    def initialize(self, comments, mode, command_id=0, enrollment=None):
        """Create a bounded task while preserving explicit manual mode compatibility."""
        base = self.pulls.repo("git/ref/heads/" + self.config["base"])["object"]["sha"]
        self.enroll()
        self.state = {
            "version": 2,
            "issue": self.issue["number"],
            "mode": mode,
            "controller": self.controller_id,
            "stage": "intake",
            "enrollment": enrollment,
            "base": base,
            "calls": 0,
            "round": 0,
            "plan_revisions": 0,
            "reviews": {},
            "command_id": command_id,
            "requirements": self.requirements(comments),
            "issue_digest": self.issue_digest(),
            "config_digest": config_digest(self.config),
            "bundle_id": self.bundle.identity,
            "implementation_skill": self.bundle.hashes[implementation_bundle.SKILL],
            "charter_id": digest(self.bundle.charter),
            "charter": self.bundle.charter,
            "execution": self.config["paths"],
            "design_generation": 0,
            "design_reopens": 0,
            "design_votes": {},
            "design_history": [],
            "design_phase": "cooperative",
        }
        self.state.update({key: self.config[key] for key in ("upstream", "fork", "config_sha") if key in self.config})
        if "agents" in self.config:
            self.state["model_configuration"] = {key: deepcopy(self.config[key]) for key in ("models", "agents")}
        self.state["prior_outcomes"] = self.retrieve_outcomes()
        self.welcome()
        self.save()

    def welcome(self):
        """Introduce the pinned team once, using the same recoverable outbox as other notices."""
        roles = [
            ("Intake", assigned(self.config, "triage"), "intake", None),
            ("Planning", assigned(self.config, "planner"), "plan", None),
        ]
        roles += [("Design review", member["provider"], "plan_qc", member) for member in design_roster(self.config)]
        roles += [("Implementation", assigned(self.config, "implementer"), "develop", None)]
        roles += [("PR review", name, "review", None) for name in assigned(self.config, "pr_reviewers")]
        team = {}
        for role, name, stage, member in roles:
            if name == "copilot" and "agents" not in self.config:
                model, effort = selected_model(self.config, name, stage) or "CLI default", "CLI default"
            else:
                profile = resolve(self.config, name, stage, member).profile
                model, effort = profile.model or "CLI default", profile.effort
            team.setdefault(role, []).append(markup.code(model, cell=True) + " (" + text(effort) + ")")
        rows = [[role, " · ".join(members)] for role, members in team.items()]
        actor = (self.state.get("enrollment") or {}).get("actor", "")
        mention = (
            "@" + actor + " "
            if isinstance(actor, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", actor)
            else ""
        )
        conversation.queue(
            self.state,
            "",
            mention
            + "I've picked up this issue and will start digging in!\n\n"
            + "Here's the team which has currently been assigned:\n\n"
            + markup.table(["Role", "Model (effort)"], rows),
        )

    def revise_plan(self, stage, reason, invalidate=True, save_exhausted=True):
        """Limit automatic plan revisions across QC feedback and changing inputs."""
        return revisions.RevisionBudget(self.state, self.config).revise(
            stage,
            reason,
            invalidate_design=self.invalidate_design,
            save=self.save,
            invalidate=invalidate,
            save_exhausted=save_exhausted,
        )

    def plan_revision_limit(self):
        """Return the pinned automatic allowance plus explicit writer grants."""
        return revisions.RevisionBudget(self.state, self.config).limit()

    def waiting_revision(self):
        """Return the already-invalidated revision waiting for capacity."""
        return revisions.RevisionBudget(self.state, self.config).waiting()

    def refuse_revision_command(self, comment, exhausted=False):
        """Consume an inapplicable writer command with one durable notice, without suspending work."""
        return self._command_handler().refuse_revision_command(comment, exhausted)

    def require_pr_unchanged(self, message):
        """Read the recorded PR once and require the same head, open and not merged."""
        state = self.state
        pr = self.pulls.repo(f"pulls/{state['pr']}")
        if pr["head"]["sha"] != state["head"] or pr["state"] != "open" or pr.get("merged"):
            raise Blocked(message)

    @contextmanager
    def consume_on_refusal(self, comment, *, save=True):
        """Consume only Blocked validation refusals, preserving the caller's save policy."""
        state = self.state
        try:
            yield
        except Blocked:
            state["command_id"] = comment["id"]
            if save:
                self.save()
            raise

    def reconciled_changed_paths(self):
        """Read accumulated PR paths once and require a nonempty, policy-allowed set."""
        changed = self.reader.pr_paths(self.state, self.state["pr"])
        if not changed or not all(path_allowed(path, self.config) for path in changed):
            raise Blocked(reason_guidance.ACCUMULATED_CHANGES)
        return changed

    def extend_plan_revisions(self, comment, amount, comments):
        """Atomically grant bounded revision capacity and resume the recorded revision, never waive findings."""
        return self._command_handler().extend_plan_revisions(comment, amount, comments)

    def invalidate_design(self, reason):
        """Archive bounded judgment evidence and invalidate all approval generations."""
        state = self.state
        self.remember_legacy_pr_body()
        if state.get("plan_id"):
            state["design_history"].append(
                {
                    "generation": state["design_generation"],
                    "plan": state["plan_id"],
                    "summary": state["plan"]["summary"][:500],
                    "parent": state.get("head"),
                    "reason": reason[:1000],
                    "candidate_digest": digest(state.get("candidate")),
                    "votes": [
                        {
                            "identity": v["identity"],
                            "binding": v["binding"],
                            "decision": v["report"]["decision"],
                            "report_digest": digest(v["report"]),
                            "concerns": v["report"]["concerns"],
                        }
                        for v in state.get("design_votes", {}).values()
                    ],
                }
            )
        if not state.get("review_batch", {}).get("active"):
            state.pop("review_batch", None)
        state["design_generation"] += 1
        state["input_revision"] = state.get("input_revision", 0) + 1
        withdrawn = review_rules.withdraw(state)
        if state.get("pr"):
            review_rules.queue_withdrawal(state, withdrawn)
        state["design_phase"] = "cooperative"
        state.pop("cooperative_votes", None)
        state["design_votes"] = {}
        state.pop("approval", None)
        state.pop("local_evidence", None)
        for key in (
            "plan_draft",
            "intake_draft",
            "validation_feedback",
            "validation_failure",
            "validation_stalls",
            "review_repair",
        ):
            state.pop(key, None)

    def reopen_design(self, issue, source):
        """Pause candidate work and preserve the published parent under persistent budgets."""
        validate_design_issue(issue)
        state = self.state
        if state.get("pending"):
            raise Blocked(reason_guidance.PREPARED_PUBLICATION)
        state.setdefault("reopen_history", []).append(
            {"source": source, "issue": issue, "head": state.get("head"), "plan": state["plan_id"]}
        )
        state["design_feedback"] = issue
        self.invalidate_design(issue["explanation"])
        if state["design_reopens"] >= self.config["max_design_reopens"]:
            state.update(stage="escalated", reason="Design reopen budget exhausted.")
            self.save()
            return
        state["design_reopens"] += 1
        if not self.revise_plan("plan", issue["explanation"], invalidate=False):
            return
        file_inventory.retain_approval_parent(state)
        state["design_parent"] = state.get("head")
        if state.get("pr"):
            state["changed_files"] = self.reconciled_changed_paths()
        self.save()

    def record_outcome(self):
        """Persist bounded outcome evidence in the bot-owned GitHub state, without policy edits."""
        state = self.state
        if state["stage"] not in ("merged", "observed_merged", "rejected", "escalated", "blocked", "stopped"):
            return
        outcome = {
            "version": 1,
            "status": state["stage"],
            "issue": state["issue"],
            "title": state["requirements"]["title"][:200],
            "reason": state.get("reason", "")[:1000],
            "plan_summary": state.get("plan", {}).get("summary", "")[:500],
            "goals": sorted(
                {
                    goal
                    for vote in state.get("design_votes", {}).values()
                    for goal in vote["report"]["scope"]["goal_ids"]
                }
            ),
            "files": state.get("plan", {}).get("files", []),
            "head": state.get("head"),
            "plan": state.get("plan_id"),
            "bundle": state.get("bundle_id"),
            "calls": state["calls"],
            "candidate_digest": digest(state.get("candidate")),
            "reviews_digest": digest({p: classification.core(r) for p, r in state.get("reviews", {}).items()}),
            "completion": state.get("completion"),
        }
        if state.get("classification_snapshot"):
            snapshot = state["classification_snapshot"]
            outcome["classification_snapshot"] = {
                **deepcopy(snapshot),
                "fresh": snapshot["binding"] == classification.binding(state, classification.stage_of(state)),
            }
        history = state.setdefault("outcomes", [])
        if history and classification.without_hints(history[-1]) == classification.without_hints(outcome):
            history[-1] = outcome
        elif not history or history[-1] != outcome:
            history.append(outcome)
            del history[:-6]

    def retrieve_outcomes(self):
        """Read a fixed GitHub window and select related, explicitly untrusted lessons."""
        state = self.state
        issues = self.gh.outcome_candidates(self.config["max_outcome_issues"])
        words = set(re.findall(r"[a-z]{4,}", state["requirements"]["title"].lower()))
        lessons = []
        for issue, comments in issues[: self.config["max_outcome_issues"]]:
            if issue == state["issue"]:
                continue
            try:
                prior = Checkpoints(self.gh, self.config, issue, self.report_cache, self.reader).load(comments[:10])
                if prior is None:
                    continue
                outcome = prior.get("outcomes", [])[-1]
                relevance = len(words & set(re.findall(r"[a-z]{4,}", outcome["title"].lower())))
                if not relevance or outcome.get("version") != 1:
                    continue
                lesson = {
                    k: outcome.get(k)
                    for k in (
                        "issue",
                        "status",
                        "title",
                        "reason",
                        "plan_summary",
                        "goals",
                        "files",
                        "head",
                        "plan",
                        "bundle",
                        "completion",
                    )
                }
                if len(json.dumps(lesson).encode()) <= 4000:
                    lessons.append((relevance, issue, lesson))
            except (CheckpointError, ValueError, KeyError, IndexError, TypeError):
                continue
        lessons.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [lesson for _, _, lesson in lessons[: self.config["max_outcome_lessons"]]]

    def refresh_autonomous(self, comments):
        """Resume clarification or invalidate stale inputs without resetting any budget."""
        state = self.state
        if self.enrollment() != state["enrollment"]:
            raise Blocked("autonomous enrollment removed, changed or no longer authorized")
        requirements = self.requirements(comments)
        changed = requirements != state["requirements"]
        if not changed:
            return state["stage"] != "clarify"
        if state.get("head") or state.get("pending"):
            if state.get("pending"):
                raise Blocked(reason_guidance.CHANGED_PUBLICATION)
            return self.resume_with_head(requirements)
        if not self.revise_plan("intake", "Requirements or base changed; QC is reassessing."):
            return False
        state.update(requirements=requirements, issue_digest=self.issue_digest())
        for key in ("plan", "plan_id", "branch", "qc"):
            state.pop(key, None)
        self.save()
        return True

    def review_output_budget(self):
        """Expose the response transport capacity, independent of audit history size."""
        return {"max_json_bytes": MODEL_RESPONSE_LIMIT, "encoding": "UTF-8 JSON"}

    def agent_fields(self, provider, stage):
        """Attach profile identity to reports only for named-agent configurations."""
        return {"agent": resolve(self.config, provider, stage).record()} if "agents" in self.config else {}

    def record_pr_review(self, state, provider, report):
        """Apply a validated PR review to the ledger and queue its publication on the PR.

        A design reopen is kept as the pending review event, not as the head's report.
        """
        sequence, identifiers = review_rules.apply(
            state,
            provider,
            report,
            state["head"],
            protocol.issue_url(self.config["repository"], state["issue"]),
        )
        report.update(self.agent_fields(provider, "review"))
        report.update(
            head=state["head"],
            model=selected_model(self.config, provider, "review") or "CLI default",
            input_revision=state.get("input_revision", 0),
            sequence=sequence,
            finding_ids=identifiers,
        )
        if report["verdict"] == "reopen":
            state["review_event"] = report
        else:
            state["reviews"][provider] = report
        review_rules.queue_review(state, provider, report)

    def validate_design_report(self, report, context):
        """Validate reviewer output before recording any vote or resolving an objection."""
        report = classification.core(report)
        try:
            validate_qc(report, "plan_qc", self.bundle.charter)
        except Blocked as error:
            raise design.ReviewError("review_shape", detail=str(error)) from None
        except (TypeError, KeyError, ValueError):
            raise design.ReviewError("review_shape") from None
        own = context.get("own_objections", [])
        try:
            design.validate_evidence(report, self.state["requirements"], own)
        except design.ReviewError:
            raise
        except Blocked as error:
            raise design.ReviewError("review_evidence", detail=str(error)) from None
        except (TypeError, KeyError, ValueError):
            raise design.ReviewError("review_evidence") from None
        try:
            source_files = self.files(
                self.state.get("head") or self.state["base"],
                list({v["path"] for v in report["sources"]}),
                inspection=True,
            )
        except (Blocked, UnicodeError, ValueError):
            raise design.ReviewError("review_source") from None
        design.validate_evidence(report, self.state["requirements"], own, source_files)

    def repair_review(self, report, error, member):
        """Bound corrections per reviewer and design binding; retain task-wide usage history."""
        state = self.state
        binding = digest([approval_binding(state, self.config), member])
        signature = digest([error.code, report])
        previous = review_state.slot(state).get("review_repair", {})
        if previous.get("binding") != binding:
            previous = {}
        repeated = previous.get("signature") == signature
        attempts = previous.get("attempts", 0)
        review_state.slot(state)["review_repair"] = {
            "binding": binding,
            "signature": signature,
            "attempts": attempts,
            "feedback": {
                "code": error.code,
                "field": error.field,
                "detail": error.detail or design.REPAIR_DETAILS[error.code],
            },
        }
        state.pop("approval", None)
        if repeated or attempts >= self.config["max_review_repairs"]:
            state.update(
                stage="escalated",
                retry_stage="plan_qc",
                reason="Review output repair made no progress or exhausted its budget.",
            )
        else:
            review_state.slot(state)["review_repair"]["attempts"] = attempts + 1
            state["review_repairs"] = state.get("review_repairs", 0) + 1
            state.update(stage="plan_qc", reason="Review output needs correction: " + error.code)
        review_state.slot(state)["review_draft"] = deepcopy(report)
        diagnostic = {
            "binding": binding,
            **review_state.slot(state)["review_repair"]["feedback"],
            "attempt": review_state.slot(state)["review_repair"]["attempts"],
            "limit": self.config["max_review_repairs"],
            "status": "Escalated" if state["stage"] == "escalated" else "Correction requested",
        }
        conversation.queue(
            state,
            None,
            source=review_state.path(state, "review_draft"),
            kind="review_rejection",
            details={
                "provider": member["provider"],
                "round": state["design_generation"] + 1,
                "phase": state.get("design_phase", "cooperative"),
                "diagnostic": diagnostic,
            },
        )
        self.save()

    def quality_context(self, stage, member=None):
        """Build only this reviewer's context; never include peer judgments."""
        state = self.state
        context = self.context()
        phase = state.get("design_phase", "cooperative")
        if stage == "plan_qc":
            context["review_phase"] = phase
            context["review_criteria"] = design.criteria(state["requirements"])
            context["objection_instructions"] = design.OBJECTION_INSTRUCTIONS
            context["plan"] = {k: v for k, v in context["plan"].items() if k != "publication"}
            if phase == "cooperative":
                context["own_objections"] = [v for v in state.get("objections", []) if v["reviewer"] == member["id"]]
            else:
                context["plan"] = {k: v for k, v in context["plan"].items() if k != "responses"}
            context.update(
                files=self.file_manifest(state["base"], state["plan"]["files"]),
                design_binding=approval_binding(state, self.config),
                reviewer_identity=member,
                accumulated_changed_files=state.get("changed_files", []),
                published_parent=state.get("design_parent"),
            )
        else:
            context["editable_files"] = [p for p in self.tree(state["base"]) if path_allowed(p, self.config)]
        if member and state.get("head"):
            context["published_files"] = self.file_manifest(state["head"], state["plan"]["files"])
        provider = member["provider"] if member else assigned(self.config, "triage")
        if member:
            context["review_output_budget"] = self.review_output_budget()
            repair = review_state.slot(state).get("review_repair", {})
            if repair.get("binding") == digest([approval_binding(state, self.config), member]):
                context["review_validation_feedback"] = {
                    **repair["feedback"],
                    "instruction": design_eligibility.CORRECTION_INSTRUCTION,
                }
        if not member and state.get("intake_draft"):
            context["intake_validation_feedback"] = {
                **state["intake_draft"],
                "instruction": (
                    "Correct the intake report against its schema and the rejection reason. "
                    "Use clarify with questions when a human answer is needed; do not remove real questions to proceed. "
                    "Proceed requires empty questions and scope conflicts; advisory concerns may accompany it. "
                    "Preserve genuine blockers with an appropriate non-proceed decision. "
                    "This feedback concerns output validity, not a request to approve the issue."
                ),
            }
        return provider, context

    def quality_control(self, stage):
        """Use parallel transport when supported, keeping the legacy serial adapter."""
        if stage == "plan_qc" and getattr(self.agents, "parallel", False):
            legacy = self.state.get("operation", {})
            if not (
                legacy.get("stage") == "plan_qc"
                and legacy.get("status") != "consumed"
                or self.state.get("review_repair")
            ):
                design_batch.run(self)
                return
        member = None
        if stage == "plan_qc":
            member = next((m for m in design_roster(self.config) if m["id"] not in self.state["design_votes"]), None)
            if member is None:
                self.finish_consensus()
                self.save()
                return
        provider, context = self.quality_context(stage, member)
        report = self.ask(provider, stage, context)
        self.apply_quality_report(stage, provider, member, context, report)

    def _accept_design_report(self, report, context, member, phase):
        """Validate before copying vote state; rejected reports schedule only a correction."""
        try:
            self.validate_design_report(report, context)
        except design.ReviewError as error:
            self.repair_review(report, error, member)
            return
        # Validate fully before recording a vote or resolving an objection.
        state = deepcopy(self.state)
        binding = digest([approval_binding(state, self.config), member])
        for previous in state.get("reports", []):
            if previous.get("diagnostic", {}).get("binding") == binding:
                previous["diagnostic"]["status"] = "Recovered"
        review_state.slot(state).pop("review_repair", None)
        resolved, disposition_ids = design.update_objections(state, report, member, phase)
        return state, resolved, disposition_ids

    def _accept_intake_report(self, report, stage, provider):
        """Validate intake or schedule its correction, leaving accepted state ready for recording."""
        state = self.state
        try:
            validate_qc(report, stage, self.bundle.charter)
        except Blocked as error:
            self.repair_intake(report, str(error), provider)
            return
        except (KeyError, TypeError, ValueError):
            self.repair_intake(report, "invalid intake field types; return the structured intake schema", provider)
            return
        state.pop("intake_draft", None)
        return state

    def _quality_history(self, state, stage, provider, member, phase, report):
        """Build the accepted report's history entry at its existing recording boundary."""
        return {
            "stage": stage,
            "decision": report["decision"],
            "risk": report["risk"],
            "rationale": report["rationale"][:500],
            "report_digest": digest(report),
            "base": state["base"],
            "requirements_digest": digest(state["requirements"]),
            "plan": state.get("plan_id"),
            "provider": provider,
            "model": member["model"] if member else selected_model(self.config, provider, stage),
            **self.agent_fields(provider, stage),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "round": state["design_generation"] + 1,
            "phase": phase if member else "intake",
        }

    def apply_quality_report(self, stage, provider, member, context, report):
        """Validate and apply one result serially using the existing review policy."""
        state = self.state
        phase = context.get("review_phase", "cooperative")
        resolved, disposition_ids = [], []
        if member:
            accepted = self._accept_design_report(report, context, member, phase)
            if accepted is None:
                return
            state, resolved, disposition_ids = accepted
        else:
            state = self._accept_intake_report(report, stage, provider)
            if state is None:
                return
        limit = context["review_output_budget"]["max_json_bytes"] if member else 6000
        report = self.classify(report, provider, stage, limit, member)
        response = review_state.operation(state).get("response")
        if isinstance(response, dict):
            response.pop("classification", None)
        state.setdefault("qc", {})[stage] = report
        state.setdefault("qc_history", []).append(self._quality_history(state, stage, provider, member, phase, report))
        identity = member or {
            "id": digest([provider, "intake"]),
            "provider": provider,
            "model": selected_model(self.config, provider, stage),
        }
        conversation.queue(
            state,
            None,
            source=("qc", stage),
            kind="qc",
            details={
                "provider": provider,
                "model": identity["model"],
                **self.agent_fields(provider, stage),
                "plan": state.get("plan_id", "intake"),
                "round": state["design_generation"] + 1,
                "phase": phase if member else "intake",
                "reviewer": identity["id"],
                "resolved": resolved,
                "disposition_ids": disposition_ids,
            },
        )

        state["reason"] = report["rationale"]
        if member:
            role = sessions.role_key(self.config, provider, stage, member["model"])
            session = {**state.get("sessions", {}).get(role, {}), "role": role}
            state["design_votes"][member["id"]] = {
                "identity": member,
                "binding": approval_binding(state, self.config),
                "report": report,
                "first_pass": design_eligibility.eligible(state, context, session),
                "objection_ids": [v["id"] for v in context.get("own_objections", [])],
            }
            self.classification_room(state, CHECKPOINT_LIMIT - 6000)
        self.state = state
        self.refresh_classification()
        self.finish_quality_control(stage, report)
        if stage == "intake" and state["stage"] == "plan" and report["concerns"]:
            conversation.queue_intake_concerns(state, report["concerns"])
        self.record_outcome()
        self.transition(state)
        self.classification_room(state, CHECKPOINT_LIMIT - 1000)
        self.save()

    def finish_quality_control(self, stage, report):
        """Apply a validated vote's transition before the final checkpoint capacity check."""
        state, decision = self.state, report["decision"]
        if report["risk"] == "high" or decision == "escalate":
            state.update(stage="escalated", reason=ESCALATION_REASON + " " + report["rationale"])
        elif decision in ("reject", "clarify"):
            state["stage"] = "rejected" if decision == "reject" else "clarify"
        elif stage == "intake":
            state["stage"] = "plan"
        elif len(state["design_votes"]) == len(design_roster(self.config)):
            objections = [
                vote["report"] for vote in state["design_votes"].values() if vote["report"]["decision"] != "approve"
            ]
            if objections:
                state["design_feedback"] = objections
                self.revise_plan("plan", "Design reviewers require a revision.", save_exhausted=False)
            else:
                self.finish_consensus()
        if state["stage"] in JUDGED_STAGES:
            state["retry_stage"] = self.waiting_revision() or "intake"
            state.pop("approval", None)

    def finish_consensus(self):
        """Authorize implementation only after a complete current unanimous round."""
        state = self.state
        if not consensus_valid(state, self.config, require_final=False):
            raise Blocked("unanimous design consensus missing or stale")
        if not consensus_valid(state, self.config):
            state.update(design_phase="fresh", design_votes={})
            return
        if state.get("mode") == "manual":
            state["stage"] = "planned"
        else:
            state.update(
                stage="implement",
                approval={
                    "kind": "consensus",
                    "plan": state["plan_id"],
                    "binding": approval_binding(state, self.config),
                },
            )

    def suspend(self, stage, reason=None, publish_events=True, outboxes=True):
        """Pause work without replacing an earlier resume target on repeated failures."""
        if self.state["stage"] not in SUSPENDED_STAGES:
            self.state["retry_stage"] = self.state["stage"]
        self.state["stage"] = stage
        if reason is not None:
            self.state["reason"] = reason
        self.save(publish_events=publish_events, outboxes=outboxes)

    def resume_with_head(self, requirements):
        """Continue a design whose PR exists under changed requirements; whether planning resumes.

        An answered clarification re-plans the open design; a changed issue reopens it.
        """
        state = self.state
        if state["stage"] == "clarify" and all(
            requirements[key] == state["requirements"][key] for key in ("title", "body")
        ):
            self.invalidate_design("Clarification answered for the open design.")
            state.update(stage="plan", requirements=requirements)
            self.save()
            return True
        self.reopen_design(
            {
                "kind": "requirements",
                "explanation": "Authorized issue requirements changed.",
                "evidence": [digest(requirements)],
            },
            "requirements",
        )
        state.update(requirements=requirements, issue_digest=self.issue_digest())
        self.save()
        return state["stage"] == "plan"

    def retry_reviewed(self, comment, comments):
        """A writer may request a bounded repair with fresh feedback; CI events alone never do."""
        return self._command_handler().retry_reviewed(comment, comments)

    def require_current_bundle(self):
        """Only current instructions may produce or consume a model result."""
        if not implementation_bundle.current(self.state, self.bundle) or self.state.get("charter_id") != digest(
            self.bundle.charter
        ):
            if (
                self.state.get("stage") == "implement"
                and self.state.get("charter_id") == digest(self.bundle.charter)
                and implementation_bundle.original_skill(self.state, self.bundle) is not None
            ):
                raise Blocked(implementation_bundle.RETRY_REASON)
            raise Blocked(BUNDLE_REASON)

    def retry_implementation_bundle(self, comment, comments):
        """Adopt only proven implementer-skill changes on an authorized implementation retry."""
        return self._command_handler().retry_implementation_bundle(comment, comments)
        # The caller saves the receipt, session rotation and consumed command together.

    def invalidate_task(self, comment, comments):
        """Explicitly adopt current governance and restart assessment without resetting spent budgets."""
        return self._command_handler().invalidate_task(comment, comments)

    def _command_handler(self):
        """Build a handler using the controller's current state and binding function."""
        return commands.CommandHandler(self, config_digest)

    def commands(self, comments):
        """Apply exact writer commands; return True when a command starts a new assessment step."""
        handler = self._command_handler()
        for comment in comments:
            if comment["id"] <= (self.state or {}).get("command_id", 0):
                continue
            command = commands.parse(comment["body"])
            if command is None:
                continue
            if not self.permitted(comment["user"]["login"]):
                continue
            action = handler.dispatch(command, comment, comments)
            if action is commands.LoopAction.CONTINUE:
                continue
            if action is commands.LoopAction.RETURN_TRUE:
                return True

    def rejected_output(self, value, reason, key):
        """Bound a rejected JSON excerpt against the remaining durable checkpoint capacity."""
        attempt = self.state.get(key, {}).get("attempt", 0) + 1
        output = json.dumps(value, indent=2, ensure_ascii=True)
        saved = deepcopy(self.state)
        saved.pop(key, None)
        saved.get("operation", {}).pop("response", None)
        # JSON string escaping can double the excerpt; reserve publication and dashboard metadata.
        room = max(0, (CHECKPOINT_LIMIT - len(self.checkpoints.audit_body(saved).encode()) - 4000) // 2)
        limit = min(20000, room)
        return {"output": output[:limit], "truncated": len(output) > limit, "reason": reason, "attempt": attempt}

    def repair_intake(self, report, reason, provider):
        """Archive invalid intake output before scheduling at most two paid corrections."""
        state = self.state
        state["intake_draft"] = self.rejected_output(report, reason, "intake_draft")
        state.pop("approval", None)
        state.get("qc", {}).pop("intake", None)
        state.update(stage="intake", reason="Intake output needs correction: " + reason)
        if state["intake_draft"]["attempt"] > INTAKE_OUTPUT_REPAIRS:
            state.update(
                stage="escalated",
                retry_stage="intake",
                reason="Intake output still needs correction after two retries: " + reason,
            )
        sequence = state.get("conversation", {}).get("sequence", 0) + 1
        conversation.queue(
            state,
            None,
            source=("intake_draft",),
            kind="intake_rejection",
            details={
                "provider": provider,
                "round": f"rejected output {sequence}",
                "agent": resolve(self.config, provider, "intake").record(),
            },
        )
        self.save()

    def repair_plan(self, plan, reason):
        """Publish rejected output and request at most two corrections across restarts."""
        self.state["plan_draft"] = self.rejected_output(plan, reason, "plan_draft")
        attempt = self.state["plan_draft"]["attempt"]
        self.state.pop("approval", None)
        self.state.update(stage="plan", reason="Planner output needs correction: " + reason)
        if attempt > PLAN_OUTPUT_REPAIRS:
            self.state.update(
                stage="escalated",
                retry_stage="plan",
                reason="Planner output still needs correction after two retries: " + reason,
            )
        self.save()

    def plan(self):
        """Triage and attach a structured plan without making code changes."""
        context = self.context()
        context["design_feedback"] = self.state.get("design_feedback")
        context["plan_output_contract"] = plan_output_contract(self.config)
        if self.state.get("plan_draft"):
            context["plan_validation_feedback"] = self.state["plan_draft"]
        context["open_objections"] = [v for v in self.state.get("objections", []) if not v["resolved"]]
        context["editable_files"] = [p for p in self.tree(self.state["base"]) if path_allowed(p, self.config)]
        if self.state.get("head"):
            paths = self.state["changed_files"]
            context["accumulated_changes"] = changes(
                self.files(self.state["base"], paths, inspection=True),
                self.files(self.state["head"], paths, inspection=True),
                self.config["paths"],
            )
        plan = self.ask(assigned(self.config, "planner"), "plan", context)
        try:
            validate_plan(plan, self.config)
            expected = {v["id"] for v in context["open_objections"]}
            if {v["id"] for v in plan["responses"]} != expected or len(plan["responses"]) != len(expected):
                raise Blocked("planner must respond to every open objection exactly once")
            if self.state.get("head") and not plan["questions"]:
                if not all(checks.in_scope(path, plan) for path in self.state.get("changed_files", [])):
                    raise Blocked("revised scope does not reconcile the accumulated PR diff")
        except Blocked as error:
            self.repair_plan(plan, str(error))
            return
        except (KeyError, TypeError, ValueError):
            self.repair_plan(plan, "invalid plan field types; return the structured plan schema")
            return
        self.state["plan"] = plan
        self.state.pop("reason", None)
        self.state.pop("design_feedback", None)
        self.state.pop("review_repair", None)
        for key in ("plan_draft", "validation_feedback", "validation_failure", "validation_stalls"):
            self.state.pop(key, None)
        self.state["plan_id"] = digest(
            {"plan": plan, "base": self.state["base"], "requirements": self.state["requirements"]}
        )
        if not self.state.get("head"):
            self.state["branch"] = task_branch(self.config["repository"], self.issue["number"])
        self.state["design_votes"] = {}
        self.state["design_phase"] = "cooperative"
        self.state.pop("approved_inventory_parent", None)
        self.state["approved_accumulated_files"] = self.state.get("changed_files", [])
        self.state["design_parent"] = self.state.get("head")
        self.state["stage"] = "clarify" if plan["questions"] else "plan_qc"
        self.save()

    def design_authorization(self):
        """Attest the supplied implementation plan only from current validated controller consensus."""
        state = self.state
        if not approval_valid(state, self.config):
            raise Blocked("implementation authorization requires complete current design approval")
        roster = design_roster(self.config)
        return {
            "verified_by": "controller",
            "approval_kind": state["approval"]["kind"],
            "binding": approval_binding(state, self.config),
            "plan_id": state["plan_id"],
            "plan_digest": digest(state["plan"]),
            "base": state["base"],
            "bundle_id": state["bundle_id"],
            "charter_id": state["charter_id"],
            **(
                {"implementation_bundle": deepcopy(state["implementation_bundle"])}
                if state.get("implementation_bundle")
                else {}
            ),
            "requirements_digest": digest(state["requirements"]),
            "generation": state["design_generation"],
            "required_identities": roster,
            "successful_votes": [
                {"identity": member, "binding": state["design_votes"][member["id"]]["binding"], "decision": "approve"}
                for member in roster
            ],
        }

    def develop(self, parent):
        """Ask the implementer for a native-workspace change, then validate its bytes centrally."""
        state = self.state
        self.design_authorization()
        pr_template.pin(state, self.config, self.files, self.save)
        round_key = f"{state['design_generation']}:{state['round']}"
        attempts = state.setdefault("implementation_attempts", {})
        feedbacks = state.setdefault("implementation_feedback", {})
        repair_feedback = feedbacks.get(round_key)
        for attempt in range(attempts.get(round_key, 0), self.config["max_fix_rounds"] + 1):
            context = self.context()
            context.update(
                prior_reviews=state["reviews"],
                review_findings=review_rules.open_findings(state),
                implementation_feedback=repair_feedback,
                design_authorization=self.design_authorization(),
                conventional_commits=self.config.get("conventional_commits", False),
            )
            report = self.ask(assigned(self.config, "implementer"), "develop", context)
            extraction_error = report.get("workspace_error")
            report = {key: value for key, value in report.items() if key != "workspace_error"}
            try:
                candidates.development(report)
            except Blocked as error:
                repair_feedback = str(error) + ". Return a corrected implementation response; nothing was applied."
                feedbacks[round_key], attempts[round_key] = repair_feedback, attempt + 1
                self.save()
                continue
            if report["design_issue"] is not None:
                self.reopen_design(report["design_issue"], "implementation")
                return None
            proposal = {"summary": report["summary"], "edits": report["edits"]}
            names = (
                [e.get("path") for e in report["edits"] if isinstance(e, dict)]
                if isinstance(report["edits"], list)
                else []
            )
            # Validate paths before reading them, and refuse the entire proposal before creating Git objects.
            readable = sorted({p for p in names if path_allowed(p, self.config) and checks.in_scope(p, state["plan"])})
            try:
                if extraction_error:
                    raise Blocked(extraction_error)
                originals = self.files(parent, readable, inspection=True)
                edits = validate_edits(proposal, state["plan"], originals, self.config)
                all_paths = sorted(set(readable) | set(state.get("changed_files", [])))
                before = self.files(state["base"], all_paths, inspection=True)
                after = {**self.files(parent, all_paths, inspection=True), **{e["path"]: e["content"] for e in edits}}
                changed = candidates.validate(before, after, state["plan"], self.config)
                before, after = ({p: values[p] for p in changed} for values in (before, after))
                over = self.oversized(None, before, after, description=report)
                if over:
                    raise Blocked(over)
            except Blocked as error:
                repair_feedback = str(error) + ". Reduce the change or request a design reopen. Nothing was applied."
                feedbacks[round_key], attempts[round_key] = repair_feedback, attempt + 1
                self.save()
                continue
            return proposal, {
                "changed_files": changed,
                "validation": report["validation"],
                **(
                    {"description": report["description"], "commit": report["commit"]}
                    if "description" in report
                    else {}
                ),
                "edits_digest": digest(edits),
            }
        raise Blocked("implementation exceeds the approved scope or capacity; repair allowance exhausted")

    def oversized(self, local_context, before, after, dependencies=None, description=None):
        """Why a review of this candidate would not fit, or None.

        Measures the local review and each reviewer's prospective PR review as they would be sent,
        with the same serialization and limit as `ask()`. Files are never omitted to make one fit.
        """
        contexts = [("local review", local_context)] if local_context is not None else []
        contexts += [
            (provider + " PR review", self.pr_review_context(provider, before, after, "0" * 40, dependencies))
            for provider in assigned(self.config, "pr_reviewers")
        ]
        if description and description.get("description"):
            parts = pr_description.sections(
                {**description, "head": "0" * 40},
                "0" * 40,
                self.state["plan"].get("publication", {}).get("review_focus", []),
            )
            for _, context in contexts:
                context["published_pr_body"] = (
                    pr_template.render(parts, self.state.get("pr_template"), self.state["base"]) + " " * 1024
                )
        limit = self.config["max_context_bytes"] - DISPATCH_ALLOWANCE
        over = [
            f"{name} ({size} of {limit} bytes)"
            for name, context in contexts
            if (size := transport_size(context)) > limit
        ]
        if not over:
            return None
        return (
            "The change is too large to review: " + ", ".join(over) + ". Reduce it, or return a design_issue "
            "to split the work."
        )

    def pr_review_context(self, provider, before, after, head, dependencies=None):
        """One reviewer's PR review context for a candidate's changed files."""
        context = self.context(sorted(after))
        context.update(changes=changes(before, after), head=head)
        attention = change_attention.paths([change["path"] for change in context["changes"]], self.config)
        if attention:
            context["workflow_and_policy_changes"] = attention
            context["workflow_and_policy_review"] = (
                "Review changes to CI, workflows and policy documents explicitly, including whether the required "
                "checks still validate the contribution. These paths are editable under the target policy."
            )
        context["published_pr_body"] = (
            (self.pulls.repo(f"pulls/{self.state['pr']}").get("body") or "")
            if self.state.get("pr") and head != "0" * 40
            else ""
        )
        context["pr_description_criterion"] = (
            "The published PR description accurately describes the whole base-to-head change and its validation."
        )
        context["own_findings"] = review_rules.context(self.state, provider)
        context["review_output_budget"] = self.review_output_budget()
        context["prior_outcomes_untrusted"] = classification.without_hints(context["prior_outcomes_untrusted"])
        context = feedback.prepare(context)
        # Diff previews are optional; file identities, findings and review criteria are not.
        limit = self.config["max_context_bytes"] - DISPATCH_ALLOWANCE
        preview = MAX_DIFF
        while transport_size(context) > limit and preview:
            preview //= 2
            if preview < 256:
                preview = 0
            for change in context["changes"]:
                if len(change["diff"].encode()) > preview:
                    change["diff"] = diff_preview(change["diff"], preview)
                    change["truncated"] = True
        return context

    def _publication(self):
        """Bind publication to current state; checkpoint callbacks stay explicit at write boundaries."""
        return Publication(
            self.code,
            self.pulls,
            self.config,
            self.state,
            self.checkpoints.plan if self.checkpoints else None,
            self.reader,
        )

    def reserve_development_branch(self):
        """Reserve a fork branch and publish its issue link with recoverable intent."""
        return self._publication().reserve_development_branch(self.save)

    def remember_legacy_pr_body(self):
        """Adopt only an exact legacy body before its original plan or candidate is replaced."""
        state = self.state
        if state.get("pr") and state.get("plan") and not state.get("pr_description"):
            expected = self._publication().pr_body(legacy=True)
            if self.pulls.repo(f"pulls/{state['pr']}").get("body") == expected:
                state["pr_description"] = {"last": digest(expected)}

    def implement(self):
        """Reserve the fork branch; publish candidate objects only after local acceptance."""
        self.design_authorization()
        self.remember_legacy_pr_body()
        self.reserve_development_branch()
        state = self.state
        parent = state.get("head", state["base"])
        developed = self.develop(parent)
        if developed is None:
            return
        result, evidence = developed
        edits = validate_edits(
            result,
            state["plan"],
            self.files(parent, [e["path"] for e in result["edits"]], inspection=True),
            self.config,
        )
        commit = self._publication().build_commit(parent, edits, self.tree, INLINE_BLOB_LIMIT, evidence.get("commit"))
        state["pending"] = {
            "head": commit["sha"],
            "parent": parent,
            "summary": result["summary"],
            "initial": "head" not in state,
        }
        state["candidate"] = {
            **evidence,
            "summary": result["summary"],
            "head": commit["sha"],
            "parent": parent,
            "base": state["base"],
            "plan": state["plan_id"],
        }
        file_inventory.compact_candidate(state["candidate"])
        state["stage"] = "publish"
        conversation.queue(
            state,
            None,
            source=("candidate",),
            kind="implementation",
            details={
                "provider": assigned(self.config, "implementer"),
                "round": state["round"],
                **self.agent_fields(assigned(self.config, "implementer"), "develop"),
            },
        )
        self.save()

    def publish(self):
        """Reconcile interrupted publication without overwriting a foreign branch tip."""
        return self._publication().publish(self.save, self.checkpoints.preflight)

    def pr_title(self):
        """The plan's headline, or its summary's first sentence, as plain text within 72 characters."""
        return self._publication().pr_title()

    def pr_body(self):
        """The plan's summary, its artifact, any dependency change, and the issue it closes."""
        return self._publication().pr_body()

    def pr_url(self, number):
        """The task's pull request on GitHub."""
        return self._publication().pr_url(number)

    def review(self):
        """Run one fresh provider review of the same immutable change."""
        state = self.state
        provider = next((p for p in assigned(self.config, "pr_reviewers") if p not in state["reviews"]), None)
        if provider:
            changed = state["changed_files"]
            context = self.pr_review_context(
                provider,
                self.files(state["base"], changed, inspection=True),
                self.files(state["head"], changed, inspection=True),
                state["head"],
                None,
            )
            report = self.ask(provider, "review", context)
            owned = review_rules.owned(state, provider)
            validate_pr_review(
                report,
                [f["id"] for f in owned if f["status"] == "open"],
                [f["id"] for f in owned if f["status"] == "withdrawn"],
            )
            report = self.classify(report, provider, "review", MODEL_RESPONSE_LIMIT)
            self.record_pr_review(state, provider, report)
            source = ("review_event",) if report["verdict"] == "reopen" else ("reviews", provider)
            conversation.queue(
                state,
                None,
                source=source,
                kind="review",
                details={
                    "head": state["head"],
                    "round": state["round"] + 1,
                    "pr": state["pr"],
                    "provider": provider,
                    "model": selected_model(self.config, provider, "review") or "CLI default",
                    "pr_url": self.pr_url(state["pr"]),
                    **self.agent_fields(provider, "review"),
                },
            )
            self.refresh_classification()
            if report["verdict"] == "reopen":
                self.reopen_design(report["design_issue"], "pr_review:" + provider)
                return
            self.save()
        if len(state["reviews"]) == len(assigned(self.config, "pr_reviewers")):
            if all(r["verdict"] == "approve" for r in state["reviews"].values()):
                # A reason at the gates is always the gates' own; an earlier stage's is stale.
                state.pop("reason", None)
                state["stage"] = "gates"
            elif state["round"] < self.config["max_fix_rounds"]:
                state["round"] += 1
                state["stage"] = "implement"
            else:
                raise Blocked("independent reviews still request changes; fix budget exhausted")
            self.save()

    def _observe_ci(self, pr, files, threads, reasons):
        """Collect CI evidence and recheck the PR, appending blockers in observation order."""
        state = self.state
        refs = readiness.ci_refs(pr)
        if len(refs) > 1:
            commit = self.pulls.repo("git/commits/" + refs[1])
            if [parent["sha"] for parent in commit.get("parents", [])] != [pr["base"]["sha"], state["head"]]:
                reasons.append("GitHub test-merge commit is not current")
        observations = [
            (
                sha,
                self.pulls.pages(f"commits/{sha}/check-runs?filter=all", "check_runs"),
                self.pulls.pages(f"commits/{sha}/statuses"),
                self.pulls.pages(f"commits/{sha}/check-suites", "check_suites"),
                self.pulls.pages(f"actions/runs?head_sha={sha}", "workflow_runs"),
            )
            for sha in refs
        ]
        reasons += readiness.ci_reasons(observations)
        latest = self.pulls.repo(f"pulls/{state['pr']}")
        if readiness.ci_refs(latest) != readiness.ci_refs(pr) or latest["base"] != pr["base"]:
            reasons.append("PR changed while observing CI")
        reasons += readiness.review_reasons(state, latest, files, threads, self.config)
        return latest

    def gates(self):
        """Observe current reviews and CI; mark ready once, never merge or rerun CI."""
        state = self.state
        pr = self.pulls.repo(f"pulls/{state['pr']}")
        files = self.reader.pr_paths(state, state["pr"])
        threads = self.pulls.threads(state["pr"])
        reasons = readiness.review_reasons(state, pr, files, threads, self.config)
        latest = self._observe_ci(pr, files, threads, reasons)
        pr = latest
        seen_before = state.get("ready_seen", False)
        if not pr["draft"]:
            state["ready_seen"] = True
        # Never reverse a human's later conversion back to draft, even on a new head.
        if pr["draft"] and state.get("ready_seen"):
            reasons.append(reason_guidance.RETURNED_TO_DRAFT)
        if not reasons and pr["draft"]:
            # The response can be lost; the next tick reconstructs completion from isDraft.
            state["ready_intent"] = state["head"]
            self.save()
            pr = self.pulls.repo(f"pulls/{state['pr']}")
            if (
                readiness.ci_refs(pr) != readiness.ci_refs(latest)
                or pr["base"] != latest["base"]
                or pr["state"] != "open"
            ):
                return False
            if pr["draft"]:
                self.pulls.mark_ready(pr["node_id"])
            pr = self.pulls.repo(f"pulls/{state['pr']}")
            if pr["draft"] or pr["head"]["sha"] != state["head"]:
                raise Blocked("PR readiness response did not confirm the reviewed head")
            state["ready_seen"] = True
        ready = not reasons and not pr["draft"]
        reason = (
            "; ".join(dict.fromkeys(reasons))
            if reasons
            else "CI passed and all Concurro reviewers approved. Ready for human review."
        )
        changed = (
            state.get("handoff_ready") != ready
            or state.get("reason") != reason
            or state.get("ready_seen", False) != seen_before
        )
        state["handoff_ready"], state["reason"] = ready, reason
        if ready and state.get("handoff") != state["head"]:
            state["handoff"] = state["head"]
            conversation.queue(
                state,
                "Ready for review",
                link(f"PR #{state['pr']}", self.pr_url(state["pr"]))
                + " has passing CI and current approvals from all Concurro reviewers. Human review and merge remain yours.",
            )
            changed = True
        if changed:
            self.save()
        return False

    def observe_merge(self, pr):
        """Record external completion without claiming that controller gates authorized it."""
        state = self.state
        if pr.get("head", {}).get("sha") != state.get("head") or not state.get("head"):
            raise Blocked("merged PR head differs from the exact published controller head")
        if not pr.get("merge_commit_sha"):
            raise Blocked("merged PR lacks completion evidence")
        state["completion"] = {
            "kind": "observed_external",
            "head": state["head"],
            "merge_sha": pr["merge_commit_sha"],
            "pr": state["pr"],
            "gates_verified": False,
            "observed_stage": state["stage"],
            "merged_by": (pr.get("merged_by") or {}).get("login"),
        }
        state.update(
            stage="observed_merged",
            merge_sha=pr["merge_commit_sha"],
            reason="Observed an external merge of the published head.",
        )
        self.save(outboxes=False)

    def advance(self, number):
        """Advance one stage and classify what the task waits for next."""
        self.foreign = False
        return self.outcome(self.step(number))

    def outcome(self, progressed):
        """Derive the scheduling outcome from durable stage, so every return path agrees."""
        if self.foreign or self.issue and "pull_request" in self.issue:
            return Outcome("terminal")
        if not self.state:
            return Outcome("await-human")
        stage = self.state["stage"]
        if stage in TERMINAL_STAGES:
            return Outcome("terminal", progressed)
        if stage in ("blocked", "stopped", "rejected", "escalated"):
            return Outcome("blocked", progressed)
        if stage in ("planned", "clarify"):
            return Outcome("await-human", progressed)
        if progressed:
            return Outcome("continue", True)
        return Outcome("wait-until", retry_after=WAIT_SECONDS)

    def _reconcile_external(self):
        """Observe PR completion before issue closure; None permits normal processing."""
        if self.state and self.state.get("pr") and self.state["stage"] not in MERGED_STAGES:
            pr = self.pulls.repo(f"pulls/{self.state['pr']}")
            if pr.get("merged"):
                self.observe_merge(pr)
                return True
            if pr["state"] == "closed":
                if self.state["stage"] != "closed":
                    self.state.update(stage="closed", reason="PR closed without merging.")
                    self.save(outboxes=False)
                return False
        if self.issue["state"] != "open":
            if self.state and self.state["stage"] not in ("merged", "observed_merged", "stopped"):
                self.state.update(stage="stopped", reason="Issue closed without a confirmed exact-head merge.")
                self.save(outboxes=False)
                return True
            return False
        return None

    def _migrate_legacy_block(self):
        """Resume old observer-only bundle blocks without changing model-work authority."""
        if (
            self.state
            and self.state["stage"] == "blocked"
            and self.state.get("pr")
            and self.state.get("retry_stage") in ("gates", "merging")
            and self.state.get("reason") in (LEGACY_BUNDLE_REASON, BUNDLE_REASON)
        ):
            # Old releases blocked observers before they could read the PR's completion state.
            self.state["stage"] = "gates"
            self.state.pop("retry_stage", None)
            self.state.pop("reason", None)

    def _needs_flush(self):
        """Identify retained outboxes or display state that must be saved before commands."""
        return (
            self.stage_sync_warned
            or self.stage_fallback
            or self.checkpoints.dirty
            or self.state
            and (
                self.state.get("conversation", {}).get("pending")
                or self.state.get("plan_event_intent")
                or self.state.get("pr_outbox")
                or self.state.get("blocked_notices")
                and self.state["stage"] in actions.RESUMED_STAGES
            )
        )

    def _validate_authority(self, comments):
        """Check current work authority; the PR guard deliberately checks only open/head."""
        state = self.state
        if state["stage"] == "merging":
            state["stage"] = "gates"
        if state["config_digest"] != config_digest(self.config):
            raise Blocked("controller configuration changed; re-plan required")
        if state.get("mode") == "autonomous":
            if not self.refresh_autonomous(comments):
                return False
        elif state["stage"] == "clarify":
            return False
        if state["issue_digest"] != self.issue_digest():
            raise Blocked("issue requirements changed; re-plan required")
        if state["stage"] in ("intake", "plan", "plan_qc", "implement", "review"):
            self.require_current_bundle()
        if state.get("mode") == "manual" and state.get("approval"):
            self.check_human_approval(comments)
        if state["stage"] in ("implement", "publish", "review", "gates") and not approval_valid(state, self.config):
            raise Blocked("approved plan missing or stale")
        if state.get("head") and state["stage"] != "publish":
            pr = self.pulls.repo(f"pulls/{state['pr']}")
            if pr["state"] != "open" or pr["head"]["sha"] != state["head"]:
                raise Blocked(reason_guidance.CLOSED_OR_CHANGED_HEAD)
        return True

    def step(self, number):
        """Process writer commands, validate current authority, and advance one stage."""
        self.issue = self.gh.repo(f"issues/{number}")
        if "pull_request" in self.issue:
            return False
        comments = self.gh.pages(f"issues/{number}/comments")
        self.load(comments)
        if self.state and self.state.get("controller", ACTIONS) != self.controller_id:
            # Another controller owns this task: no label, comment, artifact or health write.
            # Dropping the state also keeps the caller's health and failure paths silent.
            self.state = None
            self.foreign = True
            return False
        reconciled = self._reconcile_external()
        if reconciled is not None:
            return reconciled
        if self.state and self.state["stage"] in TERMINAL_STAGES:
            return False
        if self.state:
            actions.discover_blocked(self.state, comments, self.identities.author("conversation"))
        if self.state and (
            digest(self.state.get("charter")) != self.state.get("charter_id")
            or self.state.get("execution") != self.config["paths"]
        ):
            raise Blocked(
                "Recorded charter or path policy is inconsistent; start a new task rather than adopting unverified state."
            )
        self._migrate_legacy_block()
        self.sync_stage()
        if self._needs_flush():
            self.save()
        if not self.state and self.config["autonomous"]:
            enrollment = self.enrollment()
            if enrollment:
                self.initialize(comments, "autonomous", enrollment=enrollment)
        if self.state and actions.answers(self.state, self.config, comments, self.issue_digest(), self.answerer):
            self.save()
        if self.commands(comments):
            return True
        if not self.state or self.state["stage"] in (
            "planned",
            "blocked",
            "stopped",
            "merged",
            "observed_merged",
            "rejected",
            "escalated",
        ):
            return False
        state = self.state
        if not self._validate_authority(comments):
            return False
        method_name = self.STAGE_METHODS.get(state["stage"])
        if method_name is None:
            raise Blocked("unknown state")
        method = getattr(self, method_name)
        result = method(state["stage"]) if method_name == "quality_control" else method()
        return result is not False

    def check_human_approval(self, comments):
        """Retain revocable writer approval for explicitly configured manual mode."""
        return self._command_handler().check_human_approval(comments)
