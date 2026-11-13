"""Checked records at the extracted engine boundaries, not a full checkpoint schema.

Model documents and transport payloads remain opaque until their existing validators run.
Persisted vocabulary stays string-valued so legacy/unknown values retain their runtime paths.
"""

from __future__ import annotations

from os import PathLike
from typing import Any, Protocol, TypedDict

# Validated elsewhere; this iteration does not duplicate model or GitHub schemas.
Document = dict[str, Any]


class Config(TypedDict, total=False):
    """Pinned configuration fields consumed by the checked components."""

    repository: str
    upstream: str
    fork: str
    base: str
    bot_login: str
    pr_template_sections: dict[str, list[str]]
    conventional_commits: bool
    autonomous: bool
    max_plan_revisions: int
    max_review_repairs: int
    max_fix_rounds: int


class CommentUser(TypedDict):
    """GitHub login attached to a writer comment."""

    login: str


class Comment(TypedDict):
    """Writer-command input after the controller's author and ordering checks."""

    id: int
    user: CommentUser
    body: str


class RevisionWait(TypedDict):
    """The exhausted revision's resume target; legacy recovery may leave it unknown."""

    stage: str | None


class RevisionGrant(TypedDict):
    """A writer's explicit addition to the revision allowance."""

    actor: str
    comment: int
    added: int
    limit: int


class Timing(TypedDict, total=False):
    """Optional normalized exchange observations; missing timestamps remain unknown."""

    turn: str
    status: str
    created: float | None
    started: float | None
    finished: float | None
    operation: str
    attempt: int


class Operation(TypedDict, total=False):
    """Operation fields inspected by eligibility, timing and command recovery."""

    id: str
    status: str
    stage: str
    provider: str
    attempt: int
    timing: Timing
    resume_rebind: str
    admission: Document  # Frozen payer/version/call identity, distinct from prospective attribution.
    shadow_sponsorship: Document  # Prospective attribution; actual payment remains org.


class Session(TypedDict, total=False):
    """Retained session provenance relevant to independence and restarts."""

    role: str
    scope: str
    turn: int
    restarts: int
    completed: Document
    execution_bundle: str


class Reviewer(TypedDict, total=False):
    """The assignment field used when displaying a batch member."""

    provider: str


class EligibilityIdentity(TypedDict):
    """The assessment binding, reviewer and retained session commitment."""

    binding: str
    reviewer: str
    session: str


class EligibilityReceipt(EligibilityIdentity):
    """Sticky independence evidence recorded before dispatch."""

    inputs: str
    eligible: bool
    reason: str


class ReviewRepair(TypedDict, total=False):
    """Correction metadata; older serial checkpoints can lack identity fields."""

    binding: str
    attempts: int
    signature: str
    feedback: Document


class BatchMember(TypedDict, total=False):
    """Per-reviewer progress consumed by commands and dashboard rendering."""

    identity: Reviewer
    status: str
    failures: int
    operation: Operation
    review_repair: ReviewRepair


class ReviewBatch(TypedDict, total=False):
    """The reviewer records inspected by these components."""

    members: dict[str, BatchMember]


class Diagnostic(TypedDict, total=False):
    """Fixed engine correction metadata displayed with an archived report."""

    binding: str
    status: str
    attempt: int
    limit: int
    detail: str


class ReportIndex(TypedDict, total=False):
    """Display and timing fields of an archived report reference."""

    artifact: int
    step: str
    round: str
    provider: str
    outcome: str
    diagnostic: Diagnostic
    timing: Timing


# Presentation offsets from the still-unchecked dashboard_runs module.
Boundary = dict[str, int]


class PlanArtifact(TypedDict):
    """The plan archive identifier needed for public links."""

    number: int


class PendingPublication(TypedDict):
    """Approved candidate publication intent, saved before branch advancement."""

    head: str
    parent: str
    summary: str
    initial: bool


class BranchReservation(TypedDict):
    """Recoverable ownership intent for a development branch."""

    base: str
    created: bool


class Edit(TypedDict):
    """An accepted text edit; None deletes the file."""

    path: str
    content: str | None


class TreeEntry(TypedDict, total=False):
    """Git tree fields read or written during object publication."""

    path: str
    mode: str
    type: str
    sha: str | None
    content: str


