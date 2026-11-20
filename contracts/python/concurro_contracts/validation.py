"""Checked decoding for versioned Concurro wire events."""

import json

from concurro_contracts.github_issues_v1 import Envelope
from concurro_contracts.github_issue_comment_v1 import Envelope as IssueCommentEnvelope
from concurro_contracts.github_pull_request_v1 import Envelope as PullRequestEnvelope
from concurro_contracts.github_pull_request_review_v1 import Envelope as PullRequestReviewEnvelope
from concurro_contracts.github_pull_request_review_comment_v1 import Envelope as PullRequestReviewCommentEnvelope
from concurro_contracts.github_pull_request_review_thread_v1 import Envelope as PullRequestReviewThreadEnvelope
from concurro_contracts.github_push_v1 import Envelope as PushEnvelope
from concurro_contracts.github_status_v1 import Envelope as StatusEnvelope
from concurro_contracts.github_check_run_v1 import Envelope as CheckRunEnvelope
from concurro_contracts.github_check_suite_v1 import Envelope as CheckSuiteEnvelope
from concurro_contracts.github_workflow_run_v1 import Envelope as WorkflowRunEnvelope
from concurro_contracts.ingress_coverage_advanced_v1 import Envelope as AdvancedEnvelope
from concurro_contracts.ingress_coverage_gap_v1 import Envelope as GapEnvelope
from concurro_contracts.request_tasks_v1 import Envelope as RequestOutcomeEnvelope
from concurro_contracts.task_v1 import Envelope as TaskSnapshotEnvelope
from concurro_contracts.task_notice_v1 import Envelope as TaskNoticeEnvelope
from concurro_contracts.task_report_v1 import Envelope as TaskReportEnvelope
from concurro_contracts.turn_submitted_v1 import Envelope as TurnSubmittedEnvelope
from concurro_contracts.turn_status_v1 import Envelope as TurnStatusEnvelope
from concurro_contracts.turn_finished_v1 import Envelope as TurnFinishedEnvelope


class UnsupportedVersion(ValueError):
    """A contract version this consumer must acknowledge and skip."""


def decode_github_issues(raw: bytes) -> Envelope:
    """Read the version first, then validate the full GitHub issues envelope."""
    event = _decode_fact(raw, Envelope, "github.issues")
    if event.type != f"github.issues.{event.data.root.action}":
        raise ValueError(f"event type does not match action: {event.type}")
    return event


def _decode_github(raw: bytes, model, family: str, *, bare: bool = False):
    """Validate one typed GitHub fact and its action-bearing subject."""
    event = _decode_fact(raw, model, family)
    action = event.data.action
    if (action is None) != bare:
        raise ValueError(f"invalid action for {family}")
    expected = family if bare else f"{family}.{action}"
    if event.type != expected:
        raise ValueError(f"event type does not match action: {event.type}")
    return event


def decode_github_issue_comment(raw: bytes):
    """Decode one issue comment fact."""
    return _decode_github(raw, IssueCommentEnvelope, "github.issue_comment")


def decode_github_pull_request(raw: bytes):
    """Decode one pull request fact."""
    return _decode_github(raw, PullRequestEnvelope, "github.pull_request")


def decode_github_pull_request_review(raw: bytes):
    """Decode one pull request review fact."""
    return _decode_github(raw, PullRequestReviewEnvelope, "github.pull_request_review")


def decode_github_pull_request_review_comment(raw: bytes):
    """Decode one pull request review comment fact."""
    return _decode_github(raw, PullRequestReviewCommentEnvelope, "github.pull_request_review_comment")


def decode_github_pull_request_review_thread(raw: bytes):
    """Decode one pull request review thread fact."""
    return _decode_github(raw, PullRequestReviewThreadEnvelope, "github.pull_request_review_thread")


def decode_github_push(raw: bytes):
    """Decode one push fact."""
    return _decode_github(raw, PushEnvelope, "github.push", bare=True)


def decode_github_status(raw: bytes):
    """Decode one commit status fact."""
    return _decode_github(raw, StatusEnvelope, "github.status", bare=True)


def decode_github_check_run(raw: bytes):
    """Decode one check run fact."""
    return _decode_github(raw, CheckRunEnvelope, "github.check_run")


def decode_github_check_suite(raw: bytes):
    """Decode one check suite fact."""
    return _decode_github(raw, CheckSuiteEnvelope, "github.check_suite")


def decode_github_workflow_run(raw: bytes):
    """Decode one workflow run fact."""
    return _decode_github(raw, WorkflowRunEnvelope, "github.workflow_run")


