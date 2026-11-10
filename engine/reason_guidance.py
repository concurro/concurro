"""Explicit human actions for controller-generated failure and readiness reasons."""

PREPARED_PUBLICATION = "prepared publication must be reconciled before reopening design"
ACCUMULATED_CHANGES = "cannot reconcile accumulated PR changes"
REVIEW_PUBLICATION_FAILED = "PR review publication failed repeatedly; inspect the PR and its review threads"
UNCERTAIN_MODEL = "model call outcome was uncertain twice; operator reconciliation required"
REVIEW_CONFLICT = "PR review publication differs from its recorded content; reconcile manually"

LEGACY_BUNDLE = "trusted charter, role, skill, schema or profile bundle changed; operator reconciliation required"
DUPLICATE_REVIEW = "duplicate PR review publication; reconcile manually"
UNIDENTIFIED_REVIEW = "Unidentified pending review; operator reconciliation required"
UNCONFIRMED_REVIEW = "Review submission is unconfirmed; operator reconciliation required. Same-ID retry is not enabled."
CLOSED_BEFORE_REVIEW = "PR closed before review publication; operator reconciliation required"
CHANGED_PUBLICATION = "approved base or prepared publication changed; reconcile manually"
CLOSED_OR_DUPLICATE_PR = "PR was closed or duplicated; reconcile manually"
MISSING_PR = "recorded PR disappeared; reconcile manually"
REQUEST_CONFLICT = "Concurro request comment differs from its recorded content; reconcile manually"
RETURNED_TO_DRAFT = "PR returned to draft; a human must mark it ready"
CLOSED_PR = "PR is closed"
UNRESOLVED_THREADS = "unresolved review threads"
CHANGED_HEAD = "head changed outside the controller"
CHANGED_PR_IDENTITY = "PR identity differs from the controller publication"
CHANGED_BASE_BRANCH = "PR targets a different base branch"
CHANGED_FILES = "PR file set differs from controller evidence"
UNAPPROVED_PATH = "unapproved path"
CLOSED_OR_CHANGED_HEAD = "PR closed or head changed outside the controller"
FOREIGN_PR_AUTHOR = "PR is not authored by the controller App"
CHANGED_BRANCH = "branch moved outside the controller"
CHANGED_IMPLEMENTATION_BRANCH = "implementation branch moved outside the controller"
MISSING_IMPLEMENTATION_BRANCH = "implementation branch disappeared"
CLOSED_BEFORE_REVIEW_BUILD = "PR closed before review publication"

OPERATOR_ACTION = "An operator must reconcile the recorded state before work can continue."
OPERATOR_REASONS = {
    CLOSED_OR_CHANGED_HEAD: "The PR closed or its head changed outside Concurro.",
    FOREIGN_PR_AUTHOR: "The PR is not authored by the controller App.",
    CHANGED_BRANCH: "The publication branch moved outside Concurro.",
    CHANGED_IMPLEMENTATION_BRANCH: "The implementation branch moved outside Concurro.",
    MISSING_IMPLEMENTATION_BRANCH: "The implementation branch disappeared.",
    CLOSED_BEFORE_REVIEW_BUILD: "The PR closed before its review was published.",
    PREPARED_PUBLICATION: "A prepared publication prevents reopening the design.",
    ACCUMULATED_CHANGES: "Concurro cannot account for the accumulated PR changes.",
    REVIEW_PUBLICATION_FAILED: "Concurro could not finish publishing the PR review.",
    UNCERTAIN_MODEL: "A model call's outcome could not be confirmed.",
    LEGACY_BUNDLE: "The trusted bundle has changed.",
    REVIEW_CONFLICT: "Published review content differs from Concurro's record.",
    DUPLICATE_REVIEW: "More than one review matches the same publication.",
    UNIDENTIFIED_REVIEW: "The App has a pending review Concurro cannot identify.",
    UNCONFIRMED_REVIEW: "Concurro cannot confirm the review submission.",
    CLOSED_BEFORE_REVIEW: "The PR closed before its review was published.",
    CHANGED_PUBLICATION: "The approved publication inputs have changed.",
    CLOSED_OR_DUPLICATE_PR: "The recorded PR is closed or duplicated.",
    MISSING_PR: "Concurro cannot find the recorded PR.",
    REQUEST_CONFLICT: "A published request differs from Concurro's record.",
}
GATE_ACTIONS = {
    RETURNED_TO_DRAFT: "A human must mark the PR ready when it is ready for review.",
    CLOSED_PR: "A maintainer must decide whether to reopen the PR or stop this task.",
    UNRESOLVED_THREADS: "Resolve the remaining review threads on the PR.",
    CHANGED_HEAD: OPERATOR_ACTION,
    CHANGED_PR_IDENTITY: OPERATOR_ACTION,
    CHANGED_BASE_BRANCH: OPERATOR_ACTION,
    CHANGED_FILES: OPERATOR_ACTION,
    UNAPPROVED_PATH: OPERATOR_ACTION,
}
CI_FAILURES = (
    "CI suite unsuccessful: ",
    "CI workflow unsuccessful: ",
    "CI unsuccessful: ",
    "CI failure: ",
    "CI error: ",
    "CI unknown: ",
)


def operator_summary(reason, stage="blocked"):
    """Match complete engine reasons, excluding model-written escalation/rejection prose."""
    return OPERATOR_REASONS.get(reason) if stage == "blocked" else None


def gate_action(reason):
    """Classify engine gate reasons, never arbitrary substrings in a model rationale."""
    for known, action in GATE_ACTIONS.items():
        if reason == known or reason.startswith(known + "; ") or ("; " + known) in reason:
            return action
    if any(part.startswith(CI_FAILURES) for part in reason.split("; ")):
        return "Inspect the failed CI results, fix the cause, and rerun the checks."
    return None
