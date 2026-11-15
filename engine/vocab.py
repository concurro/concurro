"""Wire-compatible names for controller stages and native turn results."""

from enum import StrEnum


class Stage(StrEnum):
    """Durable controller stages; incoming strings remain uncoerced."""

    INTAKE = "intake"
    CLARIFY = "clarify"
    PLAN = "plan"
    PLAN_QC = "plan_qc"
    PLANNED = "planned"
    IMPLEMENT = "implement"
    PUBLISH = "publish"
    REVIEW = "review"
    # Standalone review rounds only.
    QUEUED = "queued"
    PUBLISHING = "publishing"
    COMPLETE = "complete"
    GATES = "gates"
    MERGING = "merging"
    MERGED = "merged"
    OBSERVED_MERGED = "observed_merged"
    CLOSED = "closed"
    AWAITING_CREDENTIALS = "awaiting_credentials"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BLOCKED = "blocked"
    STOPPED = "stopped"
    REJECTED = "rejected"
    ESCALATED = "escalated"


class TurnOutcome(StrEnum):
    """Native turn results, separate from scheduler outcomes and exchange statuses."""

    OK = "ok"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    LOST = "lost"
    TIMED_OUT = "timed_out"
    TERMINATED = "terminated"
    ADMISSION_UNKNOWN = "admission_unknown"
    QUEUE_EXPIRED = "queue_expired"


# Confirmed merge completion, including merges observed outside the controller.
MERGED_STAGES: frozenset[Stage] = frozenset((Stage.MERGED, Stage.OBSERVED_MERGED))
# PR lifecycle ended; closing without merging must not imply successful completion.
TERMINAL_STAGES: frozenset[Stage] = MERGED_STAGES | {Stage.CLOSED}
# Pauses that preserve the stage to resume, rather than requiring reassessment.
SUSPENDED_STAGES: frozenset[Stage] = frozenset(
    (Stage.BLOCKED, Stage.STOPPED, Stage.AWAITING_CREDENTIALS, Stage.BUDGET_EXHAUSTED)
)
# Assessment decisions that require reassessment unless correcting rejected output.
JUDGED_STAGES: frozenset[Stage] = frozenset((Stage.REJECTED, Stage.ESCALATED))

# Task kinds select a lifecycle; a task without a recorded kind is a contribution.
CONTRIBUTION = "contribution"
STANDALONE_REVIEW = "standalone_pr_review"
TASK_KINDS = (CONTRIBUTION, STANDALONE_REVIEW)
