"""Project targets, issues and engine checkpoint state into the frontend's `Target` and `Task` shapes.

List views use stage labels only (one GitHub call per target); a task view loads and verifies the
engine's audit checkpoint with `Checkpoints.load`, exactly as the controller does.
"""

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime

from checkpoints import MARKER, Checkpoints
from github import STAGE_LABEL_PREFIX, NotFound
from identities import Identities
from report_store import Cache
from vocab import Stage

from .auth import User
from .github import UserAccess
from .settings import Settings

# Verified immutable report artifacts, shared across requests; the engine scopes entries to their provenance.
REPORTS = Cache()
# Decoded checkpoint states by (repository, issue, checkpoint comment digest), oldest evicted first.
STATES: dict[tuple, dict] = {}
STATE_ENTRIES = 256
# Target configurations by fork, briefly.
CONFIG_SECONDS = 120
CONFIGS: dict[str, tuple[float, tuple[dict, str]]] = {}
# The engine's task branches (`policy.py`): `concurro/issue-<number>-<digest>`.
TASK_BRANCH = re.compile(r"concurro/issue-([1-9][0-9]*)-[0-9a-f]+")
COMMANDS = ("start", "approve", "retry", "resume", "stop", "invalidate")
STAGES = {s.value for s in Stage}
PARKED = {"blocked", "stopped", "rejected", "escalated", "closed", "clarify"}
PIPELINE = ("intake", "plan", "plan_qc", "planned", "implement", "publish", "review", "gates")
DEFAULT_LIMITS = {"max_model_calls": 48, "max_fix_rounds": 2, "max_plan_revisions": 2}


class ReadCheckpoints(Checkpoints):
    """The engine's checkpoint loader without the rendered plan artifact, which only writers compare against."""

    def read(self, number, kind):
        return None if kind == "plan" else super().read(number, kind)

    def discover(self, items, kind):
        return None if kind == "plan" else super().discover(items, kind)


class TaskNotFound(Exception):
    """The target or task isn't configured, enrolled or visible to this user."""


def iso_epoch(stamp) -> str | None:
    if type(stamp) not in (int, float):
        return None
    return datetime.fromtimestamp(stamp, UTC).isoformat().replace("+00:00", "Z")


def command_text(command: str, argument: str | None, state) -> str:
    """The exact `/concurro` comment the engine parses (`commands.parse`)."""
    if command == "approve":
        plan_id = argument or (state() or {}).get("plan_id")
        if not plan_id:
            raise TaskNotFound("this task has no plan to approve")
        return f"/concurro approve {plan_id}"
    if command == "retry" and argument:
        return f"/concurro retry --plan-revisions {int(argument)}"
    return f"/concurro {command}"


