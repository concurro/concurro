"""Durable issue-to-PR state machine. Only this trusted process writes to GitHub."""

import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from agents import Agents
import actions
import conversation
import markup
from markup import block, code, link, text
import sessions
import checks
import design
import reviews as findings
from copy import deepcopy
from repository import MAX_FILE
from bundle import Bundle
from checkpoints import CheckpointError, Checkpoints, MARKER, STATE_PREFIX, LIMIT as CHECKPOINT_LIMIT
from checkpoint_health import sync_checkpoint_health
from github import GitHub, NotFound
from identities import Identities
from local import LocalWorkspace, resource_exhausted
from scheduler import QueueScan
from policy import (PR_REVIEW_LIMIT, Blocked, OutsideScope, SessionFailure, approval_binding, approval_valid, approved_checks,
                    approved_profile_ids, digest, local_evidence_valid, merge_reasons,
                    consensus_valid, design_roster, path_allowed, protection_errors, selected_model,
                    instruction_matches, validate_design_issue, validate_edits, validate_plan, validate_pr_review,
                    validate_qc, validate_review)

# GitHub rejects a comment over 65,536 characters.
SUMMARY_LIMIT = 60000
PR_TITLE_LIMIT = 72
# Context bytes `ask()` adds at dispatch (the repository commit and the operation contract).
DISPATCH_ALLOWANCE = 2000
# The status comment's steps, and the stage each running stage belongs to.
STEPS = ("Intake", "Plan", "Design review", "Implement", "Code review", "Merge")
STAGE_STEP = {"intake": 0, "plan": 1, "plan_qc": 2, "planned": 2, "implement": 3, "publish": 3, "review": 4,
              "gates": 5, "merging": 5}
ESCALATION_REASON = "High-risk or consequential work requires human intervention."
PROVIDERS = {"claude", "codex", "copilot"}
PATH_POLICY = {"editable_extensions", "protected_names", "protected_name_prefixes", "protected_prefixes",
               "protected_files", "autonomous_merge_prefixes"}
REPOSITORY = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
COMMIT = re.compile(r"[0-9a-f]{40}")


def strings(value, limit=32):
    """Return whether a value is a bounded list of distinct non-empty strings."""
    return (isinstance(value, list) and len(value) <= limit and len(set(value)) == len(value)
            and all(isinstance(v, str) and v.strip() and len(v) <= 240 for v in value))


def pr_review_cost(report):
    """Checkpoint bytes a PR review adds: the report, its finding text again in the ledger, and for a
    reopen the report's copy in the PR outbox and the copies of its design issue kept in its history,
    feedback and revision notice."""
    size = len(json.dumps(report).encode())
    cost = size + sum(len(json.dumps(f["detail"]).encode()) for f in report["findings"])
    if report["verdict"] == "reopen":
        cost += size + 3 * len(json.dumps(report["design_issue"]).encode())
    return cost


def validate_config(config):
    """Reject unsafe or internally inconsistent operator configuration."""
    if (not isinstance(config["repository"], str) or not REPOSITORY.fullmatch(config["repository"])
            or not isinstance(config["base"], str) or not BRANCH.fullmatch(config["base"])
            or ".." in config["base"] or config["base"].startswith(("/", "-"))):
        raise Blocked("invalid repository or base branch")
    if not isinstance(config["bot_login"], str) or not config["bot_login"].endswith("[bot]"):
        raise Blocked("bot_login must name the GitHub App's [bot] account")
    reviewers = config["reviewers"]
    if not {"claude", "codex"}.issubset(reviewers) or len(set(reviewers)) != len(reviewers):
        raise Blocked("independent Claude and Codex reviews are mandatory")
    if not set(reviewers).issubset(PROVIDERS):
        raise Blocked("unknown reviewer")
    if config["planner"] not in PROVIDERS or config["implementer"] not in PROVIDERS:
        raise Blocked("unknown planner or implementer")
    if type(config["autonomous"]) is not bool or config["qc"] not in PROVIDERS or not isinstance(config["qc_model"], str):
        raise Blocked("invalid autonomy or QC configuration")
    if config["autonomous"] and config["qc"] == config["planner"]:
        raise Blocked("QC must use a different provider from the planner")
    if type(config["max_plan_revisions"]) is not int or not 0 <= config["max_plan_revisions"] <= 3:
        raise Blocked("invalid plan revision budget")
    if type(config.get('max_validation_repairs')) is not int or not 0 <= config['max_validation_repairs'] <= 6:
        raise Blocked('invalid validation repair budget')
    if type(config.get('max_review_repairs')) is not int or not 0 <= config['max_review_repairs'] <= 8:
        raise Blocked('invalid review repair budget')
    if set(config["models"]) != PROVIDERS or not all(isinstance(v, str) for v in config["models"].values()):
        raise Blocked("model map must name each provider")
    if not strings(config["required_checks"], 10) or not config["required_checks"]:
        raise Blocked("required checks must name the target's CI checks")
    documents = config["policy_documents"]
    if not strings(documents, 10) or not all(
            not PurePosixPath(p).is_absolute() and ".." not in p.split("/") for p in documents):
        raise Blocked("policy documents must be relative repository paths")
    selection = config["execution_profiles"]
    if not strings(selection, 4) or not selection:
        raise Blocked("execution_profiles must list one to four profiles")
    paths = config["paths"]
    if (not isinstance(paths, dict) or set(paths) != PATH_POLICY
            or not all(strings(v) for v in paths.values()) or not paths["editable_extensions"]
            or not all(e.startswith(".") for e in paths["editable_extensions"])
            or not all(p.endswith("/") for p in paths["protected_prefixes"] + paths["autonomous_merge_prefixes"])):
        raise Blocked("invalid editable path policy")
    if not 0 <= config["max_fix_rounds"] <= 3 or not 1 <= config["max_model_calls"] <= 64:
        raise Blocked("invalid budget")
    if type(config["max_design_reopens"]) is not int or not 0 <= config["max_design_reopens"] <= 3:
        raise Blocked("invalid design reopen budget")
    if not 1 <= config["max_outcome_issues"] <= 10 or not 1 <= config["max_outcome_lessons"] <= 3:
        raise Blocked("invalid outcome retrieval bound")
    if config["local_reviewer"] not in PROVIDERS or not isinstance(config["local_review_model"], str):
        raise Blocked("invalid local reviewer")
    if not 0 <= config["max_local_fix_rounds"] <= 3 or not 600 <= config["local_timeout_seconds"] <= 2400:
        raise Blocked("invalid local iteration budget")
    if not 1 <= config["local_check_timeout_seconds"] <= 900:
        raise Blocked("invalid local check timeout")
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
# Checkpoint room a PR review step keeps for its queued events, the stage transition's reason
# (up to 4,000 characters at the gates) and outcome bookkeeping.
PR_REVIEW_RESERVE = 6000


@dataclass(frozen=True)
class Outcome:
    """How a scheduler should treat a task after one tick; truthy when the tick made progress."""

    kind: str
    progressed: bool = False
    retry_after: int | None = None

    def __bool__(self):
        """Keep `if controller.advance(...)` meaning "this tick changed the task"."""
        return self.progressed


