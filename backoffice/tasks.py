"""Project targets, issues and engine checkpoint state into the frontend's `Target` and `Task` shapes.

List views use stage labels. Full task views load the engine checkpoint; section views verify
the same audit identity and restore only the evidence needed by the selected display.
"""

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime

import report_store
from checkpoints import MARKER, CheckpointError, Checkpoints
from github import STAGE_LABEL_PREFIX, NotFound
from identities import Identities
from report_store import Cache
from sponsorship import own_consent, own_consent_present, selected_routes
from storage_helpers import owned
from vocab import Stage

from . import activity
from .auth import User
from .commands import CommandError, IndexUnavailable
from .github import Access
from .read_cache import LISTING
from .runs import Runs
from .settings import Settings

log = logging.getLogger("concurro.backoffice")

# Verified immutable report artifacts, shared across requests; the engine scopes entries to their provenance.
REPORTS = Cache()
# Decoded states by (repository, issue, checkpoint comment digest, evidence fields), oldest evicted first.
STATES: dict[tuple, dict] = {}
STATE_ENTRIES = 256
# Target configurations by fork, briefly.
CONFIG_SECONDS = 120
CONFIGS: dict[str, tuple[float, tuple[dict, str]]] = {}
# The engine's task branches (`policy.py`): `concurro/issue-<number>-<digest>`.
TASK_BRANCH = re.compile(r"concurro/issue-([1-9][0-9]*)-[0-9a-f]+")
COMMANDS = ("funding", "sponsor", "withdraw", "start", "approve", "retry", "resume", "stop", "invalidate")
STAGES = {s.value for s in Stage}
PARKED = {"blocked", "stopped", "rejected", "escalated", "closed", "clarify"}
PIPELINE = ("intake", "plan", "plan_qc", "planned", "implement", "publish", "review", "gates")
EMPTY_RUNS = {"runs": [], "message": None}
# Evidence roots consumed by each independently loaded issue section.
SECTION_FIELDS = {
    "summary": {"reason", "requests"},
    "intake": {"qc_history", "qc"},
    "plan": {"plan", "design_history"},
    "design": {"design_history", "design_votes", "objections", "qc_history"},
    "implement": set(),
    "review": {"review_history", "reviews", "findings"},
    "handoff": {"review_history", "reviews", "findings"},
    "timeline": {"reports", "qc_history"},
    "pull": set(),
}
# List index states, most to least favourable; a response reports the least favourable it read.
INDEX_STATES = ("current", "catching-up", "unavailable")
INDEX_UNAVAILABLE = "Concurro's list index is unavailable."
DEFAULT_LIMITS = {"max_model_calls": 48, "max_fix_rounds": 2, "max_plan_revisions": 2}


def gone_issue(error: RuntimeError) -> bool:
    """Recognize only GitHub's moved/deleted issue responses, preserving outages and other failures."""
    return isinstance(error, NotFound) or str(error).endswith(("(HTTP 301)", "(HTTP 410)"))


class ReadCheckpoints(Checkpoints):
    """The engine's checkpoint loader without the rendered plan artifact, which only writers compare against."""

    def __init__(self, *args, fields=None, **kwargs):
        """Select display evidence without changing the controller's full-state loader."""
        super().__init__(*args, **kwargs)
        self.fields = fields

    def hydrate(self, state, payload):
        """Verify selected report bodies; display views never consume file inventories."""
        if self.fields is None:
            return super().hydrate(state, payload)
        self.restore_reports(state, payload, fields=self.fields)
        refs = payload.get("report_data", [])
        self.trim(state, refs)

    def trim(self, state, refs=()):
        """Keep section evidence and phase-presence metadata, including detached roots."""
        present = {key for key, value in state.items() if value}
        present.update(ref["path"][0] for ref in refs)
        state.setdefault(
            "_phaseRecords",
            [
                bool(present & {"qc", "qc_history"}),
                bool(present & {"plan", "plan_id", "design_history"}),
                bool(present & {"design_history", "design_votes"}),
                bool(state.get("pr")),
                bool(present & {"review_history", "reviews"}),
                False,
            ],
        )
        for key in report_store.FIELDS - self.fields:
            state.pop(key, None)

    def load(self, comments):
        """Apply the same display projection to legacy inline checkpoints."""
        state = super().load(comments)
        if state is not None and self.fields is not None:
            self.trim(state)
            state["_audit"] = self.reference() if self.audit else None
        return state

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


