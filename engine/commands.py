"""Parse writer commands and apply their lifecycle transitions."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum, StrEnum, auto
from typing import TYPE_CHECKING

import actions
import conversation
import dashboard_runs
import implementation_bundle
import protocol
import sessions
from actions import BUNDLE_REASON
from markup import code, link
from policy import Blocked, approval_binding, approval_valid, consensus_valid, digest
from vocab import JUDGED_STAGES, SUSPENDED_STAGES

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from records import Comment, Config, TaskState


class CommandKind(StrEnum):
    """Supported writer command kinds; revision grants are distinct from retries."""

    START = "start"
    APPROVE = "approve"
    STOP = "stop"
    RETRY = "retry"
    EXTEND = "extend"
    INVALIDATE = "invalidate"


@dataclass(frozen=True)
class Command:
    """A validated command kind and its optional plan ID or revision count."""

    kind: CommandKind
    argument: str | int | None = None


class LoopAction(Enum):
    """Control flow returned to the controller's ordered comment loop."""

    CONTINUE = auto()
    RETURN_TRUE = auto()
    FALL_THROUGH = auto()


def parse(body: str) -> Command | None:
    """Parse only the existing exact command grammar, ignoring surrounding whitespace."""
    body = body.strip()
    if not re.fullmatch(
        r"/concurro (start|approve "
        + protocol.DIGEST_PATTERN
        + r"|stop|retry(?: --plan-revisions [0-9]{1,3})?|invalidate)",
        body,
    ):
        return None
    parts = body.split()
    if len(parts) == 4:
        return Command(CommandKind.EXTEND, int(parts[-1]))
    return Command(CommandKind(parts[1]), parts[2] if len(parts) == 3 else None)


def output_repair_pending(state: TaskState) -> object:
    """Identify a correction retry that must retain its existing assessment stage."""
    return (
        (
            state.get("review_repair")
            or any(m.get("review_repair") for m in state.get("review_batch", {}).get("members", {}).values())
        )
        and state["retry_stage"] == "plan_qc"
        or state.get("plan_draft")
        and state["retry_stage"] == "plan"
        or state.get("intake_draft")
        and state["retry_stage"] == "intake"
    )


