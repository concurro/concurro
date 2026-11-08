"""Plan-revision accounting and exhausted-revision recovery targets."""


class RevisionBudget:
    """Track spent revisions and writer grants on the current task state."""

    def __init__(self, state, config):
        """Use the live state and pinned configuration without copying or saving them."""
        self.state = state
        self.config = config

    def limit(self):
        """Pinned automatic allowance plus explicit writer grants; spent revisions never reset."""
        return self.config["max_plan_revisions"] + self.state.get("plan_revision_extra", 0)

    def waiting(self):
        """The already-invalidated revision to resume, including the old design-review stall."""
        waiting = self.state.get("revision_wait", {}).get("stage")
        if waiting in ("plan", "intake"):
            return waiting
        # Older checkpoints lost the resume target after recording this exact transition.
        if (
            self.state.get("reason") == "Plan revision budget exhausted: Design reviewers require a revision."
            and self.state.get("design_feedback")
            and self.state.get("plan")
            and not self.state.get("design_votes")
            and not self.state.get("approval")
        ):
            return "plan"
        return None

    def revise(self, stage, reason, *, invalidate_design, save, invalidate=True, save_exhausted=True):
        """Limit automatic plan revisions across QC feedback and changing inputs."""
        if invalidate:
            invalidate_design(reason)
        if self.state["plan_revisions"] >= self.limit():
            self.state.update(
                stage="escalated",
                retry_stage=stage,
                revision_wait={"stage": stage},
                reason="Plan revision budget exhausted: " + reason,
            )
            if save_exhausted:
                save()
            return False
        self.state["plan_revisions"] += 1
        self.state.pop("revision_wait", None)
        self.state.update(stage=stage, reason=reason)
        return True