class DependencyChange(TypedDict):
    """Package identity and change label used in dependency summaries."""

    name: str
    change: str


class DependencyDelta(TypedDict):
    """Dependency summary projection produced by the lockfile inspection."""

    counts: dict[str, int]
    changed: list[DependencyChange]


class FileInventory(TypedDict):
    """Exact changed-path commitment between two immutable Git commits."""

    base: str
    head: str
    digest: str


class Candidate(TypedDict, total=False):
    """Publication proof and dependency summaries, excluding opaque validation evidence."""

    head: str
    parent: str
    base: str
    plan: str
    edits_digest: str
    changed_files: list[str]
    file_inventory: FileInventory
    dependencies: dict[str, DependencyDelta] | None
    description: Document
    validation: str


class ImplementationBundle(TypedDict, total=False):
    """Writer-authorized implementer-only bundle adoption receipt."""

    approved_bundle: str
    old_bundle: str
    new_bundle: str
    actor: str
    comment: int
    plan: str
    old_skill: str
    new_skill: str


class TaskState(TypedDict, total=False):
    """Only checkpoint fields consumed by this checked slice; not a loader or validator.

    Optional keys reflect stage-dependent and legacy records. Reports/plans retain their
    separately validated document formats rather than being recatalogued here.
    """

    stage: str
    retry_stage: str
    credential_recovery: Document  # Recorded consent, validation receipt and automatic retry deadline.
    mode: str
    issue: int
    pr: int
    pr_description: Document
    description_update: Document
    pr_template: Document
    base: str
    branch: str
    head: str
    plan_id: str
    plan: Document
    plan_draft: Document
    intake_draft: Document
    plan_revisions: int
    plan_revision_extra: int
    plan_revision_grant: RevisionGrant
    revision_wait: RevisionWait
    round: int
    input_revision: int
    command_id: int
    reason: str
    requirements: Document
    issue_digest: str
    config_digest: str
    bundle_id: str
    charter_id: str
    charter: Document
    implementation_skill: str
    implementation_bundle: ImplementationBundle
    bundle_resume: Document
    implementation_feedback: dict[str, str]
    session_epoch: int
    sessions: dict[str, Session]
    design_generation: int
    design_generation_high: int
    design_phase: str
    design_votes: dict[str, Document]
    design_feedback: list[Document]
    design_eligibility: dict[str, EligibilityReceipt]
    operation: Operation
    review_batch: ReviewBatch
    review_repair: ReviewRepair
    approval: Document
    qc: dict[str, Document]
    reviews: dict[str, Document]
    review_history: list[dict[str, Document]]
    objections: list[Document]
    findings: list[Document]
    pending: PendingPublication
    candidate: Candidate
    development_branches: dict[str, BranchReservation]
    approved_inventory_parent: str
    changed_files: list[str]
    handoff_ready: bool
    reports: list[ReportIndex]
    invalidations: list[Document]
    requests: list[Document]
    conversation: Document
    local_status: Document  # Legacy-only local-review evidence used to infer display progress.


class GitHubClient(Protocol):
    """Narrow publication transport; response validation remains at its existing boundary."""

    def repo(self, path: str, method: str = "GET", data: Document | None = None) -> Any:
        """Read or mutate a repository endpoint."""
        ...

    def pages(self, path: str, key: str | None = None) -> list[Document]:
        """Collect an endpoint's paginated records."""
        ...

    def create_tree(self, base_tree: str, entries: list[TreeEntry], uploaded_blobs: list[str]) -> Any:
        """Create a tree with the existing bounded visibility retry."""
        ...


class CheckpointView(Protocol):
    """Read-only checkpoint interface used by dashboard rendering."""

    @property
    def plan(self) -> PlanArtifact | None:
        """The currently linked plan artifact."""
        ...

    def links(self) -> list[str]:
        """Return the audit link and recovery reference."""
        ...


class BundleView(Protocol):
    """Trusted bundle properties used to prove implementer-only adoption."""

    @property
    def hashes(self) -> dict[str, str]:
        """Hashes of the bundle's constituent resources."""
        ...

    @property
    def identity(self) -> str:
        """The digest of the resource hash map."""
        ...

    @property
    def root(self) -> PathLike[str]:
        """Filesystem root, passed to git as text by the existing implementation."""
        ...
