"""Standalone review of an existing pull request: one task per PR, one round per verified request.

The task's checkpoint and reports are artifacts of the PR itself. Each round reviews the
snapshot frozen when it opened and publishes point-in-time native reviews; nothing is
repaired, re-described, marked ready or merged.
"""

import bundle_resume
import commands
import conversation
import delegation
import protocol
import review_checks
import reviews as review_rules
from controller import WAIT_SECONDS, Controller, Outcome, config_digest
from markup import code, link, text
from model_config import assigned
from policy import MODEL_RESPONSE_LIMIT, Blocked, digest, selected_model, validate_pr_review
from repository import valid_ref
from review_checks import ACTIVE, CONFIG_REASON, current_round, now
from vocab import STANDALONE_REVIEW, SUSPENDED_STAGES

FOREIGN_KIND = "Concurro artifacts on this pull request belong to another kind of task; reconcile manually"
REVIEW_REF = "concurro/review/pr-{pr}"
BASE_REF = "concurro/review/base"
FINDING_NOTE = "Shortened to fit GitHub's limit; the complete finding is retained in Concurro's report data."
UNRENDERED = "Not rendered: binary, symbolic link, submodule or non-UTF-8 content. Inspect it in the checkout."
STEPS = {"queued": "admit", "review": "review", "publishing": "finish"}
COMMAND_SET = (
    "Standalone reviews accept "
    + ", ".join(code("/concurro " + name) for name in ("stop", "retry", "resume"))
    + f" and the provider funding commands ({code('funding')}, {code('sponsor')}, {code('withdraw')})."
)
FUNDING = (commands.CommandKind.FUNDING, commands.CommandKind.SPONSOR, commands.CommandKind.WITHDRAW)