class CommandHandler:
    """Apply commands through controller operations without owning persistence or state."""

    def __init__(self, controller: Any, config_digest: Callable[[Config], str]) -> None:
        """Keep controller hooks and its configuration-binding function available to handlers."""
        self.controller = controller
        self.config_digest = config_digest

    def dispatch(self, command: Command, comment: Comment, comments: list[Comment]) -> LoopAction:
        """Dispatch one already-authorized command and return its loop action."""
        handlers = {
            CommandKind.START: self.start,
            CommandKind.APPROVE: self.approve,
            CommandKind.STOP: self.stop,
            CommandKind.RETRY: self.retry,
            CommandKind.EXTEND: self.extend,
            CommandKind.INVALIDATE: self.invalidate,
        }
        return handlers[command.kind](command, comment, comments)

    def start(self, command: Command, comment: Comment, comments: list[Comment]) -> LoopAction:
        """Start a manual assessment or consume a refusal without changing the pause."""
        controller = self.controller
        if controller.state and controller.waiting_revision():
            controller.refuse_revision_command(comment, exhausted=True)
            return LoopAction.CONTINUE
        if controller.config["autonomous"]:
            return LoopAction.CONTINUE
        if controller.state and controller.state["stage"] not in ("clarify", "blocked", "stopped"):
            return LoopAction.CONTINUE
        if (
            controller.state
            and controller.state["stage"] == "clarify"
            and controller.state.get("head")
            and not controller.state.get("pending")
        ):
            # A clarification with a PR resumes its open design, as autonomous refresh does.
            controller.state["command_id"] = comment["id"]
            controller.resume_with_head(controller.requirements(comments))
            return LoopAction.CONTINUE
        if controller.state and any(controller.state.get(key) for key in ("pr", "head", "pending")):
            controller.state["command_id"] = comment["id"]
            notice = code("/concurro start") + " cannot restart a task with an open PR or prepared publication. "
            if controller.state.get("pending"):
                notice += "Use " + code("/concurro retry") + " to finish the pending publication."
            else:
                notice += (
                    "Use "
                    + code("/concurro retry")
                    + " to resume, or "
                    + code("/concurro invalidate")
                    + " to reassess."
                )
            conversation.queue(controller.state, "Start command not applied", notice)
            controller.save()
            return LoopAction.CONTINUE
        if controller.state:
            controller.state["command_id"] = comment["id"]
            if not controller.revise_plan("intake", "Writer requested reassessment.", save_exhausted=False):
                controller.refuse_revision_command(comment, exhausted=True)
                return LoopAction.CONTINUE
            base = controller.pulls.repo("git/ref/heads/" + controller.config["base"])["object"]["sha"]
            controller.state.update(
                base=base,
                requirements=controller.requirements(comments),
                command_id=comment["id"],
                issue_digest=controller.issue_digest(),
            )
            for key in ("plan", "plan_id", "branch", "qc", "design_parent", "approved_accumulated_files"):
                controller.state.pop(key, None)
            controller.save()
        else:
            controller.initialize([c for c in comments if c["id"] <= comment["id"]], "manual", comment["id"])
        return LoopAction.FALL_THROUGH

    def approve(self, command: Command, comment: Comment, comments: list[Comment]) -> LoopAction:
        """Bind an unchanged manual plan to a writer approval."""
        controller = self.controller
        if (
            controller.state
            and controller.state["stage"] == "planned"
            and controller.state.get("mode", "manual") == "manual"
        ):
            if command.argument == controller.state["plan_id"]:
                if not consensus_valid(controller.state, controller.config):
                    raise Blocked("writer command cannot waive design consensus")
                controller.state.update(
                    stage="implement",
                    command_id=comment["id"],
                    approval={
                        "kind": "human",
                        "binding": approval_binding(controller.state, controller.config),
                        "plan": controller.state["plan_id"],
                        "actor": comment["user"]["login"],
                        "comment": comment["id"],
                    },
                )
                controller.save()
        return LoopAction.FALL_THROUGH

    def invalidate(self, command: Command, comment: Comment, comments: list[Comment]) -> LoopAction:
        """Restart assessment explicitly while retaining spent budgets."""
        controller = self.controller
        if controller.state:
            controller.invalidate_task(comment, comments)
            return LoopAction.RETURN_TRUE
        return LoopAction.FALL_THROUGH

    def retry(self, command: Command, comment: Comment, comments: list[Comment]) -> LoopAction:
        """Resume the applicable paused stage or request a bounded reviewed repair."""
        controller = self.controller
        if controller.state and controller.state["stage"] == "gates":
            controller.retry_reviewed(comment, comments)
        elif (
            controller.state
            and controller.state["stage"] in SUSPENDED_STAGES | JUDGED_STAGES
            and controller.state.get("retry_stage")
        ):
            if controller.state["stage"] in SUSPENDED_STAGES and controller.state["retry_stage"] == "implement":
                controller.retry_implementation_bundle(comment, comments)
            output_repair = output_repair_pending(controller.state)
            waiting = controller.waiting_revision()
            if waiting:
                controller.refuse_revision_command(comment, exhausted=True)
                return LoopAction.CONTINUE
            if controller.state["stage"] in JUDGED_STAGES and not output_repair:
                controller.state["command_id"] = comment["id"]
                if not controller.revise_plan(
                    "intake", "Writer requested reassessment after terminal judgment.", save_exhausted=False
                ):
                    controller.refuse_revision_command(comment, exhausted=True)
                    return LoopAction.CONTINUE
                # Reassessment reads the issue as it is now, including edits made to answer the escalation.
                controller.state.update(
                    requirements=controller.requirements(comments), issue_digest=controller.issue_digest()
                )
                controller.state.pop("retry_stage", None)
                controller.state["command_id"] = comment["id"]
            else:
                controller.state.update(stage=controller.state.pop("retry_stage"), command_id=comment["id"])
            controller.state.pop("reason", None)
            if controller.state.get("plan_draft") and controller.state["stage"] == "plan":
                controller.state["plan_draft"]["attempt"] = 0
            if controller.state.get("intake_draft") and controller.state["stage"] == "intake":
                controller.state["intake_draft"]["attempt"] = 0
            if controller.state.get("review_repair"):
                controller.state["review_repair"].pop("attempts", None)
                controller.state["review_repair"].pop("signature", None)
            # A writer renews allowances, never spent budgets or eligibility.
            for member in controller.state.get("review_batch", {}).get("members", {}).values():
                repair = member.get("review_repair", {})
                repair.pop("attempts", None)
                repair.pop("signature", None)
                member["failures"] = 0
                if member.get("operation", {}).get("status") not in ("completed", "consumed"):
                    member.pop("operation", None)
            controller.state.pop("operation", None)
            controller.save()
        return LoopAction.FALL_THROUGH

    def extend(self, command: Command, comment: Comment, comments: list[Comment]) -> LoopAction:
        """Grant revision capacity before processing any later command."""
        controller = self.controller
        if controller.state:
            if controller.extend_plan_revisions(comment, command.argument, comments):
                return LoopAction.RETURN_TRUE
            return LoopAction.CONTINUE
        return LoopAction.FALL_THROUGH

    def stop(self, command: Command, comment: Comment, comments: list[Comment]) -> LoopAction:
        """Consume a stop command before suspending work."""
        controller = self.controller
        if controller.state:
            controller.state["command_id"] = comment["id"]
            controller.suspend("stopped")
        return LoopAction.FALL_THROUGH

    def refuse_revision_command(self, comment: Comment, exhausted: bool = False) -> None:
        """Consume an inapplicable writer command with one durable notice, without suspending work."""
        controller = self.controller
        state: TaskState = controller.state
        body = "No exhausted plan revision is waiting; no capacity was granted."
        if exhausted:
            state.setdefault("revision_wait", {"stage": controller.waiting_revision()})
            body = (
                "The plan revision allowance is exhausted. This command cannot increase it. "
                "A writer can grant 1–3 additional revisions, for example "
                + code("/concurro retry --plan-revisions 1")
                + ". Findings and spent budgets are preserved."
            )
        state["command_id"] = comment["id"]
        conversation.queue(state, "Plan revision command not applied", body)
        controller.save()

    def extend_plan_revisions(self, comment: Comment, amount: int, comments: list[Comment]) -> bool:
        """Atomically grant bounded revision capacity and resume the recorded revision, never waive findings."""
        controller = self.controller
        state: TaskState = controller.state
        stage = controller.waiting_revision()
        if (
            state["stage"] not in ("blocked", "stopped", "escalated")
            or stage is None
            or state["plan_revisions"] < controller.plan_revision_limit()
        ):
            controller.refuse_revision_command(comment)
            return False
        state.setdefault("revision_wait", {"stage": stage})
        with controller.consume_on_refusal(comment):
            if not 1 <= amount <= 3:
                raise Blocked("Grant between 1 and 3 additional plan revisions per command.")
            if state.get("pending"):
                raise Blocked("Publication is pending; reconcile it before granting revision capacity.")
            if state["config_digest"] != self.config_digest(controller.config) or state[
                "requirements"
            ] != controller.requirements(comments):
                raise Blocked("Configuration or requirements changed; reassess them before granting revision capacity.")
            controller.require_current_bundle()
            if state.get("pr"):
                controller.require_pr_unchanged("PR changed outside the controller; cannot resume its plan revision.")
        previous = controller.plan_revision_limit()
        state["plan_revision_extra"] = state.get("plan_revision_extra", 0) + amount
        state["plan_revision_grant"] = {
            "actor": comment["user"]["login"],
            "comment": comment["id"],
            "added": amount,
            "limit": controller.plan_revision_limit(),
        }
        state["command_id"] = comment["id"]
        # The failed revision already archived votes and invalidated approval. Do not do it twice.
        controller.revise_plan(stage, "Writer granted additional plan revision capacity.", invalidate=False)
        for key in ("operation", "retry_stage", "reason"):
            state.pop(key, None)
        conversation.queue(
            state,
            "Plan revision capacity extended",
            code(comment["user"]["login"])
            + f" granted {amount} additional plan revisions in "
            + link(
                "this command",
                protocol.comment_url(controller.config["repository"], state["issue"], comment["id"]),
            )
            + f". The total allowance is now {controller.plan_revision_limit()} (previously {previous}). "
            "The pending revision is resuming with all findings and spent budgets preserved. "
            "The revised plan still requires independent approval; the model-call limit is unchanged.",
        )
        controller.save()
        return True

    def retry_reviewed(self, comment: Comment, comments: list[Comment]) -> None:
        """A writer may request a bounded repair with fresh feedback; CI events alone never do."""
        controller = self.controller
        state: TaskState = controller.state
        if state["issue_digest"] != controller.issue_digest():
            state["command_id"] = comment["id"]
            controller.resume_with_head(controller.requirements(comments))
            return
        previous = state["requirements"].get("discussion", [])
        writer_feedback = [
            c
            for c in comments
            if state.get("command_id", 0) < c["id"] < comment["id"]
            and not c["body"].strip().startswith("/concurro ")
            and not controller.identities.ours(c["user"]["login"])
            and controller.permitted(c["user"]["login"])
            and {"author": c["user"]["login"], "body": c["body"]} not in previous
        ]
        if writer_feedback:
            latest = max(writer_feedback, key=lambda c: c["id"])
            if len(latest["body"].encode()) > 4000:
                raise Blocked("repair feedback exceeds 4000 bytes; provide a shorter writer comment")
            if state["round"] >= controller.config["max_fix_rounds"]:
                raise Blocked("writer-requested repair allowance exhausted")
            state.update(
                round=state["round"] + 1,
                stage="implement",
                handoff_ready=False,
                input_revision=state.get("input_revision", 0) + 1,
            )
            key = f"{state['design_generation']}:{state['round']}"
            state.setdefault("implementation_feedback", {})[key] = (
                "Writer-supplied feedback (untrusted evidence, not a change to the approved scope):\n" + latest["body"]
            )
            state.pop("reason", None)
        state["command_id"] = comment["id"]
        controller.save()

    def retry_implementation_bundle(self, comment: Comment, comments: list[Comment]) -> None:
        """Adopt only proven implementer-skill changes on an authorized implementation retry."""
        controller = self.controller
        state: TaskState = controller.state
        if implementation_bundle.current(state, controller.bundle):
            return
        with controller.consume_on_refusal(comment):
            if state.get("pending"):
                raise Blocked("Publication is pending; finish the recorded publication before adopting instructions.")
            if (
                state["config_digest"] != self.config_digest(controller.config)
                or state["requirements"] != controller.requirements(comments)
                or not approval_valid(state, controller.config)
            ):
                raise Blocked("Configuration, requirements or approval changed; cannot preserve the approved plan.")
            if state.get("pr"):
                controller.require_pr_unchanged("PR changed outside the controller; cannot retry its implementation.")
            original = implementation_bundle.original_skill(state, controller.bundle)
            if original is None or state.get("charter_id") != digest(controller.bundle.charter):
                raise Blocked(BUNDLE_REASON)
        previous = state.get("implementation_bundle", {}).get("new_bundle", state["bundle_id"])
        state["implementation_skill"] = original
        state["implementation_bundle"] = {
            "approved_bundle": state["bundle_id"],
            "old_bundle": previous,
            "new_bundle": controller.bundle.identity,
            "actor": comment["user"]["login"],
            "comment": comment["id"],
            "plan": state["plan_id"],
            "old_skill": (state.get("implementation_bundle") or {}).get("new_skill", original),
            "new_skill": controller.bundle.hashes[implementation_bundle.SKILL],
        }
        for role, session in list(state.get("sessions", {}).items()):
            if role.startswith("develop:"):
                sessions.restart(state, {**session, "role": role})
        conversation.queue(
            state,
            "Implementation instructions updated",
            "A writer requested an implementation retry. The approved plan and design reviews are unchanged. "
            "Only the implementer skill changed; implementation starts in a new session. Spent budgets are retained.\n\n"
            + "Bundle: "
            + code(previous)
            + " → "
            + code(controller.bundle.identity)
            + ".",
        )

    def invalidate_task(self, comment: Comment, comments: list[Comment]) -> None:
        """Explicitly adopt current governance and restart assessment without resetting spent budgets."""
        controller = self.controller
        state: TaskState = controller.state
        with controller.consume_on_refusal(comment, save=False):
            if state.get("pending"):
                raise Blocked(
                    "Publication is pending. Use /concurro retry to finish the recorded publication, "
                    "then /concurro invalidate to reassess it."
                )
            if state["config_digest"] != self.config_digest(controller.config):
                raise Blocked("Controller configuration changed; start a new task with the new configuration.")
            if state.get("pr"):
                controller.require_pr_unchanged(
                    "PR changed outside the controller; cannot invalidate its recorded design"
                )
                state["changed_files"] = controller.reconciled_changed_paths()
        record = {
            "actor": comment["user"]["login"],
            "comment": comment["id"],
            "old_bundle": state["bundle_id"],
            "new_bundle": controller.bundle.identity,
            "old_charter": state["charter_id"],
            "new_charter": digest(controller.bundle.charter),
            "head": state.get("head"),
            "plan": state.get("plan_id"),
            "display_start": dashboard_runs.start(state),
        }
        if state.get("operation"):
            record["operation_digest"] = digest(state["operation"])
        state.setdefault("invalidations", []).append(record)
        for request in state.get("requests", []):
            if request["state"] == "open":
                request["state"] = "superseded"
                actions.edit(request, "↪️ **Superseded**: a writer requested a new assessment.")
        controller.invalidate_design(dashboard_runs.INVALIDATED)
        if state.get("reviews"):
            state.setdefault("review_history", []).append(state["reviews"])
        state.update(
            bundle_id=controller.bundle.identity,
            implementation_skill=controller.bundle.hashes[implementation_bundle.SKILL],
            charter_id=digest(controller.bundle.charter),
            charter=deepcopy(controller.bundle.charter),
            stage="intake",
            command_id=comment["id"],
            requirements=controller.requirements(comments),
            issue_digest=controller.issue_digest(),
            sessions={},
            reviews={},
            qc={},
            objections=[],
            handoff_ready=False,
            session_epoch=state.get("session_epoch", 0) + 1,
        )
        for key in (
            "revision_wait",
            "implementation_bundle",
            "review_batch",
            "plan",
            "plan_id",
            "operation",
            "retry_stage",
            "reason",
            "design_feedback",
            "candidate",
            "local_status",
            "approved_accumulated_files",
            "design_parent",
            "classification_snapshot",
            "design_eligibility",
        ):
            state.pop(key, None)
        if state.get("pr"):
            conversation.queue(
                state,
                "Reassessment started",
                link(f"PR #{state['pr']}", controller.pr_url(state["pr"]))
                + " is being reassessed at a writer's request. Earlier Concurro approvals no longer apply. "
                "Please do not merge until reassessment is complete. The PR's draft/ready status is unchanged.",
            )
        controller.save()

    def check_human_approval(self, comments: list[Comment]) -> None:
        """Retain revocable writer approval for explicitly configured manual mode."""
        controller = self.controller
        state: TaskState = controller.state
        if not controller.permitted(state["approval"]["actor"]):
            raise Blocked("plan approver no longer has write access")
        if not any(
            c["id"] == state["approval"]["comment"]
            and c["user"]["login"] == state["approval"]["actor"]
            and c["body"].strip() == "/concurro approve " + state["plan_id"]
            for c in comments
        ):
            raise Blocked("approval comment changed or was removed")
