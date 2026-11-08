"""Durable independence receipts for cooperative design assessments."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import classification
import feedback
from policy import digest

if TYPE_CHECKING:
    from records import Document, EligibilityIdentity, EligibilityReceipt, Operation, Session, TaskState


CORRECTION_INSTRUCTION = (
    "Correct the report against its schema and the validation feedback. Reconsider the "
    "verdict if necessary; approval is never required. Preserve genuine concerns and "
    "unanswered questions, and report actionable objections when the evidence warrants "
    "them. This feedback concerns report validity, not a request to approve the design."
)
# Dispatch metadata and controller-only corrections do not change the assessment baseline.
INCIDENTAL = {"review_validation_feedback", "review_output_budget", "repository", "repository_commit", "agent"}
DISCUSSION = {
    "own_objections",
    "design_feedback",
    "design_votes",
    "qc_feedback",
    "peer_judgments",
    "writer_feedback",
    "implementation_feedback",
    "prior_reviews",
}


def baseline(context: Document) -> str:
    """Commit to full review inputs, including feedback materialized as runner files."""
    restored = classification.context_core(feedback.restore(context))
    return digest({k: v for k, v in restored.items() if k not in INCIDENTAL})


def discussion(context: Document) -> bool:
    """Detect negotiation in addition to requirements present at initial assessment."""
    context = feedback.restore(context)
    return bool(context.get("plan", {}).get("responses") or any(context.get(k) for k in DISCUSSION))


def identity(context: Document, session: Session) -> EligibilityIdentity:
    """Bind a receipt to the reviewer, exact design and retained transcript scope."""
    return {
        "binding": context["design_binding"],
        "reviewer": context["reviewer_identity"]["id"],
        "session": digest([session["role"], session["scope"]]),
    }


def prepare(state: TaskState, context: Document, session: Session | None, operation: Operation) -> None:
    """Record eligibility before dispatch; retry may erase the operation, never this receipt."""
    if context.get("review_phase") != "cooperative" or not session:
        return
    bound = identity(context, session)
    records = state.setdefault("design_eligibility", {})
    previous = records.get(bound["reviewer"])
    same = previous is not None and previous.get("binding") == bound["binding"]
    inputs = baseline(context)
    if same:
        # `same` proves this receipt exists; the checker does not track that boolean alias.
        receipt = cast("EligibilityReceipt", previous)
        reason = (
            "session_changed"
            if receipt.get("session") != bound["session"]
            else "context_changed"
            if receipt.get("inputs") != inputs
            else ""
        )
    else:
        reason = (
            "missing_provenance"
            if operation["status"] != "new"
            else "prior_session"
            if session.get("turn") != 0
            or session.get("completed")
            or previous is not None
            and previous.get("session") == bound["session"]
            else ""
        )
        receipt = records[bound["reviewer"]] = {
            **bound,
            "inputs": inputs,
            "eligible": not bool(reason),
            "reason": reason,
        }
    reason = "session_restart" if session.get("restarts") != 0 else "discussion" if discussion(context) else reason
    if reason and receipt.get("eligible") is True:
        receipt.update(eligible=False, reason=reason)


def eligible(state: TaskState, context: Document, session: Session | None) -> bool:
    """Verify an admitted assessment again when recording its vote, without creating authority."""
    if context.get("review_phase") != "cooperative" or not session:
        return False
    bound = identity(context, session)
    receipt = state.get("design_eligibility", {}).get(bound["reviewer"], {})
    return (
        receipt.get("eligible") is True
        and all(receipt.get(k) == v for k, v in bound.items())
        and receipt.get("inputs") == baseline(context)
        and session.get("restarts") == 0
        and not discussion(context)
    )


def restart(state: TaskState, session: Session) -> None:
    """Make uncertainty sticky immediately, including when a failed turn is checkpointed."""
    key = digest([session["role"], session["scope"]])
    for receipt in state.get("design_eligibility", {}).values():
        if receipt.get("session") == key and receipt.get("eligible") is True:
            receipt.update(eligible=False, reason="session_restart")
