"""Read-only issue dashboard rendering, separate from controller transitions and writes."""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

import change_attention
import classification
import dashboard_runs
import markup
import protocol
import reason_guidance
import review_state
import review_timing
from actions import BUNDLE_REASON
from checkpoints import MARKER
from markup import code, link, text
from model_config import assigned
from policy import approval_binding, design_roster, digest
from vocab import MERGED_STAGES

if TYPE_CHECKING:
    from collections.abc import Callable

    from records import Boundary, CheckpointView, Config, DependencyDelta, ReportIndex, TaskState


# GitHub rejects a comment over 65,536 characters.
SUMMARY_LIMIT = 60000
STEPS = ("Intake", "Plan", "Design review", "Implement", "Code review", "Handoff")
STAGE_STEP = {
    "intake": 0,
    "plan": 1,
    "plan_qc": 2,
    "planned": 2,
    "implement": 3,
    "publish": 3,
    "review": 4,
    "gates": 5,
    "merging": 5,
}


BATCH_STATUS: dict[str | None, str] = {
    "queued": "queued",
    "running": "reviewing",
    "retrying": "retrying",
    "complete": "result received",
    "done": "result received",
}


def dependency_summary(path: str, delta: DependencyDelta) -> str:
    """One line: a lockfile's changed packages (the first few by name) and its added and removed counts."""
    counts = delta["counts"]
    named = [f"{c['name']} {c['change']}" for c in delta["changed"][:3]]
    more = counts["changed"] - len(named)
    parts = [", ".join(named) + (f" and {more} more" if more > 0 else "")] if named else []
    parts += [f"{counts[k]} {k}" for k in ("added", "removed") if counts[k]]
    return f"Dependencies ({path}): " + ("; ".join(parts) or "checksums only")