class Tasks:
    """Views for one signed-in user; every GitHub read uses their token."""

    def __init__(self, settings: Settings, access: UserAccess, user: User):
        self.settings, self.access, self.user = settings, access, user
        self.configs: dict[str, tuple[dict, str]] = {}
        self.visible: list[tuple] | None = None

    # Targets

    def visible_targets(self) -> list[tuple]:
        """Configured targets whose fork this user can see, with that repository's metadata (once per request)."""
        if self.visible is None:
            self.visible = []
            for target in self.settings.targets:
                fork = self.access.repository(target.fork)
                if fork is not None:
                    self.visible.append((target, fork))
        return self.visible

    def config(self, fork: str) -> tuple[dict, str]:
        """The target configuration, shared briefly across users (visibility is checked separately)."""
        if fork not in self.configs:
            cached = CONFIGS.get(fork.lower())
            if cached and cached[0] > time.monotonic():
                self.configs[fork] = cached[1]
            else:
                try:
                    self.configs[fork] = self.access.config(fork)
                    CONFIGS[fork.lower()] = (time.monotonic() + CONFIG_SECONDS, self.configs[fork])
                except Exception:  # A missing or invalid configuration still lists the target.
                    self.configs[fork] = ({}, "main")
        return self.configs[fork]

    def target_record(self, target, fork: dict) -> dict:
        config, branch = self.config(target.fork)
        owner, repo = target.upstream.split("/")
        roster, profiles = roster_and_profiles(config)
        limits = {k: config.get(k, v) for k, v in DEFAULT_LIMITS.items()}
        return {
            "owner": owner,
            "repo": repo,
            "fork": target.fork,
            "description": fork.get("description") or "",
            "mode": "autonomous" if config.get("autonomous") else "manual",
            "defaultBranch": branch,
            "roster": roster,
            "profiles": profiles,
            "config": "\n".join(f"{k}: {v}" for k, v in limits.items()),
            "writer": bool((fork.get("permissions") or {}).get("push")),
        }

    def find_target(self, repository: str):
        """The configured target an issue repository belongs to, if this user can see it."""
        for target, fork in self.visible_targets():
            if repository.lower() in (r.lower() for r in target.issue_repositories) or repository.lower() in (
                target.upstream.lower(),
                target.fork.lower(),
            ):
                return target, fork
        raise TaskNotFound(f"{repository} is not a Concurro target visible to your account")

    # Lists

    def listed(self, target, record: dict) -> list[dict]:
        tasks = []
        pulls = self.pulls_by_issue(self.publication_repository(target))
        for repository in target.issue_repositories:
            for issue in self.access.task_issues(repository):
                task = summary(repository, issue, record["mode"])
                if task["issue"] in pulls:
                    task["pr"] = pulls[task["issue"]]
                tasks.append(task)
        return sorted(tasks, key=lambda t: t["updatedAt"], reverse=True)

    def task_config(self, target, repository: str) -> dict:
        """The configuration the engine runs a task with (`service.worker.Worker.load`): the target's
        own configuration, bound to the issue repository and the configured upstream and fork."""
        config, _ = self.config(target.fork)
        if not config:
            raise TaskNotFound("the target's configuration can't be read")
        return {**config, "repository": repository, "upstream": target.upstream, "fork": target.fork}

    @staticmethod
    def publication_repository(target) -> str:
        """Where the engine opens pull requests: the configured upstream (`publication.pr_url`)."""
        return target.upstream

    def pulls_by_issue(self, repository: str) -> dict[int, dict]:
        """Recent pull requests from task branches, by issue; an open one wins over closed ones.

        One listing per target, without CI or line counts (a task view loads those).
        """
        try:
            listing = self.access.get(repository, "pulls?state=all&sort=updated&direction=desc&per_page=100")
        except NotFound:
            return {}
        found: dict[int, dict] = {}
        for pull in listing:
            match = TASK_BRANCH.fullmatch((pull.get("head") or {}).get("ref") or "")
            if not match:
                continue
            issue, is_open = int(match[1]), pull.get("state") == "open"
            if issue in found and (found[issue]["open"] or not is_open):
                continue
            found[issue] = {
                "open": is_open,
                "number": pull["number"],
                "title": pull.get("title") or "",
                "branch": pull["head"]["ref"],
                "headSha": (pull["head"].get("sha") or "")[:7],
                "ci": "none",
                "additions": 0,
                "deletions": 0,
                "ready": is_open and not pull.get("draft"),
            }
        return {issue: {k: v for k, v in pull.items() if k != "open"} for issue, pull in found.items()}

    def overview(self) -> dict:
        targets, tasks = [], []
        for target, fork in self.visible_targets():
            record = self.target_record(target, fork)
            targets.append(record)
            tasks += self.listed(target, record)
        tasks.sort(key=lambda t: t["updatedAt"], reverse=True)
        return {"targets": targets, "tasks": tasks, "activity": []}

    def target_view(self, repository: str) -> dict:
        target, fork = self.find_target(repository)
        record = self.target_record(target, fork)
        return {"target": record, "tasks": self.listed(target, record), "runs": []}

    # One task

    def state(self, repository: str, number: int) -> dict | None:
        target, _ = self.find_target(repository)
        config = self.task_config(target, repository)
        github = self.access.engine(repository)
        comments = github.pages(f"issues/{number}/comments")
        author = Identities(config).author("state")
        # The checkpoint comment pins the audit artifact by digest: the same comment means the same state.
        pins = [c["body"] for c in comments if (c.get("user") or {}).get("login") == author and MARKER in c["body"]]
        key = (repository.lower(), number, hashlib.sha256(json.dumps(pins).encode()).hexdigest())
        if pins and key in STATES:
            return deepcopy(STATES[key])
        state = ReadCheckpoints(github, config, number, report_cache=REPORTS).load(comments)
        if pins and state is not None:
            STATES[key] = deepcopy(state)
            while len(STATES) > STATE_ENTRIES:
                STATES.pop(next(iter(STATES)))
        return state

    def task_view(self, repository: str, number: int) -> dict:
        target, fork = self.find_target(repository)
        record = self.target_record(target, fork)
        with ThreadPoolExecutor(2) as pool:
            issue_read = pool.submit(self.access.get, repository, f"issues/{number}")
            state_read = pool.submit(self.state, repository, number)
            try:
                issue = issue_read.result()
            except NotFound:
                raise TaskNotFound(f"{repository}#{number} doesn't exist or isn't visible to you") from None
            state = state_read.result()
        if state is None and not stage_label(issue):
            raise TaskNotFound(f"{repository}#{number} is not enrolled in Concurro")
        config, _ = self.config(target.fork)
        task = summary(repository, issue, record["mode"])
        if state:
            task |= project(state, config, repository, issue)
            if state.get("pr"):
                task["pr"] = self.pull(self.publication_repository(target), state)
        return {"task": task, "target": record}

    def pull(self, upstream: str, state: dict) -> dict:
        number = state["pr"]
        result = {
            "number": number,
            "title": "",
            "branch": state.get("branch") or "",
            "headSha": (state.get("head") or "")[:7],
            "ci": "none",
            "additions": 0,
            "deletions": 0,
            "ready": bool(state.get("handoff_ready")),
        }
        try:
            pull = self.access.get(upstream, f"pulls/{number}")
            result |= {
                "title": pull.get("title") or "",
                "branch": pull["head"]["ref"],
                "headSha": pull["head"]["sha"][:7],
                "additions": pull.get("additions", 0),
                "deletions": pull.get("deletions", 0),
            }
            runs = self.access.get(upstream, f"commits/{pull['head']['sha']}/check-runs?per_page=100")
            result["ci"] = ci(runs.get("check_runs", []))
        except (NotFound, KeyError, TypeError):
            pass
        return result