class ReviewController(Controller):
    """Advance one PR's review rounds; reuses the contribution controller's model-call and storage paths."""

    kind = STANDALONE_REVIEW

    def __init__(self, github, agents, config, *, requests, publisher=None, **options):
        """`requests()` lists the PR's verified review-request events, oldest first.

        `github` holds the App's storage identity; `publisher` is the same repository under
        the reviewer account, which alone can see and submit its pending reviews.
        """
        super().__init__(github, agents, config, **options)
        self.requests, self.publisher = requests, publisher or github
        # Repository content, permissions and PR state are read as the App.
        self.code = self.pulls = github
        self.reader.code = self.reader.pulls = github
        self.pull = None

    # Persistence and public views

    def save(self, publish_events=True, outboxes=True):
        """Persist the checkpoint, then deliver reports, reviews and the check derived from it.

        There is no dashboard comment, stage label or lifecycle notice on a reviewed PR.
        """
        state = self.state
        self.checkpoint()
        if publish_events and conversation.publish(state, self.gh, self.config, self.checkpoints.reports):
            self.checkpoint()
        if publish_events and outboxes and state["stage"] != "closed":
            # Nothing is written to a closed PR; retained reviews publish if it reopens.
            review_rules.publish(state, self.publisher, self.config, self.checkpoint)
        review_checks.sync(state, self.gh, self.config, self.checkpoint)
        self.checkpoints.dirty = False

    def _needs_flush(self):
        """Retained outboxes or an unsettled check update, left by an interrupted save."""
        state = self.state
        return bool(
            state.get("conversation", {}).get("pending")
            or state.get("pr_outbox")
            and state["stage"] != "closed"
            or review_checks.unsettled(state)
            or state.get("check") is None
            and state.get("rounds")
        )

    def outcome(self, progressed):
        """Scheduling outcome from the durable stage; a closed PR wakes again only when reopened."""
        if self.foreign:
            return Outcome("terminal")
        if not self.state:
            return Outcome("await-human")
        stage = self.state["stage"]
        if self.queued_commands:
            return Outcome("continue", True)
        if review_checks.unsettled(self.state) and stage not in ACTIVE:
            # Nothing else would wake a finished or paused task to repair or reconcile its check.
            return Outcome("wait-until", progressed, WAIT_SECONDS)
        if stage == "closed":
            return Outcome("terminal", progressed)
        if stage in SUSPENDED_STAGES:
            return Outcome("blocked", progressed)
        if stage == "complete":
            return Outcome("await-human", progressed)
        if progressed:
            return Outcome("continue", True)
        return Outcome("wait-until", retry_after=WAIT_SECONDS)

    # Enrollment and rounds

    def initialize(self, comments, request):
        """Create the task from its first verified request and persist it before any model work."""
        pr = self.pull
        self.enroll()
        self.state = {
            "version": 2,
            "kind": STANDALONE_REVIEW,
            "issue": pr["number"],
            "pr": pr["number"],
            "pull": {"node": pr["node_id"], "id": pr["id"]},
            "mode": "review",
            "controller": self.controller_id,
            "stage": "complete",
            # The requester of the first round is the funding initiation boundary.
            "enrollment": {
                "actor": request["actor"]["login"],
                "actor_id": request["actor"]["id"],
                "event": request["delivery"],
                "at": request["at"],
            },
            "calls": 0,
            "round": 0,
            "rounds": [],
            "reviews": {},
            "command_id": 0,
            "requests": [],
            "config_digest": config_digest(self.config),
            "bundle_id": self.bundle.identity,
            "charter_id": digest(self.bundle.charter),
            "charter": self.bundle.charter,
            "execution": self.config["paths"],
        }
        self.state.update({key: self.config[key] for key in ("upstream", "fork", "config_sha") if key in self.config})
        if "agents" in self.config:
            self.state["model_configuration"] = {key: self.config[key] for key in ("models", "agents")}
        self.open_round(request)
        if self.sponsorship:
            self.sponsorship.initialize(self, comments, "review", enrollment=self.state["enrollment"])
        self.save()

    def open_round(self, request):
        """Freeze the PR as it is now: every reviewer of the round reviews this snapshot."""
        state, pr = self.state, self.pull
        head, tip = pr["head"]["sha"], pr["base"]["sha"]
        base = self.gh.repo(f"compare/{tip}...{head}")["merge_base_commit"]["sha"]
        if state["reviews"]:
            state.setdefault("review_history", []).append(state["reviews"])
        number = state["round"] + 1
        state.update(
            stage="queued",
            round=number,
            reviews={},
            base=base,
            head=head,
            base_ref=pr["base"]["ref"],
            base_tip=tip,
            requirements={"title": pr["title"], "body": pr.get("body") or "", "discussion": []},
            issue_digest=digest([pr["title"], pr.get("body")]),
            input_revision=state.get("input_revision", 0) + 1,
        )
        for key in ("reason", "retry_stage"):
            state.pop(key, None)
        state["changed_files"] = self.reader.changed_paths(state, base, head)
        state["rounds"].append(
            {
                "round": number,
                "head": head,
                "base": base,
                "base_ref": pr["base"]["ref"],
                "base_tip": tip,
                "panel": list(assigned(self.config, "pr_reviewers")),
                "request": request["delivery"],
                "requested_by": dict(request["actor"]),
                "requested_at": request["at"],
                "opened": now(),
            }
        )
        state["requests"].append(request["delivery"])

    def consume_requests(self, events):
        """Apply request events not yet seen; return whether one changed the task.

        A request during unfinished work joins that round. A removal by anyone but the
        reviewer account stops further admissions; the reviewer's own submission also clears
        the request, which is not a cancellation.
        """
        state, changed = self.state, False
        record = current_round(state)
        reviewer = review_rules.publisher(state, self.config)
        for event in events:
            if event["delivery"] in state["requests"]:
                continue
            state["requests"].append(event["delivery"])
            changed = True
            if event["action"] == "requested":
                if not self.authorized(event):
                    continue
                if state["stage"] == "complete":
                    self.open_round(event)
                    record = current_round(state)
                elif state["stage"] != "closed":
                    record.setdefault("joined", []).append(event["delivery"])
            elif self.unfinished() and not reviewer.rest(event["actor"]):
                record["cancelled"] = {"by": dict(event["actor"]), "at": event["at"], "request": event["delivery"]}
        if changed:
            self.save()
        return changed

    def unfinished(self):
        """Whether the current round is still open: running, or paused for a writer or credentials."""
        return self.state["stage"] in ACTIVE or self.state["stage"] in SUSPENDED_STAGES

    def authorized(self, event):
        """Whether a request comes from a current writer of the reviewed repository."""
        return self.permitted(event["actor"]["login"])

    def observe_pull(self):
        """Record closure or reopening and refresh displayed PR metadata; whether the stage changed."""
        state, pr = self.state, self.pull
        closed = pr["state"] != "open"
        if closed and state["stage"] != "closed":
            record = current_round(state)
            if record and "outcome" not in record:
                record.update(outcome="closed", completed=now())
            state.update(
                stage="closed",
                reason="Pull request merged." if pr.get("merged") else "Pull request closed without merging.",
                completion={"merged": bool(pr.get("merged")), "merge_sha": pr.get("merge_commit_sha")},
            )
            state.pop("retry_stage", None)
            self.save()
            return True
        if not closed and state["stage"] == "closed":
            state["stage"] = "complete"
            for key in ("reason", "completion"):
                state.pop(key, None)
            self.save()
            return True
        observed = {
            "head": pr["head"]["sha"],
            "base_ref": pr["base"]["ref"],
            "base_tip": pr["base"]["sha"],
            "title": pr["title"],
            "draft": bool(pr.get("draft")),
        }
        if state.get("observed") != observed:
            # Later pushes, retargets and edits are displayed, never applied to a recorded assessment.
            state["observed"] = observed
            self.checkpoint()
        return False

    # Review execution

    def repository_refs(self, candidate):
        """The frozen merge base under the PR's base branch name, and the frozen head."""
        state = self.state
        base = state["base_ref"] if valid_ref(state["base_ref"]) else BASE_REF
        head = REVIEW_REF.format(pr=state["pr"])
        return {"refs": {base: state["base"], head: state["head"]}, "head": head}

    def context(self, changed=None):
        """Review context without contribution-only policy: there is no plan and no edit envelope."""
        context = super().context(changed)
        for key in ("edit_policy", "limits"):
            context.pop(key, None)
        state = self.state
        context.update(
            review_round=state["round"],
            base_branch=state["base_ref"],
            pull_request_author=(self.pull.get("user") or {}).get("login") if self.pull else None,
        )
        return context

    def published_body(self, head):
        """The description captured with the round's snapshot, not the PR's current one."""
        return self.state["requirements"]["body"]

    def review_files(self):
        """Readable changed files at the frozen merge base and head, and the paths that cannot be rendered."""
        state, before, after, opaque = self.state, {}, {}, []
        for path in state["changed_files"]:
            try:
                old = self.files(state["base"], [path], inspection=True)[path]
                new = self.files(state["head"], [path], inspection=True)[path]
            except (Blocked, UnicodeError):
                opaque.append(path)
                continue
            before[path], after[path] = old, new
        return before, after, opaque

    def review_context(self, provider):
        """One reviewer's context for the frozen change."""
        before, after, opaque = self.review_files()
        context = self.pr_review_context(provider, before, after, self.state["head"])
        if opaque:
            context["unrendered_changes"] = [{"path": path, "note": UNRENDERED} for path in opaque]
        if "workflow_and_policy_review" in context:
            context["workflow_and_policy_review"] = (
                "Review changes to CI, workflows and policy documents explicitly, including whether the required "
                "checks still validate the change."
            )
        return context

    def record_pr_review(self, state, provider, report):
        """Apply a validated review to the ledger and queue its publication against the frozen head."""
        sequence, identifiers = review_rules.apply(state, provider, report, state["head"], note=FINDING_NOTE)
        report.update(self.agent_fields(provider, "review"))
        report.update(
            head=state["head"],
            model=selected_model(self.config, provider, "review") or "CLI default",
            input_revision=state["input_revision"],
            review_binding=digest(review_rules.binding(state, self.config, provider)),
            sequence=sequence,
            finding_ids=identifiers,
            round=state["round"],
        )
        state["reviews"][provider] = report
        review_rules.queue_review(state, provider, report, {"round": state["round"], "base": state["base"]})

    def authorize(self):
        """Model work needs the configuration and instructions the task last adopted."""
        if not bundle_resume.config_current(self.state, config_digest(self.config)):
            raise Blocked(CONFIG_REASON)
        self.require_current_bundle()

    def _validate_authority(self, comments, stage=None):
        """Whether paused review work may continue: there is no plan or published head to revalidate."""
        self.authorize()
        return True

    def admit(self):
        """Start the queued round."""
        self.authorize()
        current_round(self.state)["started"] = now()
        self.state["stage"] = "review"
        self.save()

    def review(self):
        """Ask the next panel member of the round, each independently against the same snapshot."""
        state, record = self.state, current_round(self.state)
        provider = next((p for p in record["panel"] if p not in state["reviews"]), None)
        if provider is None or record.get("cancelled"):
            state["stage"] = "publishing"
            self.save()
            return
        self.authorize()
        context = self.review_context(provider)
        report = self.ask(provider, "review", context)
        if isinstance(report, dict) and report.get("verdict") == "reopen":
            raise Blocked("a standalone review cannot reopen a design; report design defects as findings")
        # Without a plan to reopen, any of the reviewer's own findings may become inapplicable.
        validate_pr_review(report, [], [finding["id"] for finding in review_rules.owned(state, provider)])
        report = self.classify(report, provider, "review", MODEL_RESPONSE_LIMIT)
        self.record_pr_review(state, provider, report)
        conversation.queue(
            state,
            None,
            source=("reviews", provider),
            kind="review",
            details={
                "head": state["head"],
                "round": state["round"],
                "pr": state["pr"],
                "provider": provider,
                "model": selected_model(self.config, provider, "review") or "CLI default",
                "pr_url": review_checks.pull_url(state, self.config),
                **self.agent_fields(provider, "review"),
            },
        )
        self.save()

    def finish(self):
        """Complete the round once every accepted report and review write has settled."""
        state = self.state
        if self._needs_flush():
            self.save()
        if state.get("pr_outbox") or state.get("conversation", {}).get("pending"):
            return False
        record = current_round(state)
        record.update(outcome="cancelled" if record.get("cancelled") else "complete", completed=now())
        state["stage"] = "complete"
        state.pop("reason", None)
        self.save()

    # Commands

    def acknowledge(self, comment, outcome, effect):
        """Queue the one reply a processed command revision gets, linking its source comment."""
        state = self.state
        state.setdefault("command_receipts", {})[str(comment["id"])] = {
            "revision": digest(comment["body"]),
            "outcome": outcome,
        }
        source = link(
            code(comment["body"].strip()[:80]),
            protocol.comment_url(self.config["repository"], state["issue"], comment["id"]),
        )
        conversation.queue(
            state,
            "",
            f"{source} from {text(comment['user']['login'])}: **{outcome}**. {effect}",
        )

    def funding_outcome(self, comment, command):
        """A funding command's recorded effect, from the reconciled consent sources."""
        if not self.sponsorship:
            return "Rejected", "Provider funding is not configured for this deployment."
        projection = getattr(self, "shadow_projection", None)
        if command.argument is None:
            return "No-op", "Name a provider: " + code("/concurro sponsor openai") + ", for example."
        if not projection or not projection.get("history_complete"):
            return None
        fact = next((f for f in projection["facts"] if f.get("source_id") == comment["id"]), None)
        if fact is None:
            return "Rejected", "It needs current write access to this repository."
        return "Applied", "Recorded for " + text(
            str(command.argument).split()[0]
        ) + "; it applies to future model calls."

    def lifecycle(self, comment, command):
        """Apply stop, retry or resume; return the outcome and its effect."""
        state, kind = self.state, command.kind
        if kind is commands.CommandKind.STOP:
            if not self.unfinished() or state["stage"] == "stopped":
                return "No-op", "No review is running."
            # A round already paused keeps the stage it resumes to.
            self.suspend("stopped", publish_events=False)
            return "Applied", "The review is paused; " + code("/concurro retry") + " continues it."
        if kind is commands.CommandKind.RESUME and command.argument is None:
            if state["stage"] in ("complete", "closed"):
                return "No-op", "No unfinished review round; request the reviewer again for a new one."
            config, bundle = self.resume_target() if self.resume_target else (self.config, self.bundle)
            target = state.get("retry_stage")
            bundle_resume.adopt(self, comment, config, bundle, config_digest(config))
            current_round(state)["panel"] = list(assigned(config, "pr_reviewers"))
            self.reset_allowances()
            if state["stage"] in SUSPENDED_STAGES:
                # New settings supply neither credentials nor budget: the round stays paused, still resumable.
                state["retry_stage"] = target
                return "Applied", "Current configuration and instructions adopted. The round stays paused: " + (
                    review_checks.REASONS[state["stage"]]
                )
            commands.acknowledge_provider_pause(state)
            return "Applied", "Continuing with the current configuration and instructions; findings and spend are kept."
        if kind is commands.CommandKind.RETRY:
            if state["stage"] not in SUSPENDED_STAGES or not state.get("retry_stage"):
                return "No-op", "Nothing is paused; request the reviewer again for a new round."
            state["stage"] = state.pop("retry_stage")
            state.pop("reason", None)
            commands.acknowledge_provider_pause(state)
            self.reset_allowances()
            return "Applied", "Continuing the paused review round."
        return "Rejected", COMMAND_SET

    def reset_allowances(self):
        """A writer renews retry allowances, never spent budgets."""
        self.state.pop("operation", None)
        for item in self.state.get("pr_outbox", []):
            item["attempts"] = 0

    def commands(self, comments):
        """Process each `/concurro` comment revision once, in order; whether a command changed the stage."""
        state, acted = self.state, False
        receipts = state.get("command_receipts", {})
        for comment in comments:
            body = (comment.get("body") or "").strip()
            if not body.startswith("/concurro") or self.identities.ours(comment["user"]["login"]):
                continue
            if receipts.get(str(comment["id"]), {}).get("revision") == digest(comment["body"]):
                continue
            command = commands.parse(body)
            withdrawal = command is not None and command.kind is commands.CommandKind.WITHDRAW
            if not withdrawal and not self.permitted(comment["user"]["login"]):
                continue
            before = state["stage"]
            if command is None:
                result = ("Rejected", COMMAND_SET)
            elif command.kind in FUNDING:
                result = self.funding_outcome(comment, command)
            else:
                result = self.lifecycle(comment, command)
            if result is None:
                # Consent sources are still being reconciled; answer on a later tick.
                continue
            state["command_id"] = max(state["command_id"], comment["id"])
            self.acknowledge(comment, *result)
            self.save()
            if command is not None and command.kind is commands.CommandKind.RESUME and result[0] == "Applied":
                self.resume_config(state)
            acted = acted or state["stage"] != before
            receipts = state["command_receipts"]
        return acted

    # The tick

    def step(self, number):
        """Load the task, apply requests and commands, then advance the round one bounded step."""
        self.issue = self.gh.repo(f"issues/{number}")
        if "pull_request" not in self.issue:
            self.foreign = True
            return False
        self.pull = self.gh.repo(f"pulls/{number}")
        if self.sponsorship:
            self.sponsorship.begin(self)
        comments = self.consumed_comments = self.gh.pages(f"issues/{number}/comments")
        self.load(comments)
        comments = delegation.effective(
            comments, self.delegation_key, self.config["repository"], number, self.identities.author("state")
        )
        state = self.state
        if state and state.get("controller") != self.controller_id:
            self.state, self.foreign = None, True
            return False
        if state and state.get("kind") != STANDALONE_REVIEW:
            raise Blocked(FOREIGN_KIND)
        if state and state.get("bundle_resume") and state.get("config_sha") != self.config.get("config_sha"):
            self.resume_config(state)
            return True
        events = self.requests()
        if not state:
            request = next(
                (event for event in events if event["action"] == "requested" and self.authorized(event)), None
            )
            if request is None or self.pull["state"] != "open":
                # A reviewer-list entry or an unverified request never starts or charges for work.
                return False
            self.initialize(comments, request)
            return True
        if self.sponsorship:
            self.sponsorship.sync(self, self.consumed_comments)
        if self.observe_pull():
            return True
        changed = self.consume_requests(events)
        if self._needs_flush():
            self.save()
        if self.commands(comments):
            return True
        state = self.state
        if self.sponsorship and state["stage"] == "awaiting_credentials":
            self.sponsorship.recover(self, comments)
        if state["stage"] not in ACTIVE:
            return changed
        return getattr(self, STEPS[state["stage"]])() is not False