def command_argument(command: str, argument: str | None, state) -> str | None:
    """The command's argument: the plan to approve (the current one unless given) or a revision grant."""
    if command == "funding":
        if argument not in ("openai org", "openai personal", "anthropic org", "anthropic personal"):
            raise ValueError("unknown provider funding selection")
        return argument
    if command in ("sponsor", "withdraw"):
        if argument not in ("openai", "anthropic") and not (command == "sponsor" and argument is None):
            raise ValueError("unknown sponsorship provider")
        return argument
    if command == "approve":
        plan_id = argument or (state() or {}).get("plan_id")
        if not plan_id:
            raise TaskNotFound("this task has no plan to approve")
        return plan_id
    if command == "retry" and argument:
        return str(int(argument))
    return None


class Tasks:
    """Views for one signed-in upstream writer, read with the Reader App."""

    def __init__(
        self,
        settings: Settings,
        access: Access,
        user: User,
        diagnostics=None,
        read_cache=None,
        sponsorships=None,
        lists=None,
    ):
        """Share cached display reads and live consent projection without loading unrelated evidence.

        `lists` asks the service's list index (`ServiceCommands`); without it saved lists age out.
        """
        self.settings, self.access, self.user = settings, access, user
        self.diagnostics, self.read_cache, self.sponsorships = diagnostics, read_cache, sponsorships
        self.lists, self.index, self.index_guard = lists, None, threading.Lock()
        self.configs: dict[str, tuple[dict, str]] = {}
        self.visible: list[tuple] | None = None
        # Freshness of every saved list read this request served.
        self.reads: list[dict] = []

    # Targets

    def target_access(self, target, *, require_writer=True) -> dict | None:
        """Return fork metadata only for upstream writers who can see both target repositories."""
        if require_writer and not self.access.writable(target.upstream, self.user.login, self.user.id):
            return None
        with ThreadPoolExecutor(len(target.repositories)) as pool:
            seen = dict(zip(target.repositories, pool.map(self.access.repository, target.repositories)))
        return seen[target.fork] if all(seen.values()) else None

    def visible_targets(self) -> list[tuple]:
        """Configured targets this upstream writer may access, with the fork's metadata."""
        if self.visible is None:
            targets = self.settings.targets
            with ThreadPoolExecutor(max(1, len(targets))) as pool:
                forks = list(pool.map(self.target_access, targets))
            self.visible = [(target, fork) for target, fork in zip(targets, forks) if fork is not None]
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
        try:
            requirements = selected_routes(config)
        except (ValueError, KeyError, TypeError):
            requirements = None
        limits = {k: config.get(k, v) for k, v in DEFAULT_LIMITS.items()}
        return {
            "owner": owner,
            "repo": repo,
            "fork": target.fork,
            "issueRepositories": list(target.issue_repositories),
            "description": fork.get("description") or "",
            "mode": "autonomous" if config.get("autonomous") else "manual",
            "defaultBranch": branch,
            "roster": roster,
            "credentialRequirements": requirements,
            "profiles": profiles,
            "config": "\n".join(f"{k}: {v}" for k, v in limits.items()),
            "writer": self.access.writable(target.upstream, self.user.login, self.user.id),
        }

    def find_target(self, repository: str, *, require_writer=True):
        """The configured target an issue repository belongs to, if this user is an upstream writer."""
        for target in self.settings.targets:
            if repository.lower() in (r.lower() for r in target.issue_repositories) or repository.lower() in (
                target.upstream.lower(),
                target.fork.lower(),
            ):
                fork = self.target_access(target, require_writer=require_writer)
                if fork is not None:
                    return target, fork
        raise TaskNotFound(f"{repository} is not a Concurro target visible to your account")

    # Lists

    def list_state(self, repository: str, kind: str) -> dict | None:
        """The index's entry for one list, or None with the index disabled; asked once per request.

        An entry this backoffice cannot use, malformed or in a mode it does not serve, is unavailable.
        """
        if self.lists is None:
            return None
        with self.index_guard:
            if self.index is None:
                try:
                    self.index = self.lists.list_revisions(
                        sorted({r for target in self.settings.targets for r in target.repositories})
                    )
                except IndexUnavailable as error:
                    self.index = error
        if isinstance(self.index, IndexUnavailable):
            raise self.index
        try:
            entry = self.index[repository][kind]
            usable = (
                isinstance(entry["revision"], str)
                and entry["mode"] == "signals"
                and entry["state"] in INDEX_STATES[:2]
                and isinstance(entry["verified"], str | None)
            )
        except (KeyError, TypeError):
            usable = False
        if not usable:
            raise IndexUnavailable(f"the list index does not usably report {kind}")
        return entry

    def listing(self, repository: str, name: str, load, incomplete=None):
        """One list read: saved data at once and a background refresh when due, or GitHub directly.

        With the list index a saved list is due when its kind's revision moved, and nothing is
        read from GitHub while the index cannot be asked. `incomplete(items)` says a read came
        back without everything it should hold: it then never replaces a complete saved copy
        and is retried, instead of being kept as the list.
        """

        def read():
            log.info("list read: %s of %s", name.partition(":")[0], repository)
            return load()

        def direct():
            """Without a usable cache: GitHub, unless the index that decides freshness is away."""
            if "saved" in freshness:
                raise IndexUnavailable("the list index is unavailable and nothing is saved")
            return read()

        def saved_read():
            items = read()
            return {"items": items, "degraded": bool(incomplete and incomplete(items))}

        kind = name.partition(":")[0]
        entry, freshness = None, {}
        try:
            entry = self.list_state(repository, "issues" if kind == "issue" else kind)
            if entry:
                freshness = {"revision": entry["revision"]}
        except IndexUnavailable:
            freshness = {"saved": INDEX_UNAVAILABLE}
        if self.read_cache is None:
            return direct()
        scope = hashlib.sha256(json.dumps([1, "listing", self.settings.reader.app_id]).encode()).hexdigest()
        try:
            saved, status = self.read_cache.read(scope, repository, LISTING + name, saved_read, **freshness)
        except LookupError:
            raise IndexUnavailable("nothing is saved for this list") from None
        except (sqlite3.Error, OSError):
            log.warning("list read cache unavailable")
            return direct()
        if self.lists is not None:
            status |= {"state": entry["state"] if entry else "unavailable", "verified": entry and entry["verified"]}
        self.reads.append(status)
        return saved["items"]

    def freshness(self) -> dict:
        """The response's `cache` entry: its oldest saved list read and whether any is being refreshed."""
        if not self.reads:
            return {}
        return {
            "cache": {
                "observedAt": min(read["observedAt"] for read in self.reads),
                "stale": any(read["stale"] for read in self.reads),
                "refreshing": any(read["refreshing"] for read in self.reads),
                "pendingCommand": any(read["pendingCommand"] for read in self.reads),
                "error": next((read["error"] for read in self.reads if read["error"]), None),
                **self.index_freshness(),
            }
        }

    def index_freshness(self) -> dict:
        """The least favourable list index state among the lists read, and their oldest verification."""
        states = [read["state"] for read in self.reads if "state" in read]
        if not states:
            return {}
        verified = [read["verified"] for read in self.reads if read.get("verified")]
        return {"state": max(states, key=INDEX_STATES.index), "verified": min(verified, default=None)}

    def projections(self, repository: str) -> list[dict] | None:
        """The service's issue sponsorships as this user may see them; None when it can't supply them."""
        if not self.sponsorships:
            return []

        def load():
            try:
                return self.sponsorships(repository).get("issues", [])
            except CommandError:
                return None

        return self.listing(repository, f"sponsorships:{self.user.id}", load, incomplete=lambda found: found is None)

    def sponsored_issue(self, repository: str, number: int) -> dict | None:
        """An issue known only from its sponsorship, unless GitHub moved or deleted it."""

        def load():
            try:
                return self.access.get(repository, f"issues/{number}")
            except RuntimeError as error:
                if not gone_issue(error):
                    raise
                return None

        return self.listing(repository, f"issue:{number}", load)

    def listed(self, target, record: dict) -> tuple[list[dict], list[dict]]:
        """A target's tasks and its sponsored issues awaiting enrollment, from concurrent reads."""
        tasks, intents = [], []
        repositories = target.issue_repositories
        with ThreadPoolExecutor(2 * len(repositories) + 1) as pool:
            pulls_read = pool.submit(self.pulls_by_issue, self.publication_repository(target))
            issue_reads = [
                pool.submit(self.listing, repository, "issues", lambda r=repository: self.access.task_issues(r))
                for repository in repositories
            ]
            projection_reads = [pool.submit(self.projections, repository) for repository in repositories]
            pulls = pulls_read.result()
            for repository, issue_read, projection_read in zip(repositories, issue_reads, projection_reads):
                issues, found = issue_read.result(), projection_read.result()
                if found is None:
                    record["sponsorshipStatus"] = "unavailable"
                projections = {item["issue"]: item for item in found or []}
                missing = sorted(projections.keys() - {i["number"] for i in issues})
                issues += filter(None, pool.map(lambda n, r=repository: self.sponsored_issue(r, n), missing))
                for issue in issues:
                    task = summary(repository, issue, record["mode"])
                    if issue["number"] in projections:
                        task["sponsorships"] = projections[issue["number"]]
                        if (
                            not stage_label(issue)
                            and "concurro" not in {label["name"] for label in issue.get("labels", [])}
                            and not projections[issue["number"]].get("task")
                        ):
                            intents.append(
                                {
                                    key: task[key]
                                    for key in (
                                        "owner",
                                        "repo",
                                        "issue",
                                        "title",
                                        "author",
                                        "labels",
                                        "updatedAt",
                                        "sponsorships",
                                    )
                                }
                            )
                            continue
                    if task["issue"] in pulls:
                        task["pr"] = pulls[task["issue"]]
                    tasks.append(task)
        return sorted(tasks, key=lambda t: t["updatedAt"], reverse=True), intents

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
        """Recent pull requests from task branches, by issue; an open one wins over closed ones."""
        try:
            listing = self.listing(
                repository,
                "pulls",
                lambda: self.access.pull_requests(repository),
                # The REST fallback lists pull requests without their line counts or checks.
                incomplete=lambda pulls: any(pull["runs"] is None for pull in pulls),
            )
        except NotFound:
            return {}
        found: dict[int, dict] = {}
        for pull in listing:
            match = TASK_BRANCH.fullmatch(pull["ref"])
            if not match:
                continue
            issue = int(match[1])
            if issue in found and (found[issue]["open"] or not pull["open"]):
                continue
            found[issue] = {
                "open": pull["open"],
                "number": pull["number"],
                "title": pull["title"],
                "branch": pull["ref"],
                "headSha": pull["sha"][:7],
                "ci": ci(pull["runs"]) if pull["runs"] is not None else "none",
                "additions": pull["additions"],
                "deletions": pull["deletions"],
                "ready": pull["open"] and not pull["draft"],
            }
        return {issue: {k: v for k, v in pull.items() if k != "open"} for issue, pull in found.items()}

    def overview(self) -> dict:
        visible = self.visible_targets()

        def view(entry):
            record = self.target_record(*entry)
            return record, *self.listed(entry[0], record)

        with ThreadPoolExecutor(max(1, len(visible))) as pool:
            views = list(pool.map(view, visible))
        repositories = [r for target, _ in visible for r in target.issue_repositories]
        return {
            "targets": [record for record, _, _ in views],
            "tasks": sorted((t for _, tasks, _ in views for t in tasks), key=lambda t: t["updatedAt"], reverse=True),
            "issues": [intent for _, _, intents in views for intent in intents],
            "activity": activity.feed(self.recorded(repositories)),
            **self.freshness(),
        }

    def target_view(self, repository: str) -> dict:
        target, fork = self.find_target(repository)
        record = self.target_record(target, fork)
        tasks, intents = self.listed(target, record)
        return {"target": record, "tasks": tasks, "issues": intents, **self.freshness()}

    def target_runs(self, repository: str) -> dict:
        """The fork's recent executor runs, attributed to recorded turns; none without diagnostics."""
        target, _ = self.find_target(repository)
        if not self.diagnostics:
            return EMPTY_RUNS
        lookup = self.diagnostics.for_sessions
        reader = Runs(self.access.transport(target.fork), target.fork)
        found = self.listing(
            target.fork,
            "runs",
            lambda: reader.for_target(lambda sessions: lookup(target.issue_repositories, sessions)),
            # A message says the listing or a rerun's attempt could not be read.
            incomplete=lambda found: bool(found["message"]),
        )
        return found | self.freshness()

    def recorded(self, repositories, limit: int = activity.ACTIVITY_ITEMS) -> list:
        """The newest recorded turns of these repositories; none without a diagnostics directory."""
        return self.diagnostics.recent(repositories, limit) if self.diagnostics and repositories else []

    # One task

    def state(
        self, repository: str, number: int, section: str | None = None, *, comments=None, config=None, refresh=False
    ) -> dict | None:
        """Load verified evidence for a full task or one display section."""
        if config is None:
            target, _ = self.find_target(repository)
            config = self.task_config(target, repository)
        github = self.access.engine(repository)
        if comments is None:
            comments = github.pages(f"issues/{number}/comments")
        author = Identities(config).author("state")
        # A cached view retains its verified audit identity; explicit refreshes re-read the mutable audit.
        pins = [c["body"] for c in owned(comments, author, MARKER, "user")]
        fields = None if section is None else frozenset(SECTION_FIELDS["summary"] | SECTION_FIELDS[section])
        key = (repository.lower(), number, hashlib.sha256(json.dumps(pins).encode()).hexdigest(), fields)
        if pins and key in STATES and not refresh:
            return deepcopy(STATES[key])
        state = ReadCheckpoints(github, config, number, report_cache=REPORTS, fields=fields).load(comments)
        if state is not None and section is not None:
            state["_revision"] = hashlib.sha256(
                json.dumps([key[2], state.get("_audit")], sort_keys=True).encode()
            ).hexdigest()
        if pins and state is not None:
            STATES[key] = deepcopy(state)
            while len(STATES) > STATE_ENTRIES:
                STATES.pop(next(iter(STATES)))
        return state

    def issue_view(self, repository: str, number: int, projection=None) -> dict:
        """Read full writer consent or only the caller's own evidence, never checkpoint data."""
        target, fork = self.find_target(repository, require_writer=False)
        issue = self.access.transport(repository, as_user=True).request("GET", f"repos/{repository}/issues/{number}")
        writer = self.access.writable(target.upstream, self.user.login, self.user.id)
        projection = self.sponsorship_view(repository, number) if projection is None else projection
        record = summary(repository, issue, "manual")
        value = {key: record[key] for key in ("owner", "repo", "issue", "title", "author", "labels", "updatedAt")}
        value["sponsorships"] = projection if writer else own_consent(projection, str(self.user.id))
        if writer:
            value["body"] = issue.get("body") or ""
        return {
            "issue": value,
            "issueWriter": writer,
            **({"target": self.target_record(target, fork)} if writer else {}),
        }

    def sponsorship_view(self, repository: str, number: int) -> dict:
        """Keep unavailable source confirmation visible instead of inferring an empty provider slot."""
        if self.sponsorships:
            try:
                return self.sponsorships(repository, number)
            except CommandError:
                pass
        return {"funding": "unknown", "status": "unavailable", "slots": {}}

    def my_sponsorships(self) -> list[dict]:
        """Discover current and historical own consent even after upstream writer loss."""
        overview = self.overview()
        actor = str(self.user.id)
        result = [
            item
            for item in overview["tasks"] + overview["issues"]
            if own_consent_present(own_consent(item.get("sponsorships", {}), actor))
        ]
        if self.sponsorships:
            for target in self.settings.targets:
                if (
                    self.access.writable(target.upstream, self.user.login, self.user.id)
                    or self.target_access(target, require_writer=False) is None
                ):
                    continue
                for repository in target.issue_repositories:
                    for projection in self.sponsorships(repository).get("issues", []):
                        own = own_consent(projection, actor)
                        if not own_consent_present(own):
                            continue
                        try:
                            result.append(self.issue_view(repository, projection["issue"], projection)["issue"])
                        except RuntimeError as error:
                            if not gone_issue(error):
                                raise
        return result

    def snapshot(self, repository, number, config):
        """Fetch issue metadata and only owned checkpoint comments, then verify the lightweight state."""
        with ThreadPoolExecutor(2) as pool:
            issue_read = pool.submit(self.access.get, repository, f"issues/{number}")
            comments = self.access.engine(repository).pages(f"issues/{number}/comments")
            issue = issue_read.result()
        author = Identities(config).author("state")
        comments = owned(comments, author, MARKER, "user")
        state = self.state(repository, number, "summary", comments=comments, config=config, refresh=True)
        return {"issue": issue, "comments": comments, "state": state}

    def task_view(self, repository: str, number: int, section: str | None = None) -> dict:
        """Read a full legacy task or one independently rendered section."""
        target, fork = self.find_target(repository)
        self.config(target.fork)
        cache_status = None
        if self.read_cache is not None and section is not None:
            config = self.task_config(target, repository)
            scope = hashlib.sha256(
                json.dumps([1, self.settings.reader.app_id, config], sort_keys=True).encode()
            ).hexdigest()
            load = lambda: self.snapshot(repository, number, config)
            try:
                snapshot, cache_status = self.read_cache.read(scope, repository, number, load)
                if SECTION_FIELDS[section] <= SECTION_FIELDS["summary"]:
                    state = snapshot["state"]
                else:
                    try:
                        state = self.state(repository, number, section, comments=snapshot["comments"], config=config)
                        if (state or {}).get("_audit") != (snapshot["state"] or {}).get("_audit") and not cache_status[
                            "error"
                        ]:
                            state = self.state(
                                repository, number, section, comments=snapshot["comments"], config=config, refresh=True
                            )
                        if (state or {}).get("_audit") != (snapshot["state"] or {}).get("_audit"):
                            raise CheckpointError("concurrent_change")
                    except CheckpointError:
                        if cache_status["error"]:
                            raise
                        # The audit artifact can advance before a cached checkpoint pointer is refreshed.
                        snapshot, cache_status = self.read_cache.read(scope, repository, number, load, refresh=True)
                        if cache_status["error"]:
                            raise
                        state = self.state(
                            repository, number, section, comments=snapshot["comments"], config=config, refresh=True
                        )
                        if (state or {}).get("_audit") != (snapshot["state"] or {}).get("_audit"):
                            raise CheckpointError("concurrent_change")
                issue = snapshot["issue"]
                record = self.target_record(target, fork) if section == "summary" else None
            except (sqlite3.Error, OSError):
                log.warning("issue read cache unavailable; reading GitHub directly")
                cache_status = None
        if cache_status is None:
            with ThreadPoolExecutor(3) as pool:
                issue_read = pool.submit(self.access.get, repository, f"issues/{number}")
                state_read = pool.submit(self.state, repository, number, section)
                record_read = pool.submit(self.target_record, target, fork) if section in (None, "summary") else None
                try:
                    issue = issue_read.result()
                except NotFound:
                    raise TaskNotFound(f"{repository}#{number} doesn't exist or isn't visible to you") from None
                state = state_read.result()
                record = record_read.result() if record_read else None
        if state is None and not stage_label(issue):
            raise TaskNotFound(f"{repository}#{number} is not enrolled in Concurro")
        config, _ = self.config(target.fork)
        task = summary(repository, issue, "autonomous" if config.get("autonomous") else "manual")
        if section in (None, "intake"):
            task["body"] = issue.get("body") or ""
        if state:
            task |= project(state, config, repository, issue, section)
            if state.get("pr"):
                task["pr"] = (
                    self.pull(self.publication_repository(target), state)
                    if section in (None, "pull")
                    else pull_summary(state)
                )
        if section is not None:
            task["revision"] = hashlib.sha256(
                json.dumps([(state or {}).get("_revision"), issue["updated_at"]]).encode()
            ).hexdigest()
        if section in (None, "summary"):
            task["sponsorships"] = self.sponsorship_view(repository, number)
        return {
            "task": task,
            "issueWriter": self.access.writable(target.upstream, self.user.login, self.user.id),
            **({"target": record} if record is not None else {}),
            **({"cache": cache_status} if cache_status else {}),
        }

    def pull(self, upstream: str, state: dict) -> dict:
        number = state["pr"]
        result = pull_summary(state)
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


