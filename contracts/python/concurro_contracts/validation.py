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


def _decode_fact(raw: bytes, model, family):
    """Check the version before decoding one typed fact envelope."""
    header = json.loads(raw)
    if not isinstance(header, dict):
        raise ValueError("event envelope must be an object")
    version = header.get("version")
    if type(version) is not int or not 0 <= version <= 2**64 - 1:
        raise ValueError(f"invalid {family} version: {version!r}")
    if version != 1:
        raise UnsupportedVersion(f"unsupported {family} version: {version}")
    event = model.model_validate_json(raw, strict=True)
    if event.entity is not None or event.revision is not None:
        raise ValueError("fact has snapshot metadata")
    return event