class Controller:
    """Advance at most one bounded stage, preserving intent before remote mutations."""

    def __init__(self, github, agents, config, workspace_factory=None, bundle_root=None, target_root=None,
                 controller_id=ACTIONS, bundle=None):
        """Inject transports so complete lifecycles can be tested without credentials.

        `bundle`, when given, is the instance the agents also use; otherwise it is built here.
        """
        self.gh, self.agents, self.config = github, agents, config
        self.controller_id = controller_id
        self.comment_id = None
        self.checkpoints = None
        self.state = None
        self.issue = None
        self.permissions = {}
        self.trees = {}
        self.workspace_factory = workspace_factory or LocalWorkspace
        self.identities = Identities(config)
        self.bundle = bundle or Bundle(bundle_root, target_root, profiles=config["execution_profiles"])
        self.stage_synced = None
        self.foreign = False
        self.consuming = None
        self.stage_sync_warned = False
        self.stage_fallback = False

    def checkpoint(self):
        """Persist authoritative progress independently of conversation delivery."""
        conversation.compact(self.state)
        operation = self.state.get("operation")
        if (operation and operation["status"] == "completed" and self.consuming is not None
                and (operation["id"], operation["attempt"]) == self.consuming):
            # The stage holds the result; this save (of whichever state copy it keeps) persists its use.
            operation["status"] = "consumed"
            operation.pop("response", None)
        if (operation and "response" in operation
                and len(self.checkpoints.audit_body(self.state).encode()) > CHECKPOINT_LIMIT - RESULT_MARGIN):
            # Too large to retain: recovery then treats the completion as lost, within the allowance.
            del operation["response"]
        self.checkpoints.save(self.state)

    def save(self, publish_events=True, outboxes=True):
        """Persist audit and plan artifacts before publishing their concise comment summary.

        `outboxes=False` publishes the issue conversation only, leaving PR reviews and request
        edits for a later tick: a failure's own notice must not replay what just failed.
        """
        state = self.state
        self.record_outcome()
        if publish_events:
            self.transition(state)
        # Plans are linked from the status comment; an intent saved by an earlier version is spent.
        state.pop('plan_event_intent', None)
        self.checkpoint()
        self.sync_stage()
        if publish_events and conversation.publish(state, self.gh, self.config):
            self.checkpoint()
        if publish_events and outboxes:
            actions.publish(state, self.gh, self.checkpoint)
            findings.publish(state, self.gh, self.config, self.checkpoint)
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
        plan_url = (f"https://github.com/{self.config['repository']}/issues/{state['issue']}/artifacts/{artifact['number']}"
                    if artifact else None)
        actions.transition(state, self.config, (self.issue or {}).get("user", {}).get("login"), plan_url)

    def answerer(self, login):
        """Whether a login may answer an Input needed request: the issue's author or a writer."""
        return not self.identities.ours(login) and (login == self.issue.get("user", {}).get("login")
                                                    or self.permitted(login))

    def summary_body(self):
        """The status comment: stage, plan, PR and votes first; reports and evidence collapsed.

        One that would exceed its limit drops the collapsed reports and shortens the reason; the
        status itself always remains.
        """
        body = self.summary_lines(compact=False)
        if len(body.encode()) > SUMMARY_LIMIT:
            body = self.summary_lines(compact=True)
        return body

    def summary_lines(self, compact):
        """The status comment: a progress line, one row per step, every vote by round, and any action needed.

        `compact` drops the rounds table; reports themselves live in their archives.
        """
        state = self.state
        current = self.current_step()
        rows = [self.step_row(index, name, current) for index, name in enumerate(STEPS)]
        progress = " → ".join(f"**{name} {status.split(' ')[0]}**" if index == current else
                              f"{name} {status.split(' ')[0]}" if not status.startswith("Pending") else name
                              for index, (name, status, _) in enumerate(rows))
        lines = [MARKER, "## Concurro", "", progress, "",
                 markup.table(["Step", "Status", "Detail"], [[name, status, detail] for name, status, detail in rows]), ""]
        action = self.action_line()
        if action:
            lines += [action, ""]
        if not compact and state.get("reports"):
            lines += [markup.details("Rounds", self.rounds_table()), ""]
        return "\n".join(lines + self.checkpoints.links())

    def artifact_url(self, number):
        """An artifact of this issue on GitHub."""
        return f"https://github.com/{self.config['repository']}/issues/{self.state['issue']}/artifacts/{number}"

    def current_step(self):
        """The step the task is on, or the furthest it reached when paused; len(STEPS) once merged."""
        state = self.state
        stage = state["stage"]
        if stage in ("merged", "observed_merged"):
            return len(STEPS)
        if stage in STAGE_STEP:
            return STAGE_STEP[stage]
        if state.get("pr"):
            return 4 if len(state.get("reviews", {})) < len(self.config["reviewers"]) else 5
        if state.get("local_status") or state.get("approval"):
            return 3
        if state.get("design_votes") or stage == "clarify" and not state.get("plan", {}).get("questions") and state.get("plan"):
            return 2
        return 1 if state.get("plan") else 0

    def step_row(self, index, name, current):
        """`(name, status, detail)` of one step; details are engine values and at most one headline."""
        state = self.state
        stage = state["stage"]
        if index > current:
            return name, "Pending", ""
        paused = {"clarify": ("❓", "Input needed"), "blocked": (markup.BLOCKED, "Blocked"),
                  "escalated": (markup.ESCALATED, "Escalated"), "rejected": (markup.BLOCKED, "Rejected"),
                  "stopped": ("⏹️", "Stopped")}
        done = index < current
        intake = state.get("qc", {}).get("intake")
        if name == "Intake":
            status = markup.label(markup.DECISION[intake["decision"]]) if intake else "▶️ In progress"
            detail = markup.headline(intake, "rationale") or "" if intake else ""
        elif name == "Plan":
            version = f"Version {state.get('plan_revisions', 0) + 1}"
            status = f"✅ {version}" if done else "▶️ In progress"
            plan = self.checkpoints.plan
            detail = link("Implementation plan", self.artifact_url(plan["number"])) if plan else ""
        elif name == "Design review":
            roster = len(design_roster(self.config))
            approve = sum(v["report"]["decision"] == "approve" for v in state.get("design_votes", {}).values())
            objections = sum(not v["resolved"] for v in state.get("objections", []))
            round_ = f"Round {state.get('design_generation', 0) + 1} · {state.get('design_phase', 'cooperative')}"
            status = f"✅ {approve} of {roster} approve" if done else (
                "Awaiting writer approval" if stage == "planned" else f"▶️ {approve} of {roster} approve")
            detail = f"{round_} · {objections} open objection{'s' if objections != 1 else ''}"
        elif name == "Implement":
            local = state.get("local_status") or state.get("local_evidence") or {}
            checks = local.get("checks", [])
            passed = sum(c.get("exit_code") == 0 for c in checks)
            status = f"✅ {passed} of {len(checks)} checks" if done else "▶️ In progress"
            parts = []
            if local.get("review"):
                parts.append("Local review " + markup.label(markup.VERDICT[local["review"]["verdict"]]))
            if local.get("local_commit"):
                parts.append("commit " + code(local["local_commit"][:7], cell=True))
            detail = " · ".join(parts)
        elif name == "Code review":
            reviews = state.get("reviews", {})
            approve = sum(r["verdict"] == "approve" for r in reviews.values())
            total = len(self.config["reviewers"])
            status = f"✅ {approve} of {total} approve" if done else f"▶️ {len(reviews)} of {total} reviewed"
            open_findings = sum(f["status"] in ("open", "withdrawn") for f in state.get("findings", []))
            pr = link(f"PR #{state['pr']}", self.pr_url(state["pr"])) if state.get("pr") else ""
            detail = " · ".join(p for p in (pr, f"{open_findings} open finding{'s' if open_findings != 1 else ''}") if p)
        else:
            if current == len(STEPS):
                status, detail = "✅ Merged", ""
            elif stage == "gates" and state.get("handoff_ready"):
                status, detail = "✅ Handed off", "A human reviews and merges"
            else:
                status = f"{markup.GATE} Blocked" if state.get("reason") and stage == "gates" else "▶️ Checking gates"
                detail = text(state.get("reason", ""), 300) if stage == "gates" else ""
        if not done and index == current and stage in paused:
            status = markup.label(paused[stage])
        return name, status, detail

    def action_line(self):
        """One line naming what a human must do, linking the comment that asks."""
        state = self.state
        journal = state.get("conversation", {})
        latest = journal.get("latest")
        url = (f"https://github.com/{self.config['repository']}/issues/{state['issue']}#issuecomment-{latest}"
               if latest else None)
        open_request = next((r for r in reversed(state.get("requests", [])) if r["state"] == "open" and r.get("comment")), None)
        if open_request:
            url = (f"https://github.com/{self.config['repository']}/issues/{state['issue']}"
                   f"#issuecomment-{open_request['comment']}")
            return f"**Action needed:** {link('answer the questions', url)}."
        needs = {"blocked": "a fix and a retry", "escalated": "a decision", "rejected": "an edit and a retry",
                 "planned": "plan approval" if state.get("mode") == "manual" else None}.get(state["stage"])
        if not needs and state["stage"] == "gates" and state.get("handoff_ready"):
            needs = "a human review and merge"
        if not needs:
            return ""
        line = f"**Action needed:** {needs}" + (f" ({link('details', url)})." if url else ".")
        paused = {"blocked": (markup.BLOCKED, "Blocked"), "escalated": (markup.ESCALATED, "Escalated"),
                  "rejected": (markup.BLOCKED, "Rejected")}.get(state["stage"])
        if paused and state.get("reason"):
            line = f"{paused[0]} **{paused[1]}:** {text(state['reason'], 300)}\n\n" + line
        return line

    def rounds_table(self):
        """Every archived vote by step and round, each linking its full report."""
        reports = self.state.get("reports", [])
        providers = list(dict.fromkeys(r["provider"] for r in reports))
        rounds = list(dict.fromkeys((r["step"], r["round"]) for r in reports))
        rows = []
        for step, round_ in rounds:
            cells = []
            for provider in providers:
                entry = next((r for r in reversed(reports)
                              if (r["step"], r["round"], r["provider"]) == (step, round_, provider)), None)
                vocabulary = markup.DECISION if step in ("Intake", "Design review") else markup.VERDICT
                cells.append(link(markup.label(vocabulary[entry["outcome"]]), self.artifact_url(entry["artifact"]))
                             if entry else "—")
            rows.append([text(step), text(round_)] + cells)
        return markup.table(["Step", "Round"] + [text(p) for p in providers], rows)

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
        self.checkpoints = Checkpoints(self.gh, self.config, self.issue["number"])
        self.state = self.checkpoints.load(comments)
        comment = self.checkpoints.comment
        self.comment_id = comment["id"] if comment else None
        self.stage_fallback = bool(comment and comment["body"].startswith(MARKER + "\n## Concurro\n\nStage: **"))

    def tree(self, sha):
        """Fetch a complete tree; never silently treat truncation as missing files."""
        if sha in self.trees:
            return self.trees[sha]
        result = self.gh.repo(f"git/trees/{sha}?recursive=1")
        if result.get("truncated"):
            raise Blocked("repository tree is truncated")
        self.trees[sha] = {v["path"]: v for v in result["tree"]}
        return self.trees[sha]

    def packages(self, paths):
        """For each profile that scopes checks, the packages owning the planned paths at the base (None: all)."""
        tree = self.tree(self.state["base"])

        def read(path):
            # Only content problems fall back to the workspace; transient GitHub errors still propagate.
            try:
                return self.files(self.state["base"], [path], inspection=True)[path]
            except (Blocked, UnicodeError):
                return None

        return {p["id"]: checks.packages(p, paths, tree, read) for p in self.bundle.execution["profiles"]
                if p.get("package_manifest")}

    def files(self, sha, paths, inspection=False):
        """Read bounded text blobs; executable source is evidence-only, never editable."""
        tree = self.tree(sha)
        result = {}
        for path in paths:
            entry = tree.get(path)
            if entry is None:
                # Existing symlink/file ancestors must not be treated as new directories.
                ancestors = ["/".join(path.split("/")[:i]) for i in range(1, len(path.split("/")))]
                if any(a in tree and tree[a]["type"] != "tree" for a in ancestors):
                    raise Blocked("non-directory path ancestor")
                result[path] = None
                continue
            modes = ('100644', '100755') if inspection else ('100644',)
            limit = MAX_FILE if inspection else 128000
            if entry["mode"] not in modes or entry["type"] != "blob" or entry.get("size", 0) > limit:
                raise Blocked("source must be a bounded regular text file")
            blob = self.gh.repo("git/blobs/" + entry["sha"])
            if blob["encoding"] != "base64":
                raise Blocked("unsupported blob encoding")
            data = base64.b64decode(blob['content'])
            if len(data) > limit or b'\x00' in data:
                raise Blocked('source exceeds text bounds')
            result[path] = data.decode('utf-8')
        return result

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
                if not match or not any(instruction_matches(p, pattern.strip())
                                       for p in targets for pattern in match[1].split(",")):
                    continue
            selected[path] = content
        return {"issue": self.state["requirements"], "policy": selected,
                "plan": self.state.get("plan"), "base": self.state["base"],
                "bundle_id": self.bundle.identity, "charter": self.bundle.charter,
                "validation_capabilities": checks.catalog(self.state.get('plan', {}).get('files', []), self.bundle.execution),
                "prior_outcomes_untrusted": self.state.get("prior_outcomes", [])}

    def ask(self, provider, stage, context):
        """Debit the persistent attempt budget before contacting a model.

        A retried or recovered call keeps its original response contract: recorded volatile
        fields replace recomputed ones in the caller's context, which validates the response.
        """
        caller = context
        context = {**context, "repository_commit": self.state.get("head", self.state["base"])}
        size = len(json.dumps(context).encode())
        if size > self.config["max_context_bytes"]:
            raise Blocked(f"{stage} context exceeds the size limit ({size} of {self.config['max_context_bytes']} bytes); "
                          "narrow the change")
        operation = self.operation(provider, stage, context)
        caller.update(operation["contract"])
        context.update(operation["contract"])
        retained = self.recover(operation)
        if retained is not None:
            return retained
        if self.state["calls"] >= self.config["max_model_calls"]:
            raise Blocked("model-call budget exhausted")
        credential = self.agents.credential(provider, stage, {"repository": self.config["repository"],
                                                              "issue": self.state["issue"]})
        for attempt in range(2):
            if self.state["calls"] >= self.config["max_model_calls"]:
                raise Blocked("model-call budget exhausted")
            session = sessions.prepare(self.state, self.config, provider, stage, context)
            request = {**context, "controller_credential": credential.record()}
            if session:
                request["controller_session"] = session
            self.begin_attempt(operation, context)
            self.state["calls"] += 1
            used = self.state.setdefault("credentials", {}).setdefault(
                credential.id, {k: v for k, v in credential.record().items() if k != "id"} | {"calls": 0})
            used["calls"] += 1
            self.save()
            try:
                response = self.agents.ask(provider, stage, request)
            except Exception as error:
                # Timeouts and killed containers may have completed upstream; reported errors did not.
                operation["status"] = "uncertain" if getattr(error, "uncertain", False) else "failed"
                if not isinstance(error, SessionFailure) or not session:
                    raise
                sessions.restart(self.state, session)
                self.save()
                if attempt:
                    raise Blocked("provider session failed twice; a fresh session is prepared for retry") from None
                continue
            if session:
                current = self.state["sessions"][session["role"]]
                current["turn"] += 1
                current["completed"] = {"call": self.state["calls"], "request": digest(context),
                                        "response": digest(response)}
            self.complete(operation, response)
            return response

    def operation(self, provider, stage, context):
        """Bind a model call to its intended work, so incidental request changes keep its allowance."""
        work = {k: v for k, v in context.items() if k not in VOLATILE_REQUEST}
        identifier = digest([self.state["issue"], provider, stage, digest(work)])
        current = self.state.get("operation")
        if not current or current["id"] != identifier or current["status"] == "consumed":
            current = self.state["operation"] = {
                "id": identifier, "provider": provider, "stage": stage, "request": None, "attempts": 0,
                "uncertain": 0, "attempt": None, "status": "new",
                "contract": {k: context[k] for k in VOLATILE_REQUEST if k in context}}
            self.consuming = None
        return current

    def recover(self, operation):
        """Return a durably retained result not yet consumed; a completion without one was lost."""
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
            operation["uncertain"] += 1
        if operation["uncertain"] >= 2:
            raise Blocked("model call outcome was uncertain twice; operator reconciliation required")
        operation.update(attempts=operation["attempts"] + 1, attempt=uuid.uuid4().hex, status="started",
                         request=digest(context))

    def complete(self, operation, response):
        """Checkpoint the result itself, when it fits, until the stage durably consumes it."""
        operation["status"] = "completed"
        operation["response"] = deepcopy(response)
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
        events = [e for e in events if e["event"] in ("labeled", "unlabeled")
                  and e.get("label", {}).get("name") == "concurro"]
        event = events[-1] if events else None
        actor = event.get("actor") if event else None
        if not actor or event["event"] != "labeled" or not self.permitted(actor["login"]):
            return None
        return {"actor": actor["login"], "event": event["id"]}

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
        base = self.gh.repo("git/ref/heads/" + self.config["base"])["object"]["sha"]
        self.state = {"version": 2, "issue": self.issue["number"], "mode": mode, "controller": self.controller_id,
                      "stage": "intake", "enrollment": enrollment,
                      "base": base, "calls": 0, "round": 0, "plan_revisions": 0, "reviews": {},
                      "command_id": command_id, "requirements": self.requirements(comments),
                      "issue_digest": digest([self.issue["title"], self.issue["body"]]),
                      "agent_image": os.environ.get("CONCURRO_AGENT_IMAGE", ""),
                      "execution_image": os.environ.get("CONCURRO_EXECUTION_IMAGE", ""),
                      "config_digest": config_digest(self.config), "bundle_id": self.bundle.identity,
                      "charter_id": digest(self.bundle.charter),
                      "charter": self.bundle.charter, "execution": self.bundle.execution_summary,
                      "design_generation": 0,
                      "design_reopens": 0, "design_votes": {}, "design_history": [],
                      "design_phase": "cooperative"}
        self.state["prior_outcomes"] = self.retrieve_outcomes()
        self.save()

    def revise_plan(self, stage, reason, invalidate=True, save_exhausted=True):
        """Limit automatic plan revisions across QC feedback and changing inputs."""
        if invalidate:
            self.invalidate_design(reason)
        if self.state["plan_revisions"] >= self.config["max_plan_revisions"]:
            self.state.update(stage="escalated", reason="Plan revision budget exhausted: " + reason)
            if save_exhausted:
                self.save()
            return False
        self.state["plan_revisions"] += 1
        self.state.update(stage=stage, reason=reason)
        return True

    def invalidate_design(self, reason):
        """Archive bounded judgment evidence and invalidate all approval generations."""
        state = self.state
        if state.get("plan_id"):
            state["design_history"].append({"generation": state["design_generation"],
                "plan": state["plan_id"], "summary": state["plan"]["summary"][:500],
                "parent": state.get("head"), "reason": reason[:1000],
                "local_evidence_digest": digest(state.get("local_evidence")),
                "votes": [{"identity": v["identity"], "binding": v["binding"],
                    "decision": v["report"]["decision"], "report_digest": digest(v["report"]),
                    "concerns": v["report"]["concerns"]} for v in state.get("design_votes", {}).values()]})
        state["design_generation"] += 1
        state["input_revision"] = state.get("input_revision", 0) + 1
        withdrawn = findings.withdraw(state)
        if state.get("pr"):
            findings.queue_withdrawal(state, withdrawn)
        state["design_phase"] = "cooperative"
        state.pop("cooperative_votes", None)
        state["design_votes"] = {}
        state.pop("approval", None)
        state.pop("local_evidence", None)
        for key in ('plan_draft', 'validation_feedback', 'validation_failure', 'validation_stalls', 'review_repair'):
            state.pop(key, None)

    def reopen_design(self, issue, source):
        """Pause candidate work and preserve the published parent under persistent budgets."""
        validate_design_issue(issue)
        state = self.state
        if state.get("pending"):
            raise Blocked("prepared publication must be reconciled before reopening design")
        state.setdefault("reopen_history", []).append({"source": source, "issue": issue,
            "head": state.get("head"), "plan": state["plan_id"]})
        state["design_feedback"] = issue
        self.invalidate_design(issue["explanation"])
        if state["design_reopens"] >= self.config["max_design_reopens"]:
            state.update(stage="escalated", reason="Design reopen budget exhausted.")
            self.save()
            return
        state["design_reopens"] += 1
        if not self.revise_plan("plan", issue["explanation"], invalidate=False):
            return
        state["design_parent"] = state.get("head")
        if state.get("pr"):
            changed = [f["filename"] for f in self.gh.pages(f"pulls/{state['pr']}/files")]
            if not changed or not all(path_allowed(p, self.config, self.state["execution"]) for p in changed):
                raise Blocked("cannot reconcile accumulated PR changes")
            state["changed_files"] = changed
        self.save()

    def record_outcome(self):
        """Persist bounded outcome evidence in the bot-owned GitHub state, without policy edits."""
        state = self.state
        if state["stage"] not in ("merged", "observed_merged", "rejected", "escalated", "blocked", "stopped"):
            return
        outcome = {"version": 1, "status": state["stage"], "issue": state["issue"],
            "title": state["requirements"]["title"][:200], "reason": state.get("reason", "")[:1000],
            "plan_summary": state.get("plan", {}).get("summary", "")[:500],
            "goals": sorted({goal for vote in state.get("design_votes", {}).values()
                             for goal in vote["report"]["scope"]["goal_ids"]}),
            "files": state.get("plan", {}).get("files", []), "head": state.get("head"),
            "plan": state.get("plan_id"), "bundle": state.get("bundle_id"),
            "calls": state["calls"], "local_evidence_digest": digest(state.get("local_evidence")),
            "reviews_digest": digest(state.get("reviews", {})),
            "completion": state.get("completion")}
        history = state.setdefault("outcomes", [])
        if not history or history[-1] != outcome:
            history.append(outcome)
            del history[:-6]

    def retrieve_outcomes(self):
        """Read a fixed GitHub window and select related, explicitly untrusted lessons."""
        state = self.state
        issues = self.gh.outcome_candidates(self.config["max_outcome_issues"])
        words = set(re.findall(r"[a-z]{4,}", state["requirements"]["title"].lower()))
        lessons = []
        for issue, comments in issues[:self.config["max_outcome_issues"]]:
            if issue == state["issue"]:
                continue
            try:
                prior = Checkpoints(self.gh, self.config, issue).load(comments[:10])
                if prior is None:
                    continue
                outcome = prior.get("outcomes", [])[-1]
                relevance = len(words & set(re.findall(r"[a-z]{4,}", outcome["title"].lower())))
                if not relevance or outcome.get("version") != 1:
                    continue
                lesson = {k: outcome.get(k) for k in ("issue", "status", "title", "reason", "plan_summary",
                                                     "goals", "files", "head", "plan", "bundle", "completion")}
                if len(json.dumps(lesson).encode()) <= 4000:
                    lessons.append((relevance, issue, lesson))
            except (CheckpointError, ValueError, KeyError, IndexError, TypeError):
                continue
        lessons.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [lesson for _, _, lesson in lessons[:self.config["max_outcome_lessons"]]]

    def refresh_autonomous(self, comments):
        """Resume clarification or invalidate stale inputs without resetting any budget."""
        state = self.state
        if self.enrollment() != state["enrollment"]:
            raise Blocked("autonomous enrollment removed, changed or no longer authorized")
        requirements = self.requirements(comments)
        base = self.gh.repo("git/ref/heads/" + self.config["base"])["object"]["sha"]
        changed = requirements != state["requirements"] or base != state["base"]
        if not changed:
            return state["stage"] != "clarify"
        if state.get("head") or state.get("pending"):
            if base != state["base"] or state.get("pending"):
                raise Blocked("approved base or prepared publication changed; reconcile manually")
            return self.resume_with_head(requirements)
        if not self.revise_plan("intake", "Requirements or base changed; QC is reassessing."):
            return False
        state.update(requirements=requirements, base=base,
                     issue_digest=digest([self.issue["title"], self.issue["body"]]))
        for key in ("plan", "plan_id", "branch", "qc"):
            state.pop(key, None)
        self.save()
        return True

    def review_output_budget(self, context):
        """Budget both the review response and its duplicate copies in the audit checkpoint."""
        remaining = deepcopy(self.state)
        remaining.get('qc', {}).pop('plan_qc', None)
        remaining.pop('review_repair', None)
        # A retained result is temporary and dropped when its stage saves.
        remaining.get('operation', {}).pop('response', None)
        room = CHECKPOINT_LIMIT - len(self.checkpoints.audit_body(remaining).encode()) - 10000
        return {'max_json_bytes': max(1000, min(room // 3, design.report_limit(
                    len(context['review_criteria']), len(context.get('own_objections', []))))),
                'encoding': 'JSON with non-ASCII characters escaped'}

    def pr_review_room(self):
        """Checkpoint bytes a PR review may add, keeping the step's reserve.

        A retained model result is not counted: `checkpoint` drops it when it would not fit,
        and recovery then treats the result as lost, within its allowance.
        """
        remaining = deepcopy(self.state)
        remaining.get('operation', {}).pop('response', None)
        return CHECKPOINT_LIMIT - PR_REVIEW_RESERVE - len(self.checkpoints.audit_body(remaining).encode())

    def pr_review_output_budget(self):
        """The reviewer's budget: half the room, since a review's finding text is kept twice."""
        return {'max_json_bytes': max(1000, min(self.pr_review_room() // 2, PR_REVIEW_LIMIT)),
                'encoding': 'JSON with non-ASCII characters escaped'}

    def record_pr_review(self, state, provider, report):
        """Apply a validated PR review to the ledger and queue its publication on the PR.

        A design reopen is kept as the pending review event, not as the head's report.
        """
        sequence, identifiers = findings.apply(state, provider, report, state["head"])
        report.update(head=state["head"], model=self.config["models"][provider] or "CLI default",
                      input_revision=state.get("input_revision", 0), sequence=sequence, finding_ids=identifiers)
        if report["verdict"] == "reopen":
            state["review_event"] = report
        else:
            state["reviews"][provider] = report
        findings.queue_review(state, provider, report)

    def validate_design_report(self, report, context):
        """Validate reviewer output before recording any vote or resolving an objection."""
        if len(json.dumps(report).encode()) > context['review_output_budget']['max_json_bytes']:
            raise design.ReviewError('review_size')
        try:
            validate_qc(report, 'plan_qc', self.bundle.charter, len(context['review_criteria']),
                        len(context.get('own_objections', [])))
        except (Blocked, TypeError, KeyError, ValueError):
            raise design.ReviewError('review_shape') from None
        own = context.get('own_objections', [])
        try:
            design.validate_evidence(report, self.state['requirements'], own)
        except design.ReviewError:
            raise
        except (Blocked, TypeError, KeyError, ValueError):
            raise design.ReviewError('review_evidence') from None
        try:
            source_files = self.files(self.state.get('head') or self.state['base'],
                                     list({v['path'] for v in report['sources']}), inspection=True)
        except (Blocked, UnicodeError, ValueError):
            raise design.ReviewError('review_source') from None
        design.validate_evidence(report, self.state['requirements'], own, source_files)

    def repair_review(self, report, error, member):
        """Ask only the failed reviewer to correct output, under durable task-wide budgets."""
        state = self.state
        binding = digest([approval_binding(state, self.config), member])
        signature = digest([error.code, report])
        previous = state.get('review_repair', {})
        repeated = previous.get('binding') == binding and previous.get('signature') == signature
        state['review_repair'] = {'binding': binding, 'signature': signature, 'feedback': {
            'code': error.code, 'field': error.field, 'detail': design.REPAIR_DETAILS[error.code]}}
        state.pop('approval', None)
        if repeated or state.get('review_repairs', 0) >= self.config['max_review_repairs']:
            state.update(stage='escalated', retry_stage='plan_qc',
                         reason='Review output repair made no progress or exhausted its budget.')
        else:
            state['review_repairs'] = state.get('review_repairs', 0) + 1
            state.update(stage='plan_qc', reason='Review output needs correction: ' + error.code)
        self.save()

    def quality_control(self, stage):
        """Gather independent votes without exposing any peer judgments within a round."""
        state = self.state
        context = self.context()
        member = None
        phase = state.get("design_phase", "cooperative")
        if stage == "plan_qc":
            if checks.contract_errors(state['plan'], state['execution']):
                raise Blocked('design review requires a valid executable validation contract')
            roster = design_roster(self.config)
            member = next((m for m in roster if m["id"] not in state["design_votes"]), None)
            if member is None:
                if not consensus_valid(state, self.config, require_fresh=False):
                    raise Blocked("design votes are missing, stale or contradictory")
                self.finish_consensus()
                self.save()
                return
            context["review_phase"] = phase
            context['review_criteria'] = design.criteria(state['requirements'])
            context['plan'] = {k: v for k, v in context['plan'].items() if k not in ('summary', 'publication')}
            if phase == "cooperative":
                context["own_objections"] = [v for v in state.get("objections", []) if v["reviewer"] == member["id"]]
            else:
                context["plan"] = {k: v for k, v in context["plan"].items() if k != "responses"}
            context.update(files=self.files(state["base"], state["plan"]["files"]),
                           design_binding=approval_binding(state, self.config), reviewer_identity=member,
                           accumulated_changed_files=state.get("changed_files", []),
                           published_parent=state.get("design_parent"))
        else:
            context["editable_files"] = [p for p in self.tree(state["base"]) if path_allowed(p, self.config, self.state["execution"])]
        if member and state.get("head"):
            context["published_files"] = self.files(state["head"], state["plan"]["files"])
        provider = member["provider"] if member else self.config["qc"]
        if member:
            context['review_output_budget'] = self.review_output_budget(context)
            repair = state.get('review_repair', {})
            if repair.get('binding') == digest([approval_binding(state, self.config), member]):
                context['review_validation_feedback'] = repair['feedback']
        report = self.ask(provider, stage, context)
        resolved, disposition_ids = [], []
        if member:
            try:
                self.validate_design_report(report, context)
            except design.ReviewError as error:
                self.repair_review(report, error, member)
                return
            # Speculate locally; invalid or oversized output must never enter durable votes.
            state = deepcopy(self.state)
            state.pop('review_repair', None)
            resolved, disposition_ids = design.update_objections(state, report, member, phase)
        else:
            validate_qc(report, stage, self.bundle.charter)
        state.setdefault("qc", {})[stage] = report
        state.setdefault("qc_history", []).append({"stage": stage, "decision": report["decision"],
            "risk": report["risk"], "rationale": report["rationale"][:500], "report_digest": digest(report),
            "base": state["base"], "plan": state.get("plan_id"), "provider": provider,
            "model": member["model"] if member else selected_model(self.config, provider, stage),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "round": state["design_generation"] + 1, "phase": phase if member else "intake"})
        identity = member or {"id": digest([provider, "intake"]), "provider": provider,
                              "model": selected_model(self.config, provider, stage)}
        try:
            conversation.queue(state, None, source=("qc", stage), kind="qc", details={
                                   "provider": provider, "model": identity["model"],
                                   "plan": state.get("plan_id", "intake"), "round": state["design_generation"] + 1,
                                   "phase": phase if member else "intake", "reviewer": identity["id"],
                                   "resolved": resolved, "disposition_ids": disposition_ids})
        except Blocked:
            if not member:
                raise
            self.repair_review(report, design.ReviewError('review_checkpoint'), member)
            return

        state["reason"] = report["rationale"]
        decision = report["decision"]
        if member:
            state["design_votes"][member["id"]] = {"identity": member,
                "binding": approval_binding(state, self.config), "report": report,
                "objection_ids": [v['id'] for v in context.get('own_objections', [])]}
            if len(self.checkpoints.audit_body(state).encode()) > CHECKPOINT_LIMIT - 6000:
                self.repair_review(report, design.ReviewError('review_checkpoint'), member)
                return
        previous = self.state
        self.state = state
        try:
            self.finish_quality_control(stage, report)
            self.record_outcome()
            self.transition(state)
            if member and len(self.checkpoints.audit_body(state).encode()) > CHECKPOINT_LIMIT - 1000:
                raise design.ReviewError('review_checkpoint')
        except Blocked:
            self.state = previous
            if not member:
                raise
            self.repair_review(report, design.ReviewError('review_checkpoint'), member)
            return
        self.save()

    def finish_quality_control(self, stage, report):
        """Apply a validated vote's transition before the final checkpoint capacity check."""
        state, decision = self.state, report['decision']
        if report["risk"] == "high" or decision == "escalate":
            state.update(stage="escalated", reason=ESCALATION_REASON + " " + report["rationale"])
        elif decision in ("reject", "clarify"):
            state["stage"] = "rejected" if decision == "reject" else "clarify"
        elif stage == "intake":
            state["stage"] = "plan"
        elif len(state["design_votes"]) == len(design_roster(self.config)):
            objections = [vote["report"] for vote in state["design_votes"].values()
                          if vote["report"]["decision"] != "approve"]
            if objections:
                state["design_feedback"] = objections
                self.revise_plan("plan", "Design reviewers require a revision.", save_exhausted=False)
            else:
                self.finish_consensus()
        if state["stage"] in ("rejected", "escalated"):
            state["retry_stage"] = "intake"
            state.pop("approval", None)

    def finish_consensus(self):
        """Authorize implementation only after a complete current unanimous round."""
        state = self.state
        if not consensus_valid(state, self.config, require_fresh=False):
            raise Blocked("unanimous design consensus missing or stale")
        if state.get("design_phase") != "fresh":
            state.update(design_phase="fresh", design_votes={})
            return
        if state.get("mode") == "manual":
            state["stage"] = "planned"
        else:
            state.update(stage="implement", approval={"kind": "consensus", "plan": state["plan_id"],
                                                       "binding": approval_binding(state, self.config)})

    def suspend(self, stage, reason=None, publish_events=True, outboxes=True):
        """Pause work without replacing an earlier resume target on repeated failures."""
        if self.state["stage"] not in ("blocked", "stopped"):
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
        if (state["stage"] == "clarify"
                and all(requirements[key] == state["requirements"][key] for key in ("title", "body"))):
            self.invalidate_design("Clarification answered for the open design.")
            state.update(stage="plan", requirements=requirements)
            self.save()
            return True
        self.reopen_design({"kind": "requirements", "explanation": "Authorized issue requirements changed.",
                            "evidence": [digest(requirements)]}, "requirements")
        state.update(requirements=requirements, issue_digest=digest([self.issue["title"], self.issue["body"]]))
        self.save()
        return state["stage"] == "plan"

    def commands(self, comments):
        """Apply exact writer commands; plan approval is bound to its full digest."""
        for comment in comments:
            if comment["id"] <= (self.state or {}).get("command_id", 0):
                continue
            text = comment["body"].strip()
            if not re.fullmatch(r"/concurro (start|approve [a-f0-9]{64}|stop|retry)", text):
                continue
            if not self.permitted(comment["user"]["login"]):
                continue
            if text == "/concurro start":
                if self.config["autonomous"]:
                    continue
                if self.state and self.state["stage"] not in ("clarify", "blocked", "stopped"):
                    continue
                if self.state and self.state["stage"] == "clarify" and self.state.get("head") and not self.state.get("pending"):
                    # A clarification with a PR resumes its open design, as autonomous refresh does.
                    self.state["command_id"] = comment["id"]
                    self.resume_with_head(self.requirements(comments))
                    continue
                if self.state and any(self.state.get(key) for key in ("pr", "head", "pending")):
                    raise Blocked("existing PR or prepared publication requires manual reconciliation before restarting")
                if self.state:
                    if not self.revise_plan("intake", "Writer requested reassessment."):
                        continue
                    base = self.gh.repo("git/ref/heads/" + self.config["base"])["object"]["sha"]
                    self.state.update(base=base, requirements=self.requirements(comments), command_id=comment["id"],
                        issue_digest=digest([self.issue["title"], self.issue["body"]]))
                    for key in ("plan", "plan_id", "branch", "qc", "design_parent", "approved_accumulated_files"):
                        self.state.pop(key, None)
                    self.save()
                else:
                    self.initialize([c for c in comments if c["id"] <= comment["id"]], "manual", comment["id"])
            elif (self.state and text.startswith("/concurro approve ") and self.state["stage"] == "planned"
                  and self.state.get("mode", "manual") == "manual"):
                if text.split()[-1] == self.state["plan_id"]:
                    if not consensus_valid(self.state, self.config):
                        raise Blocked("writer command cannot waive design consensus")
                    self.state.update(stage="implement", command_id=comment["id"],
                                      approval={"kind": "human", "binding": approval_binding(self.state, self.config),
                                                "plan": self.state["plan_id"],
                                                "actor": comment["user"]["login"], "comment": comment["id"]})
                    self.save()
            elif (self.state and text == "/concurro retry" and self.state["stage"] in ("blocked", "stopped", "rejected", "escalated")
                  and self.state.get("retry_stage")):
                output_repair = self.state.get('review_repair') and self.state['retry_stage'] == 'plan_qc'
                if self.state["stage"] in ("rejected", "escalated") and not output_repair:
                    if not self.revise_plan("intake", "Writer requested reassessment after terminal judgment."):
                        continue
                    # Reassessment reads the issue as it is now, including edits made to answer the escalation.
                    self.state.update(requirements=self.requirements(comments),
                                      issue_digest=digest([self.issue["title"], self.issue["body"]]))
                    self.state.pop("retry_stage", None)
                    self.state["command_id"] = comment["id"]
                else:
                    self.state.update(stage=self.state.pop("retry_stage"), command_id=comment["id"])
                self.state.pop("reason", None)
                # A writer's retry authorizes a fresh attempt allowance for an uncertain call.
                self.state.pop("operation", None)
                self.save()
            elif self.state and text == "/concurro stop":
                self.state["command_id"] = comment["id"]
                self.suspend("stopped")

    def plan(self):
        """Triage and attach a structured plan without making code changes."""
        context = self.context()
        context["design_feedback"] = self.state.get("design_feedback")
        context['validation_feedback'] = self.state.get('validation_feedback', [])
        if self.state.get('plan_draft'):
            context['plan'] = self.state['plan_draft']
            context['validation_capabilities'] = checks.catalog(context['plan']['files'], self.bundle.execution)
        context["open_objections"] = [v for v in self.state.get("objections", []) if not v["resolved"]]
        context["editable_files"] = [p for p in self.tree(self.state["base"]) if path_allowed(p, self.config, self.state["execution"])]
        if self.state.get("head"):
            paths = self.state["changed_files"]
            context.update(accumulated_before=self.files(self.state["base"], paths),
                           accumulated_after=self.files(self.state["head"], paths))
        plan = self.ask(self.config["planner"], "plan", context)
        validate_plan(plan, self.config, self.state["execution"])
        expected = {v["id"] for v in context["open_objections"]}
        if {v["id"] for v in plan["responses"]} != expected or len(plan["responses"]) != len(expected):
            raise Blocked("planner must respond to every open objection exactly once")
        if self.state.get("head") and not plan["questions"]:
            if not all(checks.in_scope(path, plan) for path in self.state.get("changed_files", [])):
                raise Blocked("revised scope does not reconcile the accumulated PR diff")
        tree = self.tree(self.state['base'])
        profiles = checks.approved_profiles(plan, tree, self.state['execution']) if not plan['questions'] else []
        errors = checks.contract_errors(plan, self.state['execution'], profiles) if not plan['questions'] else []
        if not errors and not plan['questions']:
            if any((r['change'] == 'modify') != (r['path'] in tree) for r in plan['validation']['files']):
                errors = ['validation_change']
        if errors:
            self.repair_validation(plan, errors)
            return
        self.state["plan"] = plan
        # Derived from the plan and the approved base, so the design's approval binding covers it.
        self.state["approved_profiles"] = profiles
        self.state.pop('design_feedback', None)
        self.state.pop('review_repair', None)
        for key in ('plan_draft', 'validation_feedback', 'validation_failure', 'validation_stalls'):
            self.state.pop(key, None)
        self.state["plan_id"] = digest({"plan": plan, "base": self.state["base"],
                                        "requirements": self.state["requirements"]})
        if not self.state.get("head"):
            self.state["branch"] = f"concurro/issue-{self.issue['number']}-{self.state['plan_id'][:12]}"
        self.state["design_votes"] = {}
        self.state["design_phase"] = "cooperative"
        self.state["approved_accumulated_files"] = self.state.get("changed_files", [])
        self.state["design_parent"] = self.state.get("head")
        self.state["stage"] = "clarify" if plan["questions"] else "plan_qc"
        self.save()

    def repair_validation(self, plan, errors):
        """Repair machine-checkable contract defects separately from semantic design revisions."""
        state = self.state
        signature = digest([errors, plan.get('validation')])
        stalls = state.get('validation_stalls', 0) + 1 if state.get('validation_failure') == signature else 0
        state.update(plan_draft=plan, validation_feedback=[{'code': c, 'detail': checks.REPAIR_DETAILS[c]} for c in errors],
                     validation_failure=signature, validation_stalls=stalls)
        if stalls or state.get('validation_repairs', 0) >= self.config['max_validation_repairs']:
            state.update(stage='escalated', reason='Validation repair made no progress or exhausted its budget.', retry_stage='plan')
            state.pop('approval', None)
        else:
            state['validation_repairs'] = state.get('validation_repairs', 0) + 1
            state.update(stage='plan', reason='Validation contract needs repair: ' + ', '.join(errors))
        self.save()

    def local_report(self, attempt, report):
        """Persist compact evidence to GitHub and optional full diagnostics as a CI artifact."""
        state = self.state
        compact = {k: v for k, v in report.items() if k not in ("edits", "checks")}
        compact["checks"] = [{k: v for k, v in c.items() if k != "output"} for c in report.get("checks", [])]
        compact.update(attempt=attempt, round=state["round"], run_id=os.environ.get("GITHUB_RUN_ID"))
        changed = state.get("local_status") != compact
        state["local_status"] = compact
        # Only an attempt's final status, with its local review, is archived.
        if changed and compact.get("review"):
            conversation.queue(state, None, source=("local_status",), kind="local",
                               details={"reviewer": self.config["local_reviewer"]})
        directory = os.environ.get("CONCURRO_LOCAL_REPORT_DIR")
        if directory:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            path.joinpath(f"issue-{state['issue']}-round-{state['round']}-attempt-{attempt}.json").write_text(json.dumps(report))
        self.save()

    def design_authorization(self):
        """Attest the supplied implementation plan only from current validated controller consensus."""
        state = self.state
        if not approval_valid(state, self.config):
            raise Blocked("implementation authorization requires complete current design approval")
        roster = design_roster(self.config)
        return {"verified_by": "controller", "approval_kind": state["approval"]["kind"],
                "binding": approval_binding(state, self.config), "plan_id": state["plan_id"],
                "plan_digest": digest(state["plan"]), "base": state["base"],
                "bundle_id": state["bundle_id"], "charter_id": state["charter_id"],
                "requirements_digest": digest(state["requirements"]),
                "generation": state["design_generation"], "required_identities": roster,
                "successful_votes": [{"identity": member, "binding": state["design_votes"][member["id"]]["binding"],
                                      "decision": "approve"}
                                     for member in roster]}

    def develop_locally(self, parent, originals):
        """Iterate edits, real tests and one local review before creating any remote Git objects."""
        state = self.state
        self.design_authorization()
        deadline = time.monotonic() + self.config["local_timeout_seconds"]
        required = list(dict.fromkeys(approved_checks(state) + state['plan']['validation']['checks']))
        # Files earlier published rounds changed; with the parent's changes they make the cumulative change.
        accumulated = state.get("changed_files", []) if state.get("head") else []
        feedback = None
        round_key = f"{state['design_generation']}:{state['round']}"
        attempts = state.setdefault("local_attempts", {})
        if attempts.get(round_key, 0) > self.config["max_local_fix_rounds"]:
            raise Blocked("local repair budget exhausted")
        archive = self.gh.archive(parent)
        # The approved base is always staged: preparation reads only it, and `scope` judges the candidate against it.
        base_archive = archive if parent == state["base"] else self.gh.archive(state["base"])
        with self.workspace_factory(self.config, archive, state["branch"], execution=self.bundle.execution,
                                    base_archive=base_archive) as workspace:
            if workspace.files(state["plan"]["files"]) != originals:
                raise Blocked("source archive does not match the pinned GitHub files")
            scope = checks.scope_of(state["plan"])
            workspace.configure_validation(state['plan']['validation'], self.files(state['base'], state['plan']['files']), {
                "paths": scope["paths"], "max_files": scope["max_files"], "max_changed_lines": scope["max_changed_lines"],
                "accumulated": accumulated, "unapproved": checks.unapproved_triggers(approved_profile_ids(state), state["execution"])})
            workspace.prepare(required, min(600, deadline - time.monotonic()))
            executable = checks.executable(state["execution"], checks.preparing(required, state["execution"]))
            for attempt in range(attempts.get(round_key, 0), self.config["max_local_fix_rounds"] + 1):
                if deadline - time.monotonic() < 600:
                    raise Blocked("local time budget cannot accommodate another agent turn")
                attempts[round_key] = attempt + 1
                changed = self.changed(workspace, accumulated)
                context = self.context(changed)
                # Inspection overlays these current texts, so non-primary edits are visible to repair turns.
                overlay = sorted({*state["plan"]["files"], *changed})
                if len(overlay) > checks.MAX_OVERLAY_FILES:
                    raise Blocked("candidate changes too many files to inspect; it is far over the approved budget")
                context.update(files=workspace.files(overlay),
                               prior_reviews=state["reviews"], changed_files=changed,
                               review_findings=findings.open_findings(state),
                               local_feedback=feedback, available_checks=executable, required_checks=required,
                               design_authorization=self.design_authorization())
                result = self.ask(self.config["implementer"], "develop", context)
                if not isinstance(result, dict) or set(result) != {"summary", "edits", "checks", "design_issue"}:
                    raise Blocked("invalid local development response")
                if result["design_issue"] is not None:
                    if result["edits"] or result["checks"]:
                        raise Blocked("design reopening cannot include candidate edits or checks")
                    self.reopen_design(result["design_issue"], "implementation")
                    return None
                proposal = {"summary": result["summary"], "edits": result["edits"]}
                if result["edits"]:
                    current = workspace.files(sorted({e.get("path") for e in result["edits"]
                                                      if isinstance(e, dict) and isinstance(e.get("path"), str)}))
                    try:
                        validate_edits(proposal, state["plan"], current, self.config, state["execution"])
                    except OutsideScope as error:
                        # Refused whole, before anything is applied; the implementer may reopen the design.
                        feedback = {"scope": str(error) + ". Keep edits inside the approved scope, or return a "
                                             "design_issue if the change needs them."}
                        continue
                elif not workspace.export()["edits"]:
                    raise Blocked("initial development attempt must propose changes")
                checkpoint = workspace.checkpoint(result["edits"])
                changed = self.changed(workspace, accumulated)
                workspace.scope(self.packages(changed))
                reports = workspace.run_checks(required, result["checks"], deadline)
                export = workspace.export()
                record = {**export, "checks": reports, "image": state["execution_image"]}
                self.local_report(attempt, record)
                if any(resource_exhausted(c) for c in reports):
                    raise Blocked("local check exhausted execution resources; inspect diagnostics and runner capacity before retrying")
                if any(c["exit_code"] != 0 for c in reports):
                    feedback = {"checks": reports}
                    continue
                if deadline - time.monotonic() < 600:
                    raise Blocked("local time budget cannot accommodate pre-publication review")
                before, after = self.files(state["base"], changed), workspace.files(changed)
                review_context = self.context(changed)
                review_context.update(before=before, after=after, local_checks=reports,
                                      local_commit=checkpoint["local_commit"])
                oversized = self.oversized(review_context, before, after)
                if oversized:
                    feedback = {"checks": reports, "capacity": oversized}
                    continue
                review = self.ask(self.config["local_reviewer"], "local_review", review_context)
                validate_review(review)
                if review["verdict"] == "reopen":
                    self.reopen_design(review["design_issue"], "local_review")
                    return None
                record["review"] = review
                self.local_report(attempt, record)
                if review["verdict"] != "approve":
                    feedback = {"checks": reports, "review": review}
                    continue
                final = {"summary": result["summary"], "edits": export["edits"]}
                validate_edits(final, state["plan"], self.files(parent, [e["path"] for e in export["edits"]]),
                               self.config, state["execution"])
                evidence = {k: v for k, v in record.items() if k != "edits"}
                evidence["checks"] = [{k: v for k, v in c.items() if k != "output"} for c in reports]
                evidence.update(parent=parent, edits_digest=digest(export["edits"]),
                                reviewer=self.config["local_reviewer"],
                                model=selected_model(self.config, self.config["local_reviewer"], "local_review") or "CLI default")
                return final, evidence
        raise Blocked("local tests or review still fail; local repair budget exhausted")

    @staticmethod
    def changed(workspace, accumulated):
        """Files the candidate changes since the approved base: this round's and earlier published rounds'."""
        return sorted({e["path"] for e in workspace.export()["edits"]} | set(accumulated))

    def oversized(self, local_context, before, after):
        """Why a review of this candidate would not fit, or None.

        Measures the local review and each reviewer's prospective PR review as they would be sent,
        with the same serialization and limit as `ask()`. Files are never omitted to make one fit.
        """
        contexts = [("local review", local_context)]
        contexts += [(provider + " PR review", self.pr_review_context(provider, before, after, "0" * 40))
                     for provider in self.config["reviewers"]]
        limit = self.config["max_context_bytes"] - DISPATCH_ALLOWANCE
        over = [f"{name} ({size} of {limit} bytes)" for name, context in contexts
                if (size := len(json.dumps(context).encode())) > limit]
        if not over:
            return None
        return ("The change is too large to review: " + ", ".join(over) + ". Reduce it, or return a design_issue "
                "to split the work.")

    def pr_review_context(self, provider, before, after, head):
        """One reviewer's PR review context for a candidate's changed files."""
        context = self.context(sorted(after))
        context.update(before=before, after=after, head=head)
        context["own_findings"] = findings.context(self.state, provider)
        context["review_output_budget"] = self.pr_review_output_budget()
        return context

    def reserve_development_branch(self):
        """Create a linked base-only branch after approval, with durable intent for lost responses."""
        state = self.state
        if state.get("head"):
            return
        branch, base = state["branch"], state["base"]
        reservations = state.setdefault("development_branches", {})
        if branch not in reservations:
            reservations[branch] = {"base": base, "linked": False}
            self.save()
        if reservations[branch]["base"] != base:
            raise Blocked("development branch reservation base changed")
        refs = self.gh.repo("git/matching-refs/heads/" + branch)
        refs = [ref for ref in refs if ref["ref"] == "refs/heads/" + branch]
        if refs:
            if refs[0]["object"]["sha"] != base or not self.gh.branch_linked(state["issue"], branch):
                raise Blocked("development branch moved or is not linked to this issue")
        else:
            self.gh.create_linked_branch(self.issue["node_id"], branch, base)
            if not self.gh.branch_linked(state["issue"], branch):
                raise Blocked("development branch link was not confirmed")
        reservations[branch]["linked"] = True
        self.save()

    def implement(self):
        """Reserve a linked base branch; publish candidate objects only after local acceptance."""
        self.design_authorization()
        self.reserve_development_branch()
        state = self.state
        parent = state.get("head", state["base"])
        originals = self.files(parent, state["plan"]["files"])
        developed = self.develop_locally(parent, originals)
        if developed is None:
            return
        result, evidence = developed
        edits = validate_edits(result, state["plan"], self.files(parent, [e["path"] for e in result["edits"]]),
                               self.config, state["execution"])
        # Git Trees API accepts content directly, avoiding filesystem extraction and hooks.
        parent_commit = self.gh.repo("git/commits/" + parent)
        tree = self.gh.repo("git/trees", "POST", {"base_tree": parent_commit["tree"]["sha"], "tree": [
            {"path": e["path"], "mode": "100644", "type": "blob", "content": e["content"]} for e in edits]})
        commit = self.gh.repo("git/commits", "POST", {
            "message": f"fix(concurro): address issue #{state['issue']} (round {state['round']})",
            "tree": tree["sha"], "parents": [parent]})
        state["pending"] = {"head": commit["sha"], "parent": parent,
                            "summary": result["summary"], "initial": "head" not in state}
        state["local_evidence"] = {**evidence, "head": commit["sha"]}
        state["stage"] = "publish"
        self.save()

    def publish(self):
        """Reconcile interrupted publication without overwriting a foreign branch tip."""
        state, pending = self.state, self.state["pending"]
        if not local_evidence_valid(state):
            raise Blocked("local tests and review do not authorize this publication")
        refs = self.gh.repo("git/matching-refs/heads/" + state["branch"])
        refs = [r for r in refs if r["ref"] == "refs/heads/" + state["branch"]]
        if refs:
            current = refs[0]["object"]["sha"]
            if current != pending["head"]:
                reservation = state.get("development_branches", {}).get(state["branch"])
                if current != pending["parent"] or (pending["initial"] and (
                        reservation != {"base": pending["parent"], "linked": True}
                        or not self.gh.branch_linked(state["issue"], state["branch"]))):
                    raise Blocked("branch moved outside the controller")
                self.gh.repo("git/refs/heads/" + state["branch"], "PATCH",
                             {"sha": pending["head"], "force": False})
        elif pending["initial"] and state["branch"] not in state.get("development_branches", {}):
            # Recover prepared publication from controllers predating branch reservations.
            self.gh.create_linked_branch(self.issue["node_id"], state["branch"], pending["head"])
        else:
            raise Blocked("implementation branch disappeared")
        owner = self.config["repository"].split("/")[0]
        prs = self.gh.pages("pulls?state=all&head=" + quote(owner + ":" + state["branch"]) +
                            "&base=" + self.config["base"])
        if len(prs) > 1 or (prs and prs[0]["state"] != "open"):
            raise Blocked("PR was closed or duplicated; reconcile manually")
        if prs:
            pr = prs[0]
        else:
            pr = self.gh.repo("pulls", "POST", {"title": self.pr_title(), "head": state["branch"],
                                                "base": self.config["base"], "body": self.pr_body()})
        if state["reviews"]:
            state.setdefault("review_history", []).append(state["reviews"])
        state.update(head=pending["head"], pr=pr["number"], reviews={}, stage="review",
                     input_revision=state.get("input_revision", 0) + 1)
        state["changed_files"] = [f["filename"] for f in self.gh.pages(f"pulls/{pr['number']}/files")]
        del state["pending"]
        self.save()

    def pr_title(self):
        """The plan's headline, or its summary's first sentence, as plain text within 72 characters."""
        plan, suffix = self.state["plan"], f" (#{self.state['issue']})"
        title = (markup.one_line(plan.get("headline") or "") or markup.first_sentence(plan["summary"])
                 or markup.one_line(plan["summary"]))
        title = title.rstrip(".")
        if not title:
            return f"Address issue #{self.state['issue']}"
        limit = PR_TITLE_LIMIT - len(suffix)
        if len(title) > limit:
            cut = title[:limit - 1]
            # Cut at a word boundary unless the first word alone is too long.
            title = (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" ,;:-") + "…"
        return title + suffix

    def pr_body(self):
        """The plan's summary, its artifact, and the issue it closes; GitHub shows CI and reviews natively."""
        plan = self.checkpoints.plan
        lines = [block(self.state["plan"]["summary"]), ""]
        if plan:
            lines += [link("Implementation plan", self.artifact_url(plan["number"])), ""]
        return "\n".join(lines + [f"Closes #{self.state['issue']}"])

    def pr_url(self, number):
        """The task's pull request on GitHub."""
        return f"https://github.com/{self.config['repository']}/pull/{number}"

    def review(self):
        """Run one fresh provider review against the same immutable before/after files."""
        state = self.state
        provider = next((p for p in self.config["reviewers"] if p not in state["reviews"]), None)
        if provider:
            changed = state["changed_files"]
            context = self.pr_review_context(provider, self.files(state["base"], changed),
                                             self.files(state["head"], changed), state["head"])
            report = self.ask(provider, "review", context)
            owned = findings.owned(state, provider)
            validate_pr_review(report, [f["id"] for f in owned if f["status"] == "open"],
                               [f["id"] for f in owned if f["status"] == "withdrawn"])
            if pr_review_cost(report) > self.pr_review_room():
                raise Blocked("PR review exceeds the checkpoint budget")
            self.record_pr_review(state, provider, report)
            source = ("review_event",) if report["verdict"] == "reopen" else ("reviews", provider)
            conversation.queue(state, None, source=source, kind="review", details={
                "head": state["head"], "round": state["round"] + 1, "pr": state["pr"], "provider": provider,
                "model": self.config["models"][provider] or "CLI default", "pr_url": self.pr_url(state["pr"])})
            if report["verdict"] == "reopen":
                self.reopen_design(report["design_issue"], "pr_review:" + provider)
                return
            self.save()
        if len(state["reviews"]) == len(self.config["reviewers"]):
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

    def protection_errors(self):
        """Evaluate classic protection and every active rule GitHub applies to the base branch."""
        base = self.config["base"]
        try:
            classic = self.gh.repo(f"branches/{base}/protection")
        except NotFound:
            classic = None
        rules = self.gh.pages(f"rules/branches/{base}")
        identifiers = sorted({r.get("ruleset_id") for r in rules if type(r.get("ruleset_id")) is int})
        rulesets = {i: self.gh.repo(f"rulesets/{i}") for i in identifiers}
        return protection_errors(classic, rules, rulesets, self.config["required_checks"])

    def gates(self):
        """Check fresh GitHub evidence and merge only the reviewed SHA, without admin bypass."""
        state = self.state
        pr = self.gh.repo(f"pulls/{state['pr']}")
        files = [f["filename"] for f in self.gh.pages(f"pulls/{state['pr']}/files")]
        checks = self.gh.pages(f"commits/{state['head']}/check-runs?filter=latest", "check_runs")
        reasons = merge_reasons(state, pr, files, checks, self.gh.threads(state["pr"]),
                                self.gh.pages(f"pulls/{state['pr']}/reviews"), self.config)
        if reasons:
            reason = "; ".join(reasons)
            if actions.handoff(state, self.config, reasons, pr) or state.get("reason") != reason:
                state["reason"] = reason
                self.save()
            return False
        # Require server-side protection so a concurrent base/check change cannot bypass gates.
        errors = self.protection_errors()
        if errors:
            raise Blocked("; ".join(errors))
        # Save intent: a lost merge response is reconciled from GitHub on the next tick.
        state["stage"] = "merging"
        self.save()
        response = self.gh.repo(f"pulls/{state['pr']}/merge", "PUT",
                                {"sha": state["head"], "merge_method": "squash"})
        if not response.get("merged"):
            raise Blocked("GitHub rejected the guarded merge")
        state.update(stage="merged", merge_sha=response["sha"], reason="All merge gates passed.",
                     completion={"kind": "controller", "head": state["head"], "merge_sha": response["sha"],
                                 "pr": state["pr"], "gates_verified": True})
        self.save()
        return True

    def observe_merge(self, pr):
        """Record external completion without claiming that controller gates authorized it."""
        state = self.state
        if pr.get("head", {}).get("sha") != state.get("head") or not state.get("head"):
            raise Blocked("merged PR head differs from the exact published controller head")
        if not pr.get("merge_commit_sha"):
            raise Blocked("merged PR lacks completion evidence")
        intent = state["stage"] == "merging" or (state["stage"] == "blocked" and state.get("retry_stage") == "merging")
        actor = (pr.get("merged_by") or {}).get("login")
        controlled = intent and actor == self.identities.author("merge")
        state["completion"] = {"kind": "controller_reconciled" if controlled else "observed_external",
            "head": state["head"], "merge_sha": pr["merge_commit_sha"], "pr": state["pr"],
            "gates_verified": controlled, "observed_stage": state["stage"],
            "merged_by": actor}
        state.update(stage="merged" if controlled else "observed_merged", merge_sha=pr["merge_commit_sha"],
            reason="Controller merge response reconciled." if controlled else
                   "Observed an external merge of the exact published head; controller merge gates are not attested.")
        self.save()

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
        if stage in ("merged", "observed_merged"):
            return Outcome("terminal", progressed)
        if stage in ("blocked", "stopped", "rejected", "escalated"):
            return Outcome("blocked", progressed)
        if stage in ("planned", "clarify"):
            return Outcome("await-human", progressed)
        if progressed:
            return Outcome("continue", True)
        return Outcome("wait-until", retry_after=WAIT_SECONDS)

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
        if self.state and (self.state.get("bundle_id") != self.bundle.identity
                           or self.state.get("charter_id") != digest(self.bundle.charter)
                           or digest(self.state.get("charter")) != self.state.get("charter_id")
                           or self.state.get("execution") != self.bundle.execution_summary):
            raise Blocked("trusted charter, role, skill, schema or profile bundle changed; operator reconciliation required")
        self.sync_stage()
        if (self.stage_sync_warned or self.stage_fallback or self.checkpoints.dirty
                or self.state and (self.state.get("conversation", {}).get("pending") or self.state.get('plan_event_intent')
                                   or self.state.get("pr_outbox"))):
            self.save()
        if self.state and self.state.get("pr") and self.state["stage"] not in ("merged", "observed_merged"):
            pr = self.gh.repo(f"pulls/{self.state['pr']}")
            if pr.get("merged"):
                self.observe_merge(pr)
                return True
        if self.issue["state"] != "open":
            if self.state and self.state["stage"] not in ("merged", "observed_merged", "stopped"):
                self.state.update(stage="stopped", reason="Issue closed without a confirmed exact-head merge.")
                self.save()
                return True
            return False
        if not self.state and self.config["autonomous"]:
            enrollment = self.enrollment()
            if enrollment:
                self.initialize(comments, "autonomous", enrollment=enrollment)
        if self.state and actions.answers(self.state, self.config, comments,
                                          digest([self.issue["title"], self.issue["body"]]), self.answerer):
            self.save()
        self.commands(comments)
        if not self.state or self.state["stage"] in ("planned", "blocked", "stopped", "merged", "observed_merged", "rejected", "escalated"):
            return False
        state = self.state
        if state["stage"] == "merging":
            state["stage"] = "gates"
        if state["config_digest"] != config_digest(self.config):
            raise Blocked("controller configuration changed; re-plan required")
        if state["agent_image"] != os.environ.get("CONCURRO_AGENT_IMAGE", ""):
            raise Blocked("agent image changed; re-plan required")
        if state["execution_image"] != os.environ.get("CONCURRO_EXECUTION_IMAGE", ""):
            raise Blocked("execution image changed; re-plan required")
        if state.get("mode") == "autonomous":
            if not self.refresh_autonomous(comments):
                return False
        elif state["stage"] == "clarify":
            return False
        if state["issue_digest"] != digest([self.issue["title"], self.issue["body"]]):
            raise Blocked("issue requirements changed; re-plan required")
        if self.gh.repo("git/ref/heads/" + self.config["base"])["object"]["sha"] != state["base"]:
            raise Blocked("base moved; re-plan required")
        if state.get("mode") == "manual" and state.get("approval"):
            self.check_human_approval(comments)
        if state["stage"] in ("implement", "publish", "review", "gates") and not approval_valid(state, self.config):
            raise Blocked("approved plan missing or stale")
        if state.get("head") and state["stage"] != "publish":
            pr = self.gh.repo(f"pulls/{state['pr']}")
            if pr["state"] != "open" or pr["head"]["sha"] != state["head"]:
                raise Blocked("PR closed or head changed outside the controller")
        method = {"intake": lambda: self.quality_control("intake"),
                  "plan_qc": lambda: self.quality_control("plan_qc"),
                  "plan": self.plan, "implement": self.implement, "publish": self.publish,
                  "review": self.review, "gates": self.gates}.get(state["stage"])
        if method is None:
            raise Blocked("unknown state")
        result = method()
        return result is not False

    def check_human_approval(self, comments):
        """Retain revocable writer approval for explicitly configured manual mode."""
        state = self.state
        if not self.permitted(state["approval"]["actor"]):
            raise Blocked("plan approver no longer has write access")
        if state.get("approval") and not any(
                c["id"] == state["approval"]["comment"]
                and c["user"]["login"] == state["approval"]["actor"]
                and c["body"].strip() == "/concurro approve " + state["plan_id"] for c in comments):
            raise Blocked("approval comment changed or was removed")


def main():
    """Run from the trusted default branch; schedule scans the opt-in label."""
    config = json.loads(Path(".concurro/config.json").read_text())
    validate_config(config)
    if os.environ.get("GITHUB_REPOSITORY", config["repository"]) != config["repository"]:
        raise Blocked("repository identity mismatch")
    gh = GitHub(config["repository"])
    number = os.environ.get("CONCURRO_ISSUE", "")
    if number:
        if not number.isdecimal() or int(number) <= 0:
            raise Blocked("invalid issue number")
        numbers = [int(number)]
    else:
        numbers = QueueScan(gh, config)
    for number in numbers:
        controller = Controller(gh, Agents(config), config)
        try:
            advanced = controller.advance(number)
            if controller.state:
                sync_checkpoint_health(gh, config, number, failed=False)
            if advanced:
                break
        except Exception as error:
            # Do not publish raw CLI/provider errors: they can contain credentials or injected text.
            if not controller.state:
                # Ownership was never established (or belongs elsewhere): no issue-level writes.
                detail = error.public_detail() if isinstance(error, CheckpointError) else "unverified task state"
                print(f"Concurro diagnostic for issue {number}: {detail}", file=sys.stderr)
            elif isinstance(error, CheckpointError) and not isinstance(error, conversation.PublicationError):
                sync_checkpoint_health(gh, config, number, failed=True, error=error)
            else:
                reason = str(error) if isinstance(error, Blocked) else "Transport or schema failure; inspect runner health."
                try:
                    # The Blocked comment is posted now; a publication failure has the health notice instead.
                    controller.suspend("blocked", reason, outboxes=False,
                                       publish_events=not isinstance(error, conversation.PublicationError))
                except Exception as display_error:
                    diagnostic = display_error if isinstance(display_error, CheckpointError) else CheckpointError("failure_display")
                    sync_checkpoint_health(gh, config, number, failed=True, error=diagnostic)
                else:
                    if isinstance(error, conversation.PublicationError):
                        sync_checkpoint_health(gh, config, number, failed=True, error=error)
            print(f"Concurro stopped safely on issue {number}", file=sys.stderr)
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
