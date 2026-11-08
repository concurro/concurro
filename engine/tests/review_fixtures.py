"""PR findings, model responses and manual publication lifecycle helpers."""

from copy import deepcopy

from support import CONFIG

from checkpoints import Checkpoints
from controller import Controller
from fakes import FINDING, FakeAgents, FakeGitHub
from pipeline_fixtures import ISSUE
from readiness import review_reasons

DESIGN_ISSUE = {"kind": "plan", "explanation": "The plan misses a case.", "evidence": ["The guide omits it."]}


def report(verdict="approve", findings=(), dispositions=(), design_issue=None):
    return {
        "verdict": verdict,
        "summary": "Reviewed.",
        "findings": [dict(f) for f in findings],
        "dispositions": [dict(d) for d in dispositions],
        "design_issue": design_issue,
    }


def answer(identifier, status="fixed"):
    return {"id": identifier, "status": status, "evidence": "Lines 3-4 now define the term."}


class Reviewers(FakeAgents):
    """Each listed provider requests one change on its first review, then confirms the fix.

    `retire` answers withdrawn findings as obsolete; `reopen_once` makes the implementer
    reopen the design on its first repair round.
    """

    def __init__(self, requesting, findings=None, reopen_once=False):
        super().__init__()
        self.requesting = set(requesting)
        self.findings = findings
        self.reopen_once = reopen_once

    def ask(self, provider, stage, context):
        from feedback import restore

        context = restore(context)
        if stage == "develop" and self.reopen_once and context["review_findings"]:
            self.reopen_once = False
            self.calls.append((provider, stage, deepcopy(context)))
            return {"summary": "Reconsider the plan.", "edits": [], "validation": "", "design_issue": DESIGN_ISSUE}
        if stage != "review":
            return super().ask(provider, stage, context)
        self.calls.append((provider, stage, deepcopy(context)))
        if provider in self.requesting:
            self.requesting.discard(provider)
            found = (self.findings or {}).get(provider) or [
                {**FINDING, "detail": provider + " finds the term undefined."}
            ]
            return report("changes_requested", found)
        return report(
            dispositions=[
                answer(f["id"], "obsolete" if f["status"] == "withdrawn" else "fixed") for f in context["own_findings"]
            ]
        )


class Harness:
    """The real controller over the fake GitHub, ticked as the Actions path does."""

    def setUp(self):
        self.config = deepcopy(CONFIG)
        self.config["autonomous"] = False
        self.gh = FakeGitHub(self.config)

    def run_to(self, agents, stage, ticks=30, until=lambda state: True):
        """Tick, approving the plan when asked, until `stage` (and `until`); return the controller."""
        for _ in range(ticks):
            controller = Controller(self.gh, agents, self.config)
            controller.advance(ISSUE)
            state = controller.state
            if state["stage"] == "planned":
                self.gh.comment("/concurro approve " + state["plan_id"])
            if state["stage"] == stage and until(state):
                return controller
        self.fail("never reached " + stage)

    def load(self):
        """The task's checkpoint and its state."""
        checkpoints = Checkpoints(self.gh, self.config, 7)
        return checkpoints, checkpoints.load(self.gh.pages("issues/7/comments"))

    def pad(self, total, reason=None):
        """Grow the checkpoint to about `total` bytes with untrusted outcome text, which only feeds model context."""
        checkpoints, state = self.load()
        state["prior_outcomes"] = []
        if reason:
            state["reason"] = reason
        base = len(checkpoints.audit_body(state).encode())
        state["prior_outcomes"] = [{"padding": "x" * max(0, total - base - 30)}]
        checkpoints.save(state)

    def reasons(self, state):
        """The merge gates' reasons for this state against the fake's PR."""
        files = [f["filename"] for f in self.gh.pages("pulls/9/files")]
        return review_reasons(state, self.gh.pr, files, self.gh.threads(9), self.config)