def pull_summary(state: dict) -> dict:
    """Checkpoint-backed PR identity; live CI and line counts are initially unknown."""
    return {
        "number": state["pr"],
        "title": "",
        "branch": state.get("branch") or "",
        "headSha": (state.get("head") or "")[:7],
        "ci": "unknown",
        "additions": None,
        "deletions": None,
        "ready": bool(state.get("handoff_ready")),
    }


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
        "intake": [],
        "plans": [],
        "designReview": [],
        "rounds": [],
        "findings": [],
        "budgets": {k: {"used": 0, "max": 0} for k in ("modelCalls", "fixRounds", "planRevisions")},
        "timeline": [],
        "enrolledAt": issue["created_at"],
        "updatedAt": issue["updated_at"],
    }


def project(state: dict, config: dict, repository: str, issue: dict, section: str | None = None) -> dict:
    """Project summary metadata and only the requested phase evidence."""
    stage = state.get("stage") or "intake"
    plans = (
        plan_versions(state, issue["updated_at"], metadata_only=section == "design")
        if section in (None, "plan", "design")
        else []
    )
    task = {
        "stage": stage,
        "atStage": parked_at(state),
        "mode": state.get("mode") or "manual",
        "alert": alert(stage, state),
        "intake": intake_rounds(state) if section in (None, "intake") else [],
        "plans": plans,
        "approvedPlanId": (state.get("approval") or {}).get("plan"),
        "designReview": design_reviews(state) if section in (None, "design") else [],
        "rounds": review_rounds(state) if section in (None, "review", "handoff") else [],
        "findings": findings(state) if section in (None, "review", "handoff") else [],
        "budgets": {
            "modelCalls": {"used": state.get("calls", 0), "max": config.get("max_model_calls", 48)},
            "fixRounds": {"used": state.get("round", 0), "max": config.get("max_fix_rounds", 2)},
            "planRevisions": {
                "used": state.get("plan_revisions", 0),
                "max": config.get("max_plan_revisions", 2) + state.get("plan_revision_extra", 0),
            },
        },
        "timeline": timeline(state, repository, issue) if section in (None, "timeline") else [],
    }
    if "_phaseRecords" in state:
        task["phaseRecords"] = state["_phaseRecords"]
    if state.get("plan_id"):
        task["currentPlan"] = {"id": state["plan_id"]}
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
    return "plan" if state.get("plan") or state.get("plan_id") else "intake"