def decode_coverage_advanced(raw: bytes) -> AdvancedEnvelope:
    """Read and validate one coverage advancement fact."""
    return _decode_fact(raw, AdvancedEnvelope, "ingress.coverage.advanced")


def decode_coverage_gap(raw: bytes) -> GapEnvelope:
    """Read and validate one coverage gap fact."""
    return _decode_fact(raw, GapEnvelope, "ingress.coverage.gap")


def decode_request_outcome(raw: bytes) -> RequestOutcomeEnvelope:
    """Validate a task request outcome and its matching disposition subject."""
    event = _decode_fact(raw, RequestOutcomeEnvelope, "request.tasks")
    if event.type != f"request.tasks.{event.data.root.status.value}":
        raise ValueError(f"event type does not match request outcome: {event.type}")
    return event


def decode_task_snapshot(raw: bytes) -> TaskSnapshotEnvelope:
    """Validate task-scoped state and its snapshot revision metadata."""
    event = _decode_envelope(raw, TaskSnapshotEnvelope, "task")
    task_id = str(event.data.task_id.root)
    if event.type != f"task.{task_id}.state":
        raise ValueError(f"event type does not match task: {event.type}")
    if event.entity != f"task:{task_id}" or event.revision is None or event.revision < 1:
        raise ValueError("task snapshot metadata is invalid")
    return event


def decode_task_notice(raw: bytes) -> TaskNoticeEnvelope:
    """Validate a notice snapshot and its task and notice identities."""
    event = _decode_envelope(raw, TaskNoticeEnvelope, "task.notice")
    task_id = str(event.data.task_id.root)
    notice_id = str(event.data.notice_id.root)
    if event.type != f"task.{task_id}.notice":
        raise ValueError(f"event type does not match task: {event.type}")
    if event.entity != f"task:{task_id}:notice:{notice_id}" or not event.revision:
        raise ValueError("task notice metadata is invalid")
    return event


def decode_task_report(raw: bytes) -> TaskReportEnvelope:
    """Validate a report snapshot and its task and report identities."""
    event = _decode_envelope(raw, TaskReportEnvelope, "task.report")
    task_id = str(event.data.task_id.root)
    report_id = str(event.data.report_id.root)
    if event.type != f"task.{task_id}.report":
        raise ValueError(f"event type does not match task: {event.type}")
    if event.entity != f"task:{task_id}:report:{report_id}" or not event.revision:
        raise ValueError("task report metadata is invalid")
    return event


def decode_turn_submitted(raw: bytes) -> TurnSubmittedEnvelope:
    """Validate one submitted turn fact and its subject."""
    event = _decode_fact(raw, TurnSubmittedEnvelope, "turn.submitted")
    if event.type != f"turn.{event.data.turn_id.root}.submitted":
        raise ValueError(f"event type does not match turn: {event.type}")
    return event


def decode_turn_status(raw: bytes) -> TurnStatusEnvelope:
    """Validate one turn lifecycle snapshot and its revision."""
    event = _decode_envelope(raw, TurnStatusEnvelope, "turn.status")
    turn_id = str(event.data.turn_id.root)
    if event.type != f"turn.{turn_id}.status":
        raise ValueError(f"event type does not match turn: {event.type}")
    if event.entity != f"turn:{turn_id}" or not event.revision:
        raise ValueError("turn status metadata is invalid")
    return event


def decode_turn_finished(raw: bytes) -> TurnFinishedEnvelope:
    """Validate one finished turn fact and its subject."""
    event = _decode_fact(raw, TurnFinishedEnvelope, "turn.finished")
    if event.type != f"turn.{event.data.turn_id.root}.finished":
        raise ValueError(f"event type does not match turn: {event.type}")
    return event


def _decode_fact(raw: bytes, model, family):
    """Check the version before decoding one typed fact envelope."""
    event = _decode_envelope(raw, model, family)
    if event.entity is not None or event.revision is not None:
        raise ValueError("fact has snapshot metadata")
    return event


def _decode_envelope(raw: bytes, model, family):
    """Check the contract version before decoding its typed envelope."""
    header = json.loads(raw)
    if not isinstance(header, dict):
        raise ValueError("event envelope must be an object")
    version = header.get("version")
    if type(version) is not int or not 0 <= version <= 2**64 - 1:
        raise ValueError(f"invalid {family} version: {version!r}")
    if version != 1:
        raise UnsupportedVersion(f"unsupported {family} version: {version}")
    return model.model_validate_json(raw, strict=True)