HARNESS_PROVIDERS = {"claude": "anthropic", "codex": "openai"}
ROLE_KEYS = (
    ("triage", "triage", None),
    ("planner", "planner", "planner"),
    ("plan_reviewer", "plan_reviewers", "qc"),
    ("implementer", "implementer", "implementer"),
    ("pr_reviewer", "pr_reviewers", "reviewers"),
)


def roster_and_profiles(config: dict) -> tuple[list[dict], list[dict]]:
    """Roles and their model profiles, from named `agents` or the legacy per-role harness keys."""
    models = config.get("models") or {}
    agents = config.get("agents")
    roster, profiles = [], {}
    for role, key, legacy in ROLE_KEYS:
        names = (agents.get(key) if isinstance(agents, dict) else config.get(legacy) if legacy else None) or []
        for name in [names] if isinstance(names, str) else names:
            model = models.get(name)
            if isinstance(model, dict):  # Named profile: {provider, model, effort}.
                provider = model.get("provider") or "anthropic"
                profiles[name] = {
                    "name": name,
                    "provider": provider,
                    **{k: model.get(k) or "" for k in ("model", "effort")},
                }
            else:  # Legacy: the name is a harness and `models` maps it to a model ID.
                provider = HARNESS_PROVIDERS.get(name, "openai")
                profiles[name] = {"name": name, "provider": provider, "model": model or name, "effort": ""}
            roster.append({"role": role, "agent": name, "profile": name})
    return roster, list(profiles.values())


def stage_label(issue: dict) -> str | None:
    for label in issue.get("labels", []):
        if label["name"].startswith(STAGE_LABEL_PREFIX):
            stage = label["name"].removeprefix(STAGE_LABEL_PREFIX).replace("-", "_")
            return stage if stage in STAGES else None
    return None


def summary(repository: str, issue: dict, mode: str) -> dict:
    """A task from its issue alone; `project` fills in the checkpoint state."""
    owner, repo = repository.split("/")
    stage = stage_label(issue) or "intake"
    return {
        "owner": owner,
        "repo": repo,
        "issue": issue["number"],
        "title": issue["title"],
        "author": (issue.get("user") or {}).get("login", ""),
        "labels": [label["name"] for label in issue.get("labels", []) if not label["name"].startswith("concurro")],
        "stage": stage,
        "atStage": stage,
        "mode": mode,
        "alert": alert(stage, {}),
        "plans": [],
        "designReview": [],
        "rounds": [],
        "findings": [],
        "budgets": {k: {"used": 0, "max": 0} for k in ("modelCalls", "fixRounds", "planRevisions")},
        "timeline": [],
        "enrolledAt": issue["created_at"],
        "updatedAt": issue["updated_at"],
    }