def alert(stage: str, state: dict) -> dict:
    reason = state.get("reason") or ""
    if stage in ("awaiting_credentials", "budget_exhausted"):
        return {
            "tone": "caution",
            "title": "Awaiting credentials" if stage == "awaiting_credentials" else "Budget exhausted",
            "body": reason
            or "Correct the provider condition, then a writer may retry. Completed work and spending are preserved.",
        }
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


def plan_versions(state: dict, at: str, *, metadata_only: bool = False) -> list[dict]:
    """Historical plan summaries and the current plan, or identities for design-review labels."""
    versions = {}
    for n, entry in enumerate(state.get("design_history") or [], start=1):
        plan_id = entry.get("plan") or f"g{n}"
        versions[plan_id] = {
            "id": plan_id,
            "createdAt": at,
            "summary": "" if metadata_only else entry.get("summary", ""),
            "body": "" if metadata_only else entry.get("summary", ""),
        }
    plan = state.get("plan")
    if isinstance(plan, dict) or metadata_only and state.get("plan_id"):
        plan = plan or {}
        plan_id = state.get("plan_id") or "current"
        versions.pop(plan_id, None)
        versions[plan_id] = {
            "id": plan_id,
            "createdAt": at,
            "summary": plan.get("headline") or (plan.get("summary") or "")[:200],
            "body": "" if metadata_only else plan_markdown(plan),
        }
    return [{**plan, "version": n} for n, plan in enumerate(versions.values(), start=1)]