class Dashboard:
    """One rendering view of state, config and checkpoints; recreate after task changes."""

    def __init__(
        self,
        state: TaskState,
        config: Config,
        checkpoints: CheckpointView,
        pr_url: Callable[[int], str],
        waiting_revision: Callable[[], str | None],
        changed_paths: Callable[[], list[str]] | None = None,
    ) -> None:
        """Accept display data and two narrow controller queries, never its write interface."""
        self.state = state
        self.config = config
        self.checkpoints = checkpoints
        self.pr_url = pr_url
        self.waiting_revision = waiting_revision
        self.changed_paths = changed_paths

    @cached_property
    def run_boundaries(self) -> list[Boundary]:
        """Resolve invalidation offsets once for this rendering view."""
        return dashboard_runs.boundaries(self.state)

    @property
    def boundary(self) -> Boundary:
        """The current run's display offsets."""
        return self.run_boundaries[-1]

    @cached_property
    def current_reports(self) -> list[ReportIndex]:
        """Keep only report links from the current run."""
        return [r for r in self.state.get("reports", []) if r["artifact"] > self.boundary["artifact"]]

    def summary_body(self) -> str:
        """The status comment: stage, plan, PR and votes first; reports and evidence collapsed.

        One that would exceed its limit drops the collapsed reports and shortens the reason; the
        status itself always remains.
        """
        body = self.summary_lines(compact=False)
        if len(body.encode()) > SUMMARY_LIMIT:
            body = self.summary_lines(compact=True)
        return body

    def summary_lines(self, compact: bool) -> str:
        """The status comment: live steps with their review rounds, then optional diagnostics.

        `compact` drops historical rows; reports themselves live in their archives.
        """
        state = self.state
        current = self.current_step()
        rows = [self.step_row(index, name, current) for index, name in enumerate(STEPS)]
        progress = " → ".join(
            f"**{name} {status.split(' ')[0]}**"
            if index == current
            else f"{name} {status.split(' ')[0]}"
            if not status.startswith("Pending")
            else name
            for index, (name, status, _) in enumerate(rows)
        )
        lines = [
            MARKER,
            "## Concurro",
            "",
            self.activity_line(),
            "",
            *(
                [markup.BLOCKING_NOTE, ""]
                if any(f["status"] in ("open", "withdrawn") for f in state.get("findings", []))
                else []
            ),
            progress,
            "",
            markup.table(["Step", "Status", "Detail"], self.progress_rows(rows, compact, current)),
            "",
        ]
        if state.get("pr_description", {}).get("human_edited"):
            lines += ["PR description refresh stopped after a human edit. Please keep the description current.", ""]
        classification_row = self.classification_row()
        if classification_row:
            lines += [classification_row, ""]
        for path, delta in sorted((state.get("candidate", {}).get("dependencies") or {}).items()):
            lines += ["📦 " + text(dependency_summary(path, delta)), ""]
        attention = change_attention.notice(
            self.changed_paths() if self.changed_paths else state.get("changed_files", []),
            self.config,
            state.get("base"),
            state.get("pending", {}).get("head", state.get("head")),
        )
        if attention:
            lines += [attention, ""]
        action = self.action_line()
        if action:
            lines += [action, ""]
        diagnostics = self.diagnostics_table()
        if diagnostics and compact:
            diagnostics = "Output corrections and previous runs are recorded in the linked automation audit state."
        audit, reference = self.checkpoints.links()
        diagnostics = "\n\n".join(part for part in (diagnostics, audit) if part)
        return "\n".join(lines + [markup.details("Diagnostics", diagnostics), "", reference, ""])

    def activity_line(self) -> str:
        """Name current work without implying a paused or queued turn is running."""
        stage = self.state["stage"]
        paused = {
            "clarify": "Waiting for your answers before continuing.",
            "blocked": "Paused — a blocker needs attention.",
            "escalated": "Paused — the task needs attention before it can continue.",
            "rejected": "This issue was not accepted for implementation.",
            "stopped": "Work on this issue has stopped.",
            "closed": "The pull request was closed without merging.",
            "merged": "The pull request has been merged.",
            "observed_merged": "The pull request has been merged.",
            "planned": "The plan is ready; waiting for your approval.",
            "publish": "Publishing the implementation and preparing its pull request.",
        }
        handlers = {
            "gates": self._gate_activity,
            "plan_qc": self._design_activity,
            "review": self._review_activity,
            **dict.fromkeys(("intake", "plan", "implement"), self._agent_activity),
        }
        message = paused[stage] if stage in paused else handlers.get(stage, self._default_activity)()
        alert = (
            "CAUTION"
            if stage == "blocked"
            else "WARNING"
            if stage in ("planned", "clarify", "escalated", "rejected")
            or stage == "gates"
            and reason_guidance.gate_action(self.state.get("reason", ""))
            else "TIP"
            if stage == "gates" and self.state.get("handoff_ready")
            else "NOTE"
        )
        return f"> [!{alert}]\n> " + message.replace("\n", "\n> ")

    def _gate_activity(self) -> str:
        """Describe readiness checks or the completed human hand-off."""
        state = self.state
        action = reason_guidance.gate_action(state.get("reason", ""))
        if action:
            return "The pull request needs attention. " + action
        return (
            "Ready for your review. A human decides whether to merge."
            if state.get("handoff_ready")
            else "Checking reviews and CI before marking the pull request ready. No action needed."
        )

    def _design_activity(self) -> str:
        """Describe pending design reviewers, preferring batch progress over serial repair state."""
        state = self.state
        members = [m for m in design_roster(self.config) if m["id"] not in state.get("design_votes", {})]
        if not members:
            return "Checking the design-review decisions before moving on."
        phase = text(markup.review_phase(state.get("design_phase", "cooperative")))
        batch = state.get("review_batch", {}).get("members", {})
        if batch:
            tasks = []
            for member in members:
                record = batch.get(member["id"], {})
                activity = (
                    "correcting its output"
                    if record.get("review_repair")
                    else BATCH_STATUS.get(record.get("status"), "waiting")
                )
                tasks.append(f"{markup.code(member['provider'])}: {activity}")
            return f"{phase.capitalize()} design review — " + "; ".join(tasks) + "."
        repair = state.get("review_repair", {})
        correcting = next(
            (
                m
                for m in members
                if repair and repair.get("binding") == digest([approval_binding(state, self.config), m])
            ),
            None,
        )
        if correcting:
            tasks = []
            for member in members:
                task = (
                    f"correct its {phase} design review — correction {repair.get('attempts', 0)} "
                    f"of {self.config['max_review_repairs']}"
                    if member == correcting
                    else f"complete its {phase} design review"
                )
                tasks.append(f"{markup.code(member['provider'])} to {task}")
            return "We're waiting for " + "; and ".join(tasks) + "."
        names = " and ".join(markup.code(m["provider"]) for m in members)
        task = "its" if len(members) == 1 else "their"
        return f"We're waiting for {names} to complete {task} {phase} design review."

    def _review_activity(self) -> str:
        """List all PR reviewers whose current-head results are still outstanding."""
        providers = [p for p in assigned(self.config, "pr_reviewers") if p not in self.state.get("reviews", {})]
        names = " and ".join(markup.code(p) for p in providers)
        return (
            f"We're waiting for {names} to review the pull request."
            if providers
            else "Checking review findings and deciding whether a repair is needed."
        )

    def _agent_activity(self) -> str:
        """Describe intake, planning or implementation, including rejected-output correction."""
        state, stage = self.state, self.state["stage"]
        role, task = {
            "intake": ("triage", "assess the issue"),
            "plan": ("planner", "prepare the implementation plan"),
            "implement": ("implementer", "implement the approved plan"),
        }[stage]
        draft = state.get("intake_draft" if stage == "intake" else "plan_draft") if stage != "implement" else None
        return f"Waiting for {markup.code(assigned(self.config, role))} to " + (
            "correct its output; details are in Diagnostics." if draft else task + "."
        )

    def _default_activity(self) -> str:
        """Keep the fallback for stages with no specialized activity message."""
        return "Checking the task’s current state."

    def classification_row(self) -> str:
        """Render labels and archive links, never model-written rationale, on the dashboard."""
        result = classification.view(dashboard_runs.assessment_state(self.state), self.config)
        if not result.estimate.sources:
            return "**Estimate:** Not assessed yet."
        estimate = result.estimate
        scope = estimate.binding
        label = {"intake": "Intake", "plan_qc": "Design", "review": "PR"}[scope["stage"]]
        detail = label + " assessment"
        if result.provisional:
            detail += " · Assessment incomplete"
        if result.previous_binding and result.previous_binding != scope:
            detail += " · Based on the previous revision"
        if result.disagreement:
            detail += " · reviewers disagree"
        sources = classification.resolve_sources(estimate.sources, self.state)
        artifacts = sorted({s["artifact"] for s in sources if s.get("artifact")})
        links = [link(f"Report {number}", self.artifact_url(number)) for number in artifacts]
        return markup.table(
            ["Estimate", "Assessment", "Evidence"],
            [
                [
                    f"Complexity: {estimate.complexity} · Change risk: {estimate.change_risk}",
                    text(f"Confidence: {estimate.confidence} · {detail}"),
                    " · ".join(links),
                ]
            ],
        )

    def artifact_url(self, number: int) -> str:
        """An artifact of this issue on GitHub."""
        return protocol.artifact_url(self.config["repository"], self.state["issue"], number)

    def current_step(self) -> int:
        """The step the task is on, or the furthest it reached when paused; len(STEPS) only once merged."""
        state = self.state
        stage = state["stage"]
        if stage in MERGED_STAGES:
            return len(STEPS)
        if stage in STAGE_STEP:
            return STAGE_STEP[stage]
        if stage in ("blocked", "escalated") and state.get("retry_stage") in STAGE_STEP:
            return STAGE_STEP[state["retry_stage"]]
        if state.get("pr"):
            return 4 if len(state.get("reviews", {})) < len(assigned(self.config, "pr_reviewers")) else 5
        if state.get("local_status") or state.get("approval"):
            return 3
        if (
            state.get("design_votes")
            or stage == "clarify"
            and not state.get("plan", {}).get("questions")
            and state.get("plan")
        ):
            return 2
        return 1 if state.get("plan") or state.get("plan_draft") else 0

    def step_row(self, index: int, name: str, current: int) -> tuple[str, str, str]:
        """`(name, status, detail)` of one step; details are engine values and at most one headline."""
        state = self.state
        stage = state["stage"]
        if index > current:
            return name, "Pending", ""
        paused = {
            "clarify": ("❓", "Input needed"),
            "blocked": (markup.BLOCKED, "Blocked"),
            "escalated": (markup.ESCALATED, "Escalated"),
            "rejected": (markup.BLOCKED, "Rejected"),
            "stopped": ("⏹️", "Stopped"),
            "closed": ("⏹️", "Closed without merge"),
        }
        done = index < current
        intake = state.get("qc", {}).get("intake")
        if name == "Intake":
            status = markup.label(markup.DECISION[intake["decision"]]) if intake else "▶️ In progress"
            detail = markup.headline(intake, "rationale") or "" if intake else ""
        elif name == "Plan":
            version = f"Version {state.get('plan_revisions', 0) - self.boundary['plan_revisions'] + 1}"
            status = f"✅ {version}" if done else "▶️ In progress"
            plan = self.checkpoints.plan
            detail = (
                link("Implementation plan", self.artifact_url(plan["number"]))
                if plan and (state.get("plan") or state.get("plan_draft"))
                else ""
            )
        elif name == "Design review":
            roster = len(design_roster(self.config))
            approve = sum(v["report"]["decision"] == "approve" for v in state.get("design_votes", {}).values())
            objections = sum(not v["resolved"] for v in state.get("objections", []))
            round_ = f"Round {state.get('design_generation', 0) + 1 - self.boundary['generation']} · {markup.review_phase(state.get('design_phase', 'cooperative'))}"
            status = (
                f"✅ {approve} of {roster} approve"
                if done
                else ("Awaiting writer approval" if stage == "planned" else f"▶️ {approve} of {roster} approve")
            )
            detail = f"{round_} · {objections} open objection{'s' if objections != 1 else ''}"
        elif name == "Implement":
            candidate = state.get("candidate", {})
            status = "✅ Candidate published" if done else "▶️ In progress"
            detail = "Agent-reported validation; independent reviews and CI follow" if candidate else ""
        elif name == "Code review":
            reviews = state.get("reviews", {})
            approve = sum(r["verdict"] == "approve" for r in reviews.values())
            total = len(assigned(self.config, "pr_reviewers"))
            status = f"✅ {approve} of {total} approve" if done else f"▶️ {len(reviews)} of {total} reviewed"
            open_findings = sum(f["status"] in ("open", "withdrawn") for f in state.get("findings", []))
            pr = link(f"PR #{state['pr']}", self.pr_url(state["pr"])) if state.get("pr") else ""
            detail = " · ".join(
                p for p in (pr, f"{open_findings} open finding{'s' if open_findings != 1 else ''}") if p
            )
        else:
            if current == len(STEPS):
                status, detail = "✅ Merged", ""
            elif stage == "gates" and state.get("handoff_ready"):
                status, detail = "Ready for human review", "A human reviews and merges"
            else:
                status = f"{markup.GATE} Blocked" if state.get("reason") and stage == "gates" else "▶️ Checking gates"
                detail = "Readiness checks are pending; see diagnostics" if stage == "gates" else ""
        if not done and index == current and stage in paused:
            status = markup.label(paused[stage])
        return name, status, detail

    def action_line(self) -> str:
        """One line naming what a human must do, linking the comment that asks."""
        state = self.state
        journal = state.get("conversation", {})
        latest = journal.get("latest")
        url = protocol.comment_url(self.config["repository"], state["issue"], latest) if latest else None
        open_request = next(
            (r for r in reversed(state.get("requests", [])) if r["state"] == "open" and r.get("comment")), None
        )
        if open_request:
            url = protocol.comment_url(self.config["repository"], state["issue"], open_request["comment"])
            return f"**Action needed:** {link('answer the questions', url)}."
        needs = {
            "blocked": "a fix and a retry",
            "escalated": "a decision",
            "rejected": "an edit and a retry",
            "planned": "plan approval" if state.get("mode") == "manual" else None,
        }.get(state["stage"])
        if state["stage"] in ("blocked", "stopped", "escalated") and self.waiting_revision():
            needs = "a writer to grant revision capacity with " + code("/concurro retry --plan-revisions 1")
        if state["stage"] == "blocked" and state.get("reason") == BUNDLE_REASON:
            needs = (
                "a writer to continue with "
                + code("/concurro resume")
                + " or reassess with "
                + code("/concurro invalidate")
            )
        if state["stage"] == "blocked" and reason_guidance.operator_summary(state.get("reason", "")):
            needs = "operator reconciliation"
        if state["stage"] == "gates":
            needs = reason_guidance.gate_action(state.get("reason", ""))
        if not needs and state["stage"] == "gates" and state.get("handoff_ready"):
            needs = "a human review and merge"
        if not needs:
            return ""
        line = f"**Action needed:** {needs}" + (f" ({link('details', url)})." if url else ".")
        paused = {
            "blocked": (markup.BLOCKED, "Blocked"),
            "escalated": (markup.ESCALATED, "Escalated"),
            "rejected": (markup.BLOCKED, "Rejected"),
        }.get(state["stage"])
        if paused and state.get("reason"):
            line = f"{paused[0]} **{paused[1]}:** {text(state['reason'], 300)}\n\n" + line
        return line

    def progress_rows(
        self, steps: list[tuple[str, str, str]], compact: bool = False, current: int | None = None
    ) -> list[list[str]]:
        """Place each round below its stage, keeping archived votes linked in their original order."""
        reports = [r for r in self.current_reports if not r.get("diagnostic")]
        current = self.current_step() if current is None else current
        rows = []
        for step, status, detail in steps:
            rows.append([step, status, detail])
            if compact:
                continue
            rounds = list(dict.fromkeys(r["round"] for r in reports if r["step"] == step))
            current_round, expected = None, []
            if step == "Design review" and current == 2:
                current_round = (
                    f"{self.state.get('design_generation', 0) + 1} {self.state.get('design_phase', 'cooperative')}"
                )
                expected = [member["provider"] for member in design_roster(self.config)]
            elif step == "Code review" and current == 4:
                current_round = str(self.state.get("round", 0) + 1)
                expected = assigned(self.config, "pr_reviewers")
            if current_round and current_round not in rounds:
                rounds.append(current_round)
            for round_ in rounds:
                entries = {r["provider"]: r for r in reports if (r["step"], r["round"]) == (step, round_)}
                cells = self._round_cells(step, entries, expected if round_ == current_round else [])
                rows.append(
                    [
                        text("↳ Round " + dashboard_runs.display_round(step, round_, self.boundary)),
                        "Current" if round_ == current_round else "",
                        "<br>".join(cells),
                    ]
                )
        return rows

    def _round_cells(self, step: str, entries: dict[str, ReportIndex], expected: list[str]) -> list[str]:
        """Render recorded votes first, then outstanding reviewers in roster order."""
        cells = []
        vocabulary = markup.DECISION if step in ("Intake", "Design review") else markup.VERDICT
        for provider, entry in entries.items():
            label = (
                ("⚠️", "Output rejected")
                if entry["outcome"] == "invalid"
                else vocabulary.get(entry["outcome"], ("📝", "Agent report"))
            )
            cells.append(link(f"{provider}: {markup.label(label)}", self.artifact_url(entry["artifact"])))
        for provider in expected:
            if provider not in entries:
                cells.append(text(provider + ": " + self._pending_reviewer_status(provider)))
        return cells

    def _pending_reviewer_status(self, provider: str) -> str:
        """Distinguish paused, serial and batch work without changing correction wording."""
        state = self.state
        operation = review_state.operation(state)
        active = (
            state["stage"] in ("plan_qc", "review")
            and operation.get("status") == "started"
            and operation.get("provider") == provider
        )
        member = next(
            (
                m
                for m in state.get("review_batch", {}).get("members", {}).values()
                if m["identity"]["provider"] == provider
            ),
            None,
        )
        if member and state["stage"] == "plan_qc":
            return (
                "Correcting output"
                if member.get("review_repair")
                else BATCH_STATUS.get(member.get("status"), "waiting").capitalize()
            )
        return "Paused" if state["stage"] not in ("plan_qc", "review") else "Reviewing" if active else "Waiting"

    def diagnostics_table(self) -> str:
        """Show output corrections below the live status, with links to immutable evidence."""
        rows = []
        for report in self.current_reports:
            diagnostic = report.get("diagnostic")
            if not diagnostic:
                continue
            rows.append(
                [
                    text(report["provider"]),
                    text(dashboard_runs.display_round(report["step"], report["round"], self.boundary)),
                    text(f"{diagnostic['status']} · {diagnostic['attempt']}/{diagnostic['limit']} corrections"),
                    text(diagnostic["detail"], 300),
                    link("Rejected output", self.artifact_url(report["artifact"])),
                ]
            )
        repair = self.state.get("review_repair", {})
        if repair and not any(
            r.get("diagnostic", {}).get("binding") == repair.get("binding") for r in self.state.get("reports", [])
        ):
            # Older checkpoints have no rejected-report archive or reviewer identity.
            rows.append(
                [
                    "Design reviewer",
                    text(markup.review_phase(self.state.get("design_phase", ""))),
                    text(f"{repair.get('attempts', 0)}/{self.config['max_review_repairs']} corrections"),
                    text(repair.get("feedback", {}).get("detail", "Output needs correction."), 300),
                    "Not retained by the earlier engine",
                ]
            )
        for key, label in (("intake_draft", "Intake"), ("plan_draft", "Planner")):
            draft = self.state.get(key)
            if draft:
                rows.append(
                    [
                        label,
                        "—",
                        text(f"Rejected output {draft['attempt']}"),
                        text(draft["reason"], 300),
                        "See the linked report or plan",
                    ]
                )
        sections = []
        if rows:
            sections.append(
                "Output validation and correction history; these are not design objections.\n\n"
                + markup.table(["Agent", "Round", "Outcome", "Reason", "Evidence"], rows)
            )
        timings = []
        for report in self.current_reports:
            observed = review_timing.normalize(report.get("timing"))
            if observed:
                measured = review_timing.durations(observed)
                labels = [
                    "Unknown" if measured[key] is None else f"{measured[key]:.1f} s"
                    for key in ("admission_to_claim_seconds", "execution_seconds")
                ]
                timings.append(
                    [
                        text(report["provider"]),
                        text(dashboard_runs.display_round(report["step"], report["round"], self.boundary)),
                        *labels,
                        link("Report", self.artifact_url(report["artifact"])),
                    ]
                )
        if timings:
            sections.append(
                "Observed turn timing (includes setup/tools; missing durations are unknown).\n\n"
                + markup.table(["Agent", "Round", "Admission to claim", "Execution", "Evidence"], timings)
            )
        if self.state.get("reason") and self.state["stage"] == "gates":
            sections.append("Readiness: " + text(self.state["reason"], 300))
        previous = self.previous_runs()
        if previous:
            sections.append(markup.details("Previous runs", previous))
        return "\n\n".join(sections)

    def previous_runs(self) -> str:
        """Link invalidated run evidence separately from current progress, without copying reports."""
        boundaries = self.run_boundaries
        sections = []
        for i, record in enumerate(self.state.get("invalidations", [])):
            reports = [
                r
                for r in self.state.get("reports", [])
                if boundaries[i]["artifact"] < r["artifact"] <= boundaries[i + 1]["artifact"]
            ]
            command = protocol.comment_url(self.config["repository"], self.state["issue"], record["comment"])
            sections.append(
                f"**Run {i + 1} — invalidated**\n\n"
                + link("Writer requested reassessment", command)
                + ". Earlier approvals no longer apply."
            )
            rows = []
            for report in reports:
                diagnostic = report.get("diagnostic")
                outcome = "Output rejected: " + diagnostic["detail"] if diagnostic else report["outcome"]
                rows.append(
                    [
                        text(report["step"]),
                        text(dashboard_runs.display_round(report["step"], report["round"], boundaries[i])),
                        text(report["provider"]),
                        text(outcome, 300),
                        link("Report " + str(report["artifact"]), self.artifact_url(report["artifact"])),
                    ]
                )
            if rows:
                sections.append(markup.table(["Step", "Round", "Agent", "Outcome", "Evidence"], rows))
            else:
                sections.append("Earlier report links are available in the audit artifact history.")
        return "\n\n".join(sections)