def project(state: dict, config: dict, repository: str, issue: dict) -> dict:
    stage = state.get("stage") or "intake"
    plans = plan_versions(state, issue["updated_at"])
    task = {
        "stage": stage,
        "atStage": parked_at(state),
        "mode": state.get("mode") or "manual",
        "alert": alert(stage, state),
        "plans": plans,
        "approvedPlanId": (state.get("approval") or {}).get("plan"),
        "designReview": design_reviews(state),
        "rounds": review_rounds(state),
        "findings": findings(state),
        "budgets": {
            "modelCalls": {"used": state.get("calls", 0), "max": config.get("max_model_calls", 48)},
            "fixRounds": {"used": state.get("round", 0), "max": config.get("max_fix_rounds", 2)},
            "planRevisions": {
                "used": state.get("plan_revisions", 0),
                "max": config.get("max_plan_revisions", 2) + state.get("plan_revision_extra", 0),
            },
        },
        "timeline": timeline(state, repository, issue),
    }
    questions = open_questions(state)
    if questions:
        task["question"] = questions
    completion = state.get("completion")
    if isinstance(completion, dict) and completion.get("kind") == "observed_external":
        observed = completion.get("observed_stage") or "review"
        task["completion"] = {
            "kind": "observed_external",
            "mergeSha": completion.get("merge_sha") or "",
            "mergedBy": completion.get("merged_by"),
            "observedStage": observed,
            "handedOff": bool(state.get("handoff_ready")) or observed == "gates",
            "at": issue.get("closed_at") or issue["updated_at"],
        }
    return task


def parked_at(state: dict) -> str:
    stage = state.get("stage") or "intake"
    if stage in PIPELINE:
        return stage
    completion = state.get("completion") or {}
    for candidate in (state.get("retry_stage"), completion.get("observed_stage")):
        if candidate in PIPELINE:
            return candidate
    if stage in ("merged", "observed_merged"):
        return "gates"
    return "plan" if state.get("plan") else "intake"


def alert(stage: str, state: dict) -> dict:
    reason = state.get("reason") or ""
    if stage == "blocked":
        return {"tone": "caution", "title": "Blocked", "body": reason or "The task is blocked; a writer can retry."}
    if stage == "stopped":
        return {"tone": "warning", "title": "Stopped", "body": reason or "A writer stopped this task."}
    if stage in ("rejected", "escalated"):
        return {
            "tone": "warning",
            "title": stage.capitalize(),
            "body": reason or "Reviewers didn't approve the request.",
        }
    if stage == "clarify":
        return {"tone": "warning", "title": "Input needed", "body": open_questions(state) or "Triage asked a question."}
    if stage == "planned":
        return {"tone": "tip", "title": "Plan ready", "body": "A writer can approve the plan to start implementation."}
    if stage in ("merged", "observed_merged"):
        return {"tone": "tip", "title": "Merged", "body": reason or "The pull request was merged."}
    if stage == "closed":
        return {"tone": "note", "title": "Closed", "body": reason or "The issue was closed."}
    return {"tone": "note", "title": "In progress", "body": reason or f"Concurro is working on the {stage} stage."}


def open_questions(state: dict) -> str:
    lines = []
    for request in state.get("requests") or []:
        if request.get("state") == "open":
            lines += [q.get("question", "") for q in request.get("questions", []) if isinstance(q, dict)]
    return "\n".join(filter(None, lines))


def plan_markdown(plan: dict) -> str:
    parts = [plan.get("summary") or ""]
    for title, key in (("Steps", "steps"), ("Files", "files"), ("Tests", "tests"), ("Questions", "questions")):
        items = plan.get(key) or []
        if items:
            parts.append(f"### {title}\n" + "\n".join(f"- {item_text(item)}" for item in items))
    return "\n\n".join(p for p in parts if p)


def item_text(item) -> str:
    if isinstance(item, dict):
        return item.get("path") or item.get("description") or item.get("text") or item.get("summary") or str(item)
    return str(item)


def plan_versions(state: dict, at: str) -> list[dict]:
    """Earlier design generations' plans (summaries only), then the current plan in full."""
    versions = []
    for n, entry in enumerate(state.get("design_history") or [], start=1):
        versions.append(
            {
                "id": entry.get("plan") or f"g{n}",
                "version": n,
                "createdAt": at,
                "summary": entry.get("summary", ""),
                "body": entry.get("summary", ""),
            }
        )
    plan = state.get("plan")
    if isinstance(plan, dict):
        versions.append(
            {
                "id": state.get("plan_id") or "current",
                "version": len(versions) + state.get("plan_revisions", 0) + 1,
                "createdAt": at,
                "summary": plan.get("headline") or (plan.get("summary") or "")[:200],
                "body": plan_markdown(plan),
            }
        )
    return versions


def member_label(identity: dict) -> tuple[str, str]:
    """A design reviewer's profile name (legacy identities name only a provider) and model."""
    agent = identity.get("agent")
    name = agent.get("profile") if isinstance(agent, dict) else identity.get("provider")
    profile = " · ".join(filter(None, (identity.get("model"), identity.get("effort"))))
    return name or identity.get("id", "")[:12], profile