def intake_rounds(state: dict) -> list[dict]:
    """Recorded triage assessments, oldest first; the current one carries its concerns."""
    rounds = []
    for entry in state.get("qc_history") or []:
        if entry.get("stage") != "intake":
            continue
        agent = entry.get("agent") if isinstance(entry.get("agent"), dict) else {}
        name, profile = member_label(
            {"model": entry.get("model"), **agent, "agent": agent or None, "provider": entry.get("provider")}
        )
        rounds.append(
            {
                "agent": name,
                "profile": profile,
                "decision": entry.get("decision") or "",
                "risk": entry.get("risk") or "",
                "rationale": entry.get("rationale") or "",
                "concerns": [],
                "at": entry.get("at") or "",
            }
        )
    current = (state.get("qc") or {}).get("intake")
    if rounds and isinstance(current, dict):
        rounds[-1]["concerns"] = [c for c in current.get("concerns") or [] if isinstance(c, str)]
    return rounds


def member_label(identity: dict) -> tuple[str, str]:
    """A design reviewer's profile name (legacy identities name only a provider) and model."""
    agent = identity.get("agent")
    name = agent.get("profile") if isinstance(agent, dict) else identity.get("provider")
    profile = " · ".join(filter(None, (identity.get("model"), identity.get("effort"))))
    return name or identity.get("id", "")[:12], profile