def design_reviews(state: dict) -> list[dict]:
    generations = []
    for entry in state.get("design_history") or []:
        reviews = []
        for vote in entry.get("votes") or []:
            identity = vote.get("identity")
            name, profile = member_label(identity) if isinstance(identity, dict) else (str(identity or ""), "")
            reviews.append(
                {"agent": name, "profile": profile, "verdict": vote.get("decision") or "pending", "objections": []}
            )
        generations.append(
            {
                "generation": entry.get("generation", len(generations) + 1),
                "planId": entry.get("plan") or "",
                "at": "",
                "reviews": reviews,
            }
        )
    votes = state.get("design_votes") or {}
    if votes:
        objections = state.get("objections") or []
        reviews = []
        for member, vote in votes.items():
            name, profile = member_label(vote.get("identity") or {})
            mine = [o for o in objections if o.get("reviewer") == member]
            reviews.append(
                {
                    "agent": name,
                    "profile": profile,
                    "verdict": (vote.get("report") or {}).get("decision") or "pending",
                    "objections": [
                        {"id": o["id"], "text": o.get("text", ""), "resolved": bool(o.get("resolved"))} for o in mine
                    ],
                }
            )
        history = state.get("qc_history") or []
        generations.append(
            {
                "generation": state.get("design_generation", len(generations) + 1),
                "planId": state.get("plan_id") or "",
                "at": history[-1].get("at", "") if history else "",
                "reviews": reviews,
            }
        )
    return generations


def review_rounds(state: dict) -> list[dict]:
    rounds = []
    for n, reviews in enumerate([*(state.get("review_history") or []), state.get("reviews") or {}], start=1):
        if not reviews:
            continue
        votes, head = [], ""
        for provider, report in reviews.items():
            agent = report.get("agent")
            name = agent.get("profile") if isinstance(agent, dict) else provider
            votes.append(
                {"agent": name, "profile": report.get("model") or "", "verdict": report.get("verdict") or "pending"}
            )
            head = head or (report.get("head") or "")[:7]
        rounds.append({"round": n, "headSha": head, "at": "", "votes": votes})
    return rounds


def findings(state: dict) -> list[dict]:
    sequences = {}
    for n, reviews in enumerate([*(state.get("review_history") or []), state.get("reviews") or {}], start=1):
        for report in (reviews or {}).values():
            if report.get("sequence") is not None:
                sequences.setdefault(report["sequence"], n)
    result = []
    for finding in state.get("findings") or []:
        detail = finding.get("detail") or ""
        result.append(
            {
                "id": finding.get("id", ""),
                "raisedBy": finding.get("provider", ""),
                "raisedRound": sequences.get(finding.get("review"), 1),
                "status": finding.get("status", "open"),
                "severity": finding.get("severity") or "medium",
                "title": finding.get("title") or detail.split("\n", 1)[0][:120],
                "detail": detail,
                "path": finding.get("path") or "",
                "line": finding.get("line") or finding.get("start_line") or 0,
                "thread": finding.get("thread") or "",
            }
        )
    return result


def timeline(state: dict, repository: str, issue: dict) -> list[dict]:
    events = [
        {
            "id": "enrolled",
            "at": issue["created_at"],
            "kind": "github",
            "actor": (issue.get("user") or {}).get("login", ""),
            "text": "Issue opened",
        }
    ]
    for n, report in enumerate(state.get("reports") or [], start=1):
        timing = report.get("timing") or {}
        at = iso_epoch(timing.get("finished") or timing.get("started") or timing.get("created"))
        if not at:
            continue
        outcome = report.get("outcome") or ""
        events.append(
            {
                "id": f"report-{n}",
                "at": at,
                "kind": "turn",
                "actor": report.get("provider") or "concurro",
                "text": f"{report.get('step', 'turn').replace('_', ' ')}: {outcome}".strip(": "),
                "tone": "danger" if outcome in ("failed", "rejected") else "neutral",
            }
        )
    for n, entry in enumerate(state.get("qc_history") or [], start=1):
        if entry.get("at"):
            events.append(
                {
                    "id": f"qc-{n}",
                    "at": entry["at"],
                    "kind": "review",
                    "actor": "concurro",
                    "text": f"{entry.get('stage', 'review')} recorded",
                }
            )
    return sorted(events, key=lambda e: e["at"])


def ci(runs: list[dict]) -> str:
    if not runs:
        return "none"
    if any(r.get("status") != "completed" for r in runs):
        return "pending"
    if any(r.get("conclusion") in ("failure", "timed_out", "cancelled", "action_required") for r in runs):
        return "failing"
    return "passing"