def design_reviews(state: dict) -> list[dict]:
    generations = []
    recorded = quality_times(state, "plan_qc")
    for entry in state.get("design_history") or []:
        generation = entry.get("generation", len(generations) + 1)
        reviews = []
        for n, vote in enumerate(entry.get("votes") or [], start=1):
            identity = vote.get("identity")
            name, profile = member_label(identity) if isinstance(identity, dict) else (str(identity or ""), "")
            reviews.append(
                {
                    "agent": name,
                    "profile": profile,
                    "verdict": vote.get("decision") or "pending",
                    # Historical concern text; the ledger's current status for it is not projected.
                    "objections": [
                        {"id": f"g{generation}-{n}-{i}", "text": text}
                        for i, text in enumerate(vote.get("concerns") or [], start=1)
                        if isinstance(text, str) and text
                    ],
                }
            )
        generations.append(
            {
                "generation": generation,
                "planId": entry.get("plan") or "",
                "at": recorded.get(generation + 1, ""),
                "reason": entry.get("reason") or "",
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
        generation = state.get("design_generation", len(generations) + 1)
        history = state.get("qc_history") or []
        generations.append(
            {
                "generation": generation,
                "planId": state.get("plan_id") or "",
                "at": recorded.get(generation + 1) or (history[-1].get("at", "") if history else ""),
                "reason": "",
                "reviews": reviews,
            }
        )
    return generations


def quality_times(state: dict, stage: str) -> dict[int, str]:
    """The last recorded time of each review round; `_quality_history` numbers a round one past its generation."""
    times: dict[int, str] = {}
    for entry in state.get("qc_history") or []:
        if entry.get("stage") == stage and entry.get("at") and type(entry.get("round")) is int:
            times[entry["round"]] = max(entry["at"], times.get(entry["round"], ""))
    return times


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
