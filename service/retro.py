"""Retrospective collection: per-task dossiers and cross-task metrics, rebuilt offline from one snapshot.

Phase 0 of the retrospective tuning proposal. It makes no model calls and writes nothing of its own
outside the snapshot and output directories; reading exchange observations lets the exchange first
import pending native observations, as the service's own reads do. A capture reads GitHub, run
diagnostics and exchange observations once, saving each read. `python3 -m service.retro` rebuilds the
same output from the snapshot alone.

Private stores contribute only allow-listed fields: numbers, opaque identifiers, values from fixed
code sets and names the task's public configuration declares. Prompts, responses, transcripts and
correction prose never enter the snapshot.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import design_audit
from bundle import STAGES
from checkpoints import CheckpointError, artifact_history, legacy_state
from github import NotFound
from model_config import EFFORTS, HARNESSES, LEGACY_ROLES, assigned, default_effort, parse
from policy import Blocked
from report_store import audit_envelope
from reviews import ledger
from vocab import JUDGED_STAGES, STANDALONE_REVIEW, SUSPENDED_STAGES, TERMINAL_STAGES, TurnOutcome

from . import diagnostics
from .github_reads import content, enrolled_issues
from .reconciler import pages
from .target_config import read_remote

FORMAT = 1
META = "snapshot.json"
ROLES = ("triage", "planner", "plan_reviewers", "implementer", "pr_reviewers")
OTHER = "other"
UNKNOWN = "unknown"
# Fixed code sets for strings read from private stores; any other value becomes `other`.
TURN_STATUSES = frozenset({"queued", "running", "done", "lost", "cancelled"})
TURN_OUTCOMES = frozenset(TurnOutcome)
TERMINATIONS = frozenset({"runner_stopped", "turn_timeout", "process_signal"})
CATEGORIES = frozenset(
    {"auth", "quota_exhausted", "rate_limited", "budget_denied", "model_denied", "unavailable", "unknown"}
)
SOURCES = frozenset({"provider", "resolver", "native", "native_gap"})
NATIVE_STATUSES = frozenset({"available", "unavailable", "disabled"})
HARNESS_NAMES = frozenset(HARNESSES.values())
ALL_EFFORTS = frozenset().union(*EFFORTS.values())
# The only diagnostics columns read; requests, prompts, feedback, specs, responses and transcripts are not.
TURN_COLUMNS = (
    "id",
    "generation",
    "stage",
    "provider",
    "model",
    "effort",
    "agent",
    "session_turn",
    "bundle_id",
    "submitted",
    "created",
    "started",
    "finished",
    "status",
    "outcome",
    "termination_reason",
    "elapsed_seconds",
    "runner_sessions",
    "transcript_bytes",
    "evaluation",
)
# Opaque identifiers.
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
DIGEST = re.compile(r"[0-9a-f]{64}")
# Public text kept per human comment, review or writer input.
BODY_CHARACTERS = 2000
LIMITATIONS = [
    "Candidates are facts whose meaning needs interpretation; the collector does not classify them.",
    "Configuration segments come from every saved audit version. A record is attributed to one only when the"
    " version history is complete and its linked turn, or its profile, model, effort and bundle checked against"
    " each segment's configuration, leave exactly one segment; segments adopted after the record existed are"
    " ruled out. Otherwise it is `unknown`, with its candidate segments.",
    "Human means a GitHub `User` account other than Concurro's App and reviewer identities.",
    "A task's population entry reflects its stage at the observation cutoff; paused tasks may continue later.",
    "Missing diagnostics, token usage or timing are unknown, not zero; sums report how many records they cover.",
    "Token usage counts the first observation with usage for each observed inference request. A turn's usage"
    " is complete only when the exchange reported no request awaiting an observation for the task and native"
    " observations were not unavailable; observed totals may undercount.",
    "Turns recorded for operator evaluations are excluded.",
    "Tasks are found by their current `concurro` label; an issue whose label was removed is not listed.",
]


def epoch(value):
    """Seconds since the epoch for an ISO time or a number; None when absent or unreadable."""
    if type(value) in (int, float):
        return float(value)
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()
    return None


def coded(value, allowed):
    """The value when it belongs to its code set, `other` when it doesn't, None when absent."""
    if value is None:
        return None
    return value if value in allowed else OTHER


def identifier(value, pattern=IDENTIFIER):
    """An opaque identifier or declared name, `other` when it doesn't look like one."""
    if value is None:
        return None
    return value if isinstance(value, str) and pattern.fullmatch(value) else OTHER


def number(value):
    """A finite number, else None."""
    return value if type(value) in (int, float) and value == value and abs(value) != float("inf") else None


def declared_names(task_segments):
    """Profile and model names the task's public configurations declare, and whether any is legacy."""
    profiles, models, legacy = set(), set(), False
    for segment in task_segments:
        config = segment["configuration"]
        if config is None:
            continue
        legacy = legacy or config["legacy"]
        for name, profile in config["profiles"].items():
            profiles.add(name)
            models.update(profile["models"])
    return {"profiles": sorted(profiles), "models": sorted(models), "legacy": legacy}


def turn_record(row, declared):
    """The allow-listed fields of one diagnostics row; names the public configuration doesn't declare become `other`."""
    agent = row["agent"]
    try:
        profile = (json.loads(agent) or {}).get("profile") if agent else None
    except (ValueError, AttributeError):
        profile = OTHER
    if agent is None and declared["legacy"]:
        # Legacy requests carry no agent record; their profile is the harness name.
        profile = row["provider"]
    sessions = row["runner_sessions"]
    try:
        staffed = len(json.loads(sessions)) if sessions else 0
    except (ValueError, TypeError):
        staffed = None
    return {
        "id": identifier(row["id"]),
        "generation": number(row["generation"]),
        "stage": coded(row["stage"], STAGES),
        "provider": coded(row["provider"], HARNESS_NAMES),
        "model": coded(row["model"], declared["models"]),
        "effort": coded(row["effort"], ALL_EFFORTS),
        "profile": coded(profile, declared["profiles"]),
        "session_turn": number(row["session_turn"]),
        "bundle_id": identifier(row["bundle_id"], DIGEST),
        "submitted": number(row["submitted"]),
        "created": number(row["created"]),
        "started": number(row["started"]),
        "finished": number(row["finished"]),
        "status": coded(row["status"], TURN_STATUSES),
        "outcome": coded(row["outcome"], TURN_OUTCOMES),
        "termination_reason": coded(row["termination_reason"], TERMINATIONS),
        "elapsed_seconds": number(row["elapsed_seconds"]),
        "runner_sessions": staffed,
        "transcript_bytes": number(row["transcript_bytes"]),
    }


def inference_record(event, declared):
    """The allow-listed fields of one exchange observation."""
    usage = event.get("usage")
    tokens = (
        {k: usage[k] for k in ("input_tokens", "output_tokens")}
        if isinstance(usage, dict)
        and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens", "output_tokens"))
        else None
    )
    return {
        "sequence": number(event.get("sequence")),
        "request": identifier(event.get("request")),
        "turn": identifier(event.get("turn")),
        "source": coded(event.get("source"), SOURCES),
        "category": coded(event.get("category"), CATEGORIES),
        "status": event.get("status") if type(event.get("status")) is int else None,
        "completed": event.get("completed") if type(event.get("completed")) is bool else None,
        "model": coded(event.get("model"), declared["models"]),
        "usage": tokens,
    }


class Live:
    """A capture's sources: GitHub read transports by repository, run diagnostics and the exchange."""

    def __init__(self, readers, diagnostics_root, operator):
        self.readers, self.operator = readers, operator
        self.diagnostics = diagnostics.connect(diagnostics_root, readonly=True) if diagnostics_root else None

    def close(self):
        if self.diagnostics is not None:
            self.diagnostics.close()

    def get(self, path):
        """One GitHub GET through the transport for the path's repository."""
        match = re.match(r"repos/([^/]+/[^/]+)/", path)
        if not match or match[1] not in self.readers:
            raise ValueError("read outside the target's repositories")
        return self.readers[match[1]].request("GET", path)

    def private(self, repository, issue, declared):
        """The task's allow-listed diagnostics rows and exchange observations; None where unavailable.

        `declared` holds the names the task's public configuration declares; nothing else is kept.
        """
        turns = evaluations = None
        if self.diagnostics is not None:
            rows = self.diagnostics.execute(
                f"SELECT {', '.join(TURN_COLUMNS)} FROM turns WHERE repository = ? AND issue = ? ORDER BY submitted, id",
                (repository, issue),
            ).fetchall()
            turns = [turn_record(row, declared) for row in rows if row["evaluation"] is None]
            evaluations = sum(row["evaluation"] is not None for row in rows)
        inference = None
        if self.operator is not None:
            reply = self.operator.observations(repository, issue)
            inference = {
                "events": [inference_record(e, declared) for e in reply.get("events", []) if isinstance(e, dict)],
                "pending": reply.get("pending") if type(reply.get("pending")) is bool else None,
                "native_status": coded(reply.get("native_status"), NATIVE_STATUSES),
            }
        return {"turns": turns, "evaluation_turns": evaluations, "inference": inference}


class Snapshot:
    """Every read a collection makes: saved on first use while live, replayed offline otherwise."""

    def __init__(self, directory, meta=None, live=None):
        """Open an existing snapshot, or create one from `meta` when capturing with `live` sources."""
        self.directory, self.live = Path(directory), live
        path = self.directory / META
        if meta is not None:
            if path.exists():
                raise ValueError("snapshot directory already holds a snapshot")
            self.directory.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"format": FORMAT, **meta}, indent=2, sort_keys=True) + "\n")
        self.meta = json.loads(path.read_text())
        if self.meta.get("format") != FORMAT:
            raise ValueError("unsupported snapshot format")

    def file(self, kind, key):
        return self.directory / kind / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def saved(self, kind, key, read):
        """The saved value for a key, reading and saving it first while live."""
        path = self.file(kind, key)
        if not path.exists():
            if self.live is None:
                raise ValueError(f"{kind} read absent from snapshot: {key}")
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"key": key, "value": read()}, sort_keys=True))
        saved = json.loads(path.read_text())
        if saved["key"] != key:
            raise ValueError("snapshot entry key mismatch")
        return saved["value"]

    def request(self, method, path, data=None):
        """A GitHub reader: GETs only, with 404s saved and replayed as `NotFound`."""
        if method != "GET" or data is not None:
            raise ValueError("retrospective collection only reads")

        def read():
            try:
                return {"response": self.live.get(path)}
            except NotFound:
                return {"not_found": True}

        value = self.saved("github", path, read)
        if value.get("not_found"):
            raise NotFound("GitHub request failed: GET " + path)
        return value["response"]

    def private(self, repository, issue, declared):
        """The task's private records, filtered by its declared names before they are saved."""
        return self.saved("private", f"{repository}#{issue}", lambda: self.live.private(repository, issue, declared))


class Artifacts:
    """One task's issue artifacts, read through the snapshot, as `design_audit` reads them."""

    def __init__(self, snapshot, repository, issue):
        self.snapshot, self.repository, self.issue = snapshot, repository, issue

    def repo(self, path, method="GET", data=None):
        artifact = rf"issues/{self.issue}/artifacts/[1-9][0-9]*(?:\?per_page=[0-9]+&page=[1-9][0-9]*)?"
        if method != "GET" or data is not None or not re.fullmatch(artifact, path):
            raise ValueError("retrospective collection only reads this task's artifacts")
        return self.snapshot.request("GET", f"repos/{self.repository}/{path}")


def human(user, identities):
    """Whether a GitHub account is a person other than Concurro's own identities."""
    return (
        isinstance(user, dict)
        and user.get("type") == "User"
        and str(user.get("login", "")).lower() not in identities["logins"]
        and user.get("id") not in identities["user_ids"]
    )


def body(text):
    return text[:BODY_CHARACTERS] if isinstance(text, str) else None


def configuration(snapshot, fork, sha):
    """Roles and declared profiles of the fork's configuration at one commit; None when unreadable.

    Each profile lists the models it may run: a legacy profile is a harness, with the QC model for QC stages.
    """
    if not sha:
        return None
    try:
        config = read_remote(lambda path: content(snapshot, fork, path, sha))
        legacy = "agents" not in config
        # Legacy configurations name no plan reviewers; the engine derives them.
        roles = {role: assigned(config, role) for role in ROLES if not legacy or role in LEGACY_ROLES}
        if legacy:
            qc = {config["qc_model"]} if isinstance(config.get("qc_model"), str) and config["qc_model"] else set()
            profiles = {
                harness: {
                    "provider": vendor,
                    "models": sorted({config["models"][harness]} | qc),
                    "effort": default_effort(harness),
                }
                for vendor, harness in HARNESSES.items()
                if isinstance(config.get("models", {}).get(harness), str) and config["models"][harness]
            }
        else:
            profiles = {
                p.name: {"provider": p.profile.provider, "models": [p.profile.model], "effort": p.profile.effort}
                for p in parse(config).profiles
            }
    except (Blocked, NotFound, ValueError, KeyError, TypeError, AttributeError):
        return None
    return {
        "legacy": legacy,
        "roles": {k: list(v) if isinstance(v, (list, tuple)) else v for k, v in roles.items()},
        "profiles": profiles,
    }


def audit_history(artifacts, store, author):
    """Every saved audit version's state, oldest first, and whether all of them were read and verified.

    `/concurro invalidate` drops the current resume receipt, so only the version history keeps every
    configuration a task ran under. History pages must match the verified current audit, as checkpoint
    recovery requires, and cover every version from the current one down to the first.
    """
    response = artifacts.repo(f"issues/{artifacts.issue}/artifacts/1")
    latest = response["versions"][0]
    current = {**response["artifact"], "versions": [latest], "revision": latest["version"]}
    try:
        found = list(artifact_history(artifacts.repo, artifacts.issue, current))
    except (CheckpointError, NotFound):
        return [], False
    complete = [v.get("version") for v in found] == list(range(current["revision"], 0, -1))
    states = []
    for version in reversed(found):
        try:
            if (version.get("actor") or {}).get("login") != author:
                raise ValueError("audit version by another account")
            payload = audit_envelope(legacy_state(version["body"]), store, artifacts.repository, artifacts.issue)
            states.append(payload["state"])
        except (ValueError, KeyError, TypeError, IndexError, NotFound):
            complete = False
    return states, complete


def effective(state):
    """The bundle, configuration digest and configuration commit a saved state runs new work under."""
    resume = state.get("bundle_resume") or {}
    return (
        resume.get("new_bundle") or state.get("bundle_id"),
        resume.get("config_digest") or state.get("config_digest"),
        state.get("config_sha"),
    )


def boundary(previous, state):
    """The writer comment that changed the configuration between two saved states, if one is recorded."""
    resume = (state.get("bundle_resume") or {}).get("comment")
    if resume is not None and resume != (previous.get("bundle_resume") or {}).get("comment"):
        return resume
    before, after = previous.get("invalidations"), state.get("invalidations")
    if isinstance(before, list) and isinstance(after, list) and len(after) > len(before):
        return (after[-1] or {}).get("comment")
    return None


def segments(snapshot, states, latest, fork, comment_times):
    """The task's configurations in the order it used them, each starting at the comment that adopted it.

    A configuration is its bundle and configuration digest; its commit is the latest one recorded with it.
    """
    result, previous = [], None
    for state in states or [latest]:
        bundle_id, digest, sha = effective(state)
        if digest is None:
            continue
        if result and result[-1]["key"] == (bundle_id, digest):
            result[-1]["sha"] = sha or result[-1]["sha"]
        else:
            starts = comment_times.get(boundary(previous, state)) if result and previous is not None else None
            result.append({"key": (bundle_id, digest), "sha": sha, "starts": starts})
        previous = state
    return [
        {
            "id": f"S{index + 1}",
            "bundle_id": item["key"][0],
            "config_digest": item["key"][1],
            "config_sha": item["sha"],
            "starts": item["starts"],
            "configuration": configuration(snapshot, fork, item["sha"]),
        }
        for index, item in enumerate(result)
    ]


def consistent(segment, profile, model, effort, bundle):
    """Whether a record's execution evidence fits a segment; unknown evidence never rules one out."""
    if bundle is not None and segment["bundle_id"] is not None and bundle != segment["bundle_id"]:
        return False
    config = segment["configuration"]
    if profile in (None, OTHER) or config is None:
        return True
    declared = config["profiles"].get(profile)
    if declared is None:
        return False
    if model not in (None, OTHER) and model not in declared["models"]:
        return False
    return effort in (None, OTHER) or effort == declared["effort"]


def attribute(history, *, profile=None, model=None, effort=None, bundle=None, before=None):
    """The segment a record ran under, from execution evidence; `unknown` with its candidates otherwise.

    A segment whose resume comment came after `before` (when the record existed) is ruled out, as are later
    ones. Without the complete configuration history no record is attributed.
    """
    segments = history["segments"]
    if not history["complete"]:
        return {"segment": None, "method": "unknown", "candidates": [s["id"] for s in segments]}
    if len(segments) == 1:
        return {"segment": segments[0]["id"], "method": "only"}
    possible = []
    for index, segment in enumerate(segments):
        if index and before is not None and segment["starts"] is not None and segment["starts"] > before:
            break
        if consistent(segment, profile, model, effort, bundle):
            possible.append(segment["id"])
    if len(possible) == 1:
        return {"segment": possible[0], "method": "execution"}
    return {"segment": None, "method": "unknown", "candidates": possible}


def reports(state, history, artifact_url, turn_attribution):
    """The task's indexed reports: step, profile, outcome, correction code and attribution.

    A report linked to a recorded turn takes that turn's segment; otherwise its profile and model decide.
    """
    result, seen = [], set()
    for entry in state.get("reports", []):
        if not isinstance(entry, dict):
            continue
        key = ("event", entry["event"]) if entry.get("event") else ("entry", json.dumps(entry, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        publication = entry.get("publication") or {}
        details = publication.get("details") or {}
        at = publication.get("at")
        turn = (entry.get("timing") or {}).get("turn")
        record = {
            "id": f"R{publication.get('sequence', len(result) + 1)}",
            "step": entry.get("step"),
            "profile": entry.get("provider"),
            "model": details.get("model"),
            "round": entry.get("round"),
            "run": entry.get("run"),
            "outcome": entry.get("outcome"),
            "correction_code": (entry.get("diagnostic") or {}).get("code"),
            "head": details.get("head"),
            "at": at,
            "turn": turn,
            "url": artifact_url(entry["artifact"]) if entry.get("artifact") else None,
            "artifact": entry.get("artifact"),
        }
        linked = turn_attribution.get(turn)
        if linked is not None and linked["segment"] is not None:
            record["attribution"] = {"segment": linked["segment"], "method": "turn"}
        else:
            record["attribution"] = attribute(
                history, profile=record["profile"], model=record["model"], before=epoch(at)
            )
        result.append(record)
    return result


def reviewed(artifacts, store, state, task_reports):
    """From verified code-review reports: each finding's dispositions and the review that raised it."""
    history, raised, unreadable = {}, {}, 0
    entries = {e.get("artifact"): e for e in state.get("reports", []) if isinstance(e, dict)}
    for record in task_reports:
        if record["step"] != "Code review" or record["artifact"] is None:
            continue
        try:
            report = design_audit.archived_report(artifacts, store, record["artifact"], entries.get(record["artifact"]))
        except (ValueError, KeyError, TypeError, RuntimeError):
            unreadable += 1
            continue
        for identifier_ in report.get("finding_ids", []) or []:
            raised[identifier_] = record
        if type(report.get("sequence")) is int:
            raised[(record["profile"], report["sequence"])] = record
        for item in report.get("dispositions", []):
            if isinstance(item, dict) and item.get("id"):
                history.setdefault(item["id"], []).append(
                    {
                        "status": item.get("status"),
                        "report": record["id"],
                        "profile": record["profile"],
                        "at": record["at"],
                    }
                )
    return history, raised, unreadable


def findings(state, history, raised):
    """The finding ledger with each finding's dispositions and the review that raised it."""
    result = []
    for finding in ledger(state):
        origin = raised.get(finding.get("id")) or raised.get((finding.get("provider"), finding.get("review")))
        result.append(
            {
                **{
                    k: finding.get(k)
                    for k in (
                        "id",
                        "provider",
                        "severity",
                        "title",
                        "path",
                        "start_line",
                        "line",
                        "head",
                        "review",
                        "status",
                        "report_url",
                    )
                },
                "dispositions": history.get(finding.get("id"), []),
                "raised_in": origin["id"] if origin else None,
                "attribution": origin["attribution"] if origin else {"segment": None, "method": "unknown"},
            }
        )
    return result


def turns(private, history):
    """Allow-listed turns with observed token usage and the segment each ran under.

    A turn's usage is complete only when every observed request has usage, the exchange reports no
    request still awaiting an observation and native observations were not unavailable.
    """
    if private["turns"] is None:
        return None
    inference = private["inference"]
    usage = {}
    if inference is not None:
        first = {}
        for event in sorted(inference["events"], key=lambda e: e["sequence"] or 0):
            if event["turn"] and event["request"]:
                request = first.setdefault((event["turn"], event["request"]), {"usage": None})
                if request["usage"] is None and event["usage"] is not None:
                    request["usage"] = event["usage"]
        for (turn, _), request in first.items():
            total = usage.setdefault(
                turn, {"input_tokens": 0, "output_tokens": 0, "observed_requests": 0, "observed_with_usage": 0}
            )
            total["observed_requests"] += 1
            if request["usage"] is not None:
                total["observed_with_usage"] += 1
                total["input_tokens"] += request["usage"]["input_tokens"]
                total["output_tokens"] += request["usage"]["output_tokens"]
    settled = inference is not None and inference["pending"] is False and inference["native_status"] != "unavailable"
    result = []
    for turn in private["turns"]:
        record = dict(turn)
        tokens = usage.get(turn["id"]) if inference is not None else None
        record["tokens"] = tokens
        record["usage_complete"] = (
            None
            if inference is None
            else bool(settled and tokens and tokens["observed_requests"] == tokens["observed_with_usage"])
        )
        record["attribution"] = attribute(
            history,
            profile=turn["profile"],
            model=turn["model"],
            effort=turn["effort"],
            bundle=turn["bundle_id"],
            before=turn["submitted"],
        )
        result.append(record)
    return result


def pull_facts(snapshot, upstream, number_, identities, controller_head):
    """The PR's lifecycle, its commits and every human review, comment and commit on it."""
    if not number_:
        return None
    try:
        pull = snapshot.request("GET", f"repos/{upstream}/pulls/{number_}")
    except NotFound:
        return {"number": number_, "available": False}
    commits = pages(snapshot, f"repos/{upstream}/pulls/{number_}/commits")
    shas = [c.get("sha") for c in commits]
    head_index = shas.index(controller_head) if controller_head in shas else None
    human_commits = []
    for index, commit in enumerate(commits):
        if not (human(commit.get("author"), identities) or human(commit.get("committer"), identities)):
            continue
        detail = snapshot.request("GET", f"repos/{upstream}/commits/{commit['sha']}")
        human_commits.append(
            {
                "sha": commit["sha"],
                "author": (commit.get("author") or {}).get("login"),
                "at": ((commit.get("commit") or {}).get("committer") or {}).get("date"),
                "message": body(((commit.get("commit") or {}).get("message") or "").split("\n", 1)[0]),
                "files": [f.get("filename") for f in detail.get("files", [])],
                "url": commit.get("html_url"),
                "after_controller_head": None if head_index is None else index > head_index,
            }
        )
    inline = pages(snapshot, f"repos/{upstream}/pulls/{number_}/comments")
    reviews = pages(snapshot, f"repos/{upstream}/pulls/{number_}/reviews")
    conversation = pages(snapshot, f"repos/{upstream}/issues/{number_}/comments")
    human_reviews = [
        {
            "kind": "inline_comment",
            "id": c.get("id"),
            "author": c["user"]["login"],
            "path": c.get("path"),
            "start_line": c.get("start_line"),
            "line": c.get("line") or c.get("original_line"),
            "at": c.get("created_at"),
            "url": c.get("html_url"),
            "body": body(c.get("body")),
        }
        for c in inline
        if human(c.get("user"), identities)
    ]
    human_reviews += [
        {
            "kind": "review",
            "id": r.get("id"),
            "author": r["user"]["login"],
            "state": r.get("state"),
            "at": r.get("submitted_at"),
            "url": r.get("html_url"),
            "body": body(r.get("body")),
        }
        for r in reviews
        if human(r.get("user"), identities) and (r.get("body") or r.get("state") != "COMMENTED")
    ]
    human_reviews += [
        {
            "kind": "comment",
            "id": c.get("id"),
            "author": c["user"]["login"],
            "at": c.get("created_at"),
            "url": c.get("html_url"),
            "body": body(c.get("body")),
        }
        for c in conversation
        if human(c.get("user"), identities)
    ]
    return {
        "number": number_,
        "available": True,
        "url": pull.get("html_url"),
        "state": pull.get("state"),
        "draft": pull.get("draft"),
        "merged": pull.get("merged"),
        "merged_at": pull.get("merged_at"),
        "closed_at": pull.get("closed_at"),
        "head": (pull.get("head") or {}).get("sha"),
        "controller_head": controller_head,
        "commits": len(commits),
        "human_commits": human_commits,
        "human_reviews": human_reviews,
    }


def all_approved_at(task_reports, head, reviewers):
    """When every PR reviewer's latest review of the final head approved it; None when they didn't."""
    latest = {}
    for record in task_reports:
        if record["step"] == "Code review" and record["head"] == head:
            latest[record["profile"]] = record
    if not latest or (reviewers and set(reviewers) - latest.keys()):
        return None
    if any(r["outcome"] != "approve" for r in latest.values()):
        return None
    times = [epoch(r["at"]) for r in latest.values()]
    return None if None in times else max(times)


def candidates(task_findings, pull, approved):
    """Facts whose meaning needs interpretation: human changes after approval and overlapping findings."""
    result = []

    def after(at):
        stamp = epoch(at)
        return None if approved is None or stamp is None else stamp > approved

    for commit in (pull or {}).get("human_commits", []):
        result.append(
            {
                "kind": "human_commit",
                "ref": commit["sha"],
                "after_all_approved": after(commit["at"]),
                "after_controller_head": commit["after_controller_head"],
                "paths": commit["files"],
            }
        )
    for review in (pull or {}).get("human_reviews", []):
        result.append(
            {
                "kind": "human_" + review["kind"],
                "ref": review["url"],
                "after_all_approved": after(review["at"]),
                "paths": [review["path"]] if review.get("path") else [],
            }
        )
    for index, first in enumerate(task_findings):
        for second in task_findings[index + 1 :]:
            if (
                first["provider"] != second["provider"]
                and first["path"]
                and first["path"] == second["path"]
                and first["head"] == second["head"]
                and overlaps(first, second)
            ):
                result.append(
                    {"kind": "overlapping_findings", "ref": [first["id"], second["id"]], "paths": [first["path"]]}
                )
    for index, candidate in enumerate(result, 1):
        candidate["id"] = f"C{index}"
    return result


def overlaps(first, second):
    """Whether two findings' line ranges intersect; findings without lines overlap any range."""
    a = (first["start_line"] or first["line"], first["line"] or first["start_line"])
    b = (second["start_line"] or second["line"], second["line"] or second["start_line"])
    if None in a or None in b:
        return True
    return a[0] <= b[1] and b[0] <= a[1]


def writer_inputs(comments, identities):
    """Human comments on the task issue, with any `/concurro` command named."""
    result = []
    for comment in comments:
        if not human(comment.get("user"), identities):
            continue
        text = comment.get("body") or ""
        command = re.match(r"\s*/concurro\s+([a-z-]+)", text)
        result.append(
            {
                "id": comment.get("id"),
                "author": comment["user"]["login"],
                "at": comment.get("created_at"),
                "url": comment.get("html_url"),
                "command": command[1] if command else None,
                "body": body(text),
            }
        )
    return result


def membership(stage, pull, issue, since, until):
    """Whether a task belongs to the run's population, and why."""
    if stage in TERMINAL_STAGES:
        if stage == "closed" and not (pull or {}).get("closed_at"):
            ended = issue.get("closed_at")
        else:
            ended = (pull or {}).get("merged_at" if stage != "closed" else "closed_at")
        stamp = epoch(ended)
        if stamp is None:
            return False, "terminal time unknown"
        return (since <= stamp < until), ("terminal in window" if since <= stamp < until else "terminal outside window")
    if stage in SUSPENDED_STAGES or stage in JUDGED_STAGES:
        return True, "suspended or judged"
    return False, "active"


def task(snapshot, issue, repository):
    """One enrolled issue's population entry and, when included, its dossier."""
    meta = snapshot.meta
    identities = {
        "logins": {login.lower() for login in meta["identities"]["logins"]},
        "user_ids": set(meta["identities"]["user_ids"]),
    }
    entry = {"repository": repository, "issue": issue["number"], "url": issue.get("html_url")}
    artifacts = Artifacts(snapshot, repository, issue["number"])
    try:
        state, store, _ = design_audit.task_state(artifacts, meta["author"])
    except (ValueError, KeyError, TypeError, IndexError, NotFound) as error:
        return {**entry, "included": False, "reason": "no verifiable task state (" + type(error).__name__ + ")"}, None
    stage = state.get("stage")
    entry["stage"] = stage
    if state.get("kind") == STANDALONE_REVIEW:
        return {**entry, "included": False, "reason": "standalone review"}, None
    upstream = state.get("upstream") or meta["target"]["upstream"]
    fork = state.get("fork") or meta["target"]["fork"]
    pull = pull_facts(snapshot, upstream, state.get("pr"), identities, state.get("head"))
    included, reason = membership(stage, pull, issue, epoch(meta["since"]), epoch(meta["until"]))
    entry.update(included=included, reason=reason)
    if not included:
        return entry, None
    comments = pages(snapshot, f"repos/{repository}/issues/{issue['number']}/comments")
    comment_times = {c.get("id"): epoch(c.get("created_at")) for c in comments}
    states, complete = audit_history(artifacts, store, meta["author"])
    task_segments = segments(snapshot, states, state, fork, comment_times)
    history = {"segments": task_segments, "complete": complete}
    private = snapshot.private(repository, issue["number"], declared_names(task_segments))
    task_turns = turns(private, history)
    root = f"https://github.com/{repository}/issues/{issue['number']}/artifacts/"
    task_reports = reports(
        state,
        history,
        lambda number_: root + str(number_),
        {turn["id"]: turn["attribution"] for turn in task_turns or []},
    )
    history, raised, unreadable = reviewed(artifacts, store, state, task_reports)
    task_findings = findings(state, history, raised)
    final = (task_segments[-1]["configuration"] or {}).get("roles") or {}
    approved = all_approved_at(task_reports, state.get("head"), final.get("pr_reviewers"))
    dossier = {
        "task": {
            "repository": repository,
            "issue": issue["number"],
            "url": issue.get("html_url"),
            "title": issue.get("title"),
            "stage": stage,
            "reason": state.get("reason"),
            "calls": state.get("calls"),
            "plan_revisions": state.get("plan_revisions"),
            "design_reopens": state.get("design_reopens"),
            "observation_cutoff": {"collected_at": meta["collected_at"], "stage": stage},
            "all_approved_at": approved,
        },
        "segments": task_segments,
        "configuration_history": {"complete": complete, "versions": len(states)},
        "observations": {
            "reports": task_reports,
            "findings": task_findings,
            "unreadable_review_reports": unreadable,
            "turns": task_turns,
            "evaluation_turns_excluded": private["evaluation_turns"],
            "inference": None
            if private["inference"] is None
            else {k: private["inference"][k] for k in ("pending", "native_status")},
            "pull_request": pull,
            "writer_inputs": writer_inputs(comments, identities),
        },
        "candidates": candidates(task_findings, pull, approved),
    }
    return entry, dossier


def tally(counter, key):
    """Count a key, naming a missing one `unknown` so outputs keep string keys."""
    counter[key if isinstance(key, str) and key else UNKNOWN] += 1


def tallied(keys):
    counter = Counter()
    for key in keys:
        tally(counter, key)
    return counter


def known_sum(values):
    """A sum over known values, with how many it covers; None when none are known."""
    present = [v for v in values if v is not None]
    return {"sum": sum(present) if present else None, "known": len(present), "total": len(values)}


def metrics(dossiers, population):
    """Cross-task counts by profile, with the coverage each count rests on."""
    profiles = {}

    def profile(name):
        return profiles.setdefault(
            name or UNKNOWN,
            {
                "turns": 0,
                "statuses": Counter(),
                "outcomes": Counter(),
                "terminations": Counter(),
                "elapsed_seconds": [],
                "input_tokens": [],
                "output_tokens": [],
                "observed_input_tokens": [],
                "observed_output_tokens": [],
                "reports": {},
                "correction_codes": Counter(),
                "findings_raised": 0,
                "findings_by_status": Counter(),
                "findings_by_severity": Counter(),
            },
        )

    turn_total = turn_tasks = usage_complete = unattributed = 0
    for dossier in dossiers:
        observations = dossier["observations"]
        if observations["turns"] is not None:
            turn_tasks += 1
            for turn in observations["turns"]:
                turn_total += 1
                bucket = profile(turn["profile"])
                bucket["turns"] += 1
                tally(bucket["statuses"], turn["status"])
                tally(bucket["outcomes"], turn["outcome"])
                if turn["termination_reason"]:
                    tally(bucket["terminations"], turn["termination_reason"])
                bucket["elapsed_seconds"].append(turn["elapsed_seconds"])
                tokens, complete = turn["tokens"], turn["usage_complete"]
                usage_complete += bool(complete)
                bucket["input_tokens"].append(tokens["input_tokens"] if complete else None)
                bucket["output_tokens"].append(tokens["output_tokens"] if complete else None)
                bucket["observed_input_tokens"].append(tokens["input_tokens"] if tokens else None)
                bucket["observed_output_tokens"].append(tokens["output_tokens"] if tokens else None)
                unattributed += turn["attribution"]["segment"] is None
        for report in observations["reports"]:
            bucket = profile(report["profile"])
            tally(bucket["reports"].setdefault(report["step"] or UNKNOWN, Counter()), report["outcome"])
            if report["correction_code"]:
                tally(bucket["correction_codes"], report["correction_code"])
        for finding in observations["findings"]:
            bucket = profile(finding["provider"])
            bucket["findings_raised"] += 1
            tally(bucket["findings_by_status"], finding["status"])
            tally(bucket["findings_by_severity"], finding["severity"])
    for bucket in profiles.values():
        for field in (
            "elapsed_seconds",
            "input_tokens",
            "output_tokens",
            "observed_input_tokens",
            "observed_output_tokens",
        ):
            bucket[field] = known_sum(bucket[field])
    return {
        "population": {
            "included": sum(p["included"] for p in population),
            "by_stage": tallied(p.get("stage") for p in population if p["included"]),
            "excluded": tallied(p["reason"] for p in population if not p["included"]),
        },
        "tasks": {
            "calls": known_sum([d["task"]["calls"] for d in dossiers]),
            "plan_revisions": known_sum([d["task"]["plan_revisions"] for d in dossiers]),
            "design_reopens": known_sum([d["task"]["design_reopens"] for d in dossiers]),
        },
        "profiles": profiles,
        "candidates": tallied(c["kind"] for d in dossiers for c in d["candidates"]),
        "coverage": {
            "tasks_with_diagnostics": turn_tasks,
            "tasks_with_inference": sum(d["observations"]["inference"] is not None for d in dossiers),
            "tasks_with_unsettled_inference": sum(
                (d["observations"]["inference"] or {}).get("pending") is not False
                or (d["observations"]["inference"] or {}).get("native_status") == "unavailable"
                for d in dossiers
                if d["observations"]["inference"] is not None
            ),
            "turns": turn_total,
            "turns_with_complete_usage": usage_complete,
            "turns_unattributed": unattributed,
            "segments": sum(len(d["segments"]) for d in dossiers),
            "tasks_with_incomplete_configuration_history": sum(
                not d["configuration_history"]["complete"] for d in dossiers
            ),
            "segments_without_configuration": sum(s["configuration"] is None for d in dossiers for s in d["segments"]),
            "unreadable_review_reports": sum(d["observations"]["unreadable_review_reports"] for d in dossiers),
        },
    }


def collect(snapshot):
    """The run summary and every included task's dossier, from the snapshot's reads alone."""
    population, dossiers = [], []
    for repository in snapshot.meta["target"]["issue_repositories"]:
        for issue in sorted(enrolled_issues(snapshot, repository), key=lambda i: i["number"]):
            entry, dossier = task(snapshot, issue, repository)
            population.append(entry)
            if dossier is not None:
                dossiers.append(dossier)
    summary = metrics(dossiers, population)
    limitations = list(LIMITATIONS)
    coverage = summary["coverage"]
    if coverage["tasks_with_diagnostics"] < len(dossiers):
        limitations.append(
            f"{len(dossiers) - coverage['tasks_with_diagnostics']} included tasks have no run diagnostics."
        )
    if coverage["tasks_with_inference"] < len(dossiers):
        limitations.append(
            f"{len(dossiers) - coverage['tasks_with_inference']} included tasks have no exchange observations."
        )
    if coverage["tasks_with_unsettled_inference"]:
        limitations.append(
            f"{coverage['tasks_with_unsettled_inference']} included tasks had inference requests awaiting"
            " observations or unavailable native observations; their turns' usage is not complete."
        )
    if coverage["tasks_with_incomplete_configuration_history"]:
        limitations.append(
            f"{coverage['tasks_with_incomplete_configuration_history']} included tasks have an incomplete audit"
            " version history; none of their records are attributed to a configuration."
        )
    if coverage["segments_without_configuration"]:
        limitations.append(
            f"{coverage['segments_without_configuration']} configuration segments could not be read from the fork."
        )
    if coverage["unreadable_review_reports"]:
        limitations.append(
            f"{coverage['unreadable_review_reports']} code-review reports could not be verified for dispositions."
        )
    run = {
        "version": FORMAT,
        "source": snapshot.meta,
        "population": population,
        "metrics": summary,
        "limitations": limitations,
    }
    return run, dossiers


def write(output, run, dossiers):
    """Write `run.json` and one dossier per task; refuse to overwrite an earlier output."""
    output = Path(output)
    if (output / "run.json").exists():
        raise ValueError("output directory already holds a run")
    (output / "dossiers").mkdir(parents=True, exist_ok=True)
    for dossier in dossiers:
        name = f"{dossier['task']['repository'].replace('/', '-')}-{dossier['task']['issue']}.json"
        (output / "dossiers" / name).write_text(json.dumps(dossier, indent=2, sort_keys=True) + "\n")
    (output / "run.json").write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")


def window(since, until=None, clock=time.time):
    """ISO bounds for the window; `until` defaults to now."""
    if epoch(since) is None or (until is not None and epoch(until) is None):
        raise ValueError("window bounds must be ISO dates or times")
    end = until or datetime.fromtimestamp(clock(), timezone.utc).isoformat()
    if epoch(since) >= epoch(end):
        raise ValueError("window must end after it starts")
    return since, end


def capture(settings, target_name, since, until, snapshot_dir, output, live_factory):
    """Read every source once into a new snapshot, then build the run from it."""
    target = next((t for t in settings.targets if target_name in (t.fork, t.upstream)), None)
    if target is None:
        raise ValueError("no configured target has that upstream or fork")
    since, until = window(since, until)
    live, bot_login = live_factory(target)
    meta = {
        "target": {
            "upstream": target.upstream,
            "fork": target.fork,
            "issue_repositories": list(target.issue_repositories),
        },
        "author": bot_login,
        "identities": {
            "logins": [bot_login],
            "user_ids": [settings.reviewer.user_id] if settings.reviewer else [],
        },
        "since": since,
        "until": until,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "engine": settings.engine,
    }
    try:
        run, dossiers = collect(Snapshot(snapshot_dir, meta, live))
    finally:
        live.close()
    write(output, run, dossiers)
    return run


def describe(run):
    """A one-line summary for the operator."""
    summary = run["metrics"]
    return (
        f"Included {summary['population']['included']} of {len(run['population'])} enrolled issues;"
        f" {summary['coverage']['turns']} turns, {sum(summary['candidates'].values())} candidates,"
        f" {len(run['limitations'])} limitations."
    )


def main(argv=None):
    """Rebuild a run offline from an existing snapshot."""
    parser = argparse.ArgumentParser(prog="python3 -m service.retro", description=__doc__.split("\n", 1)[0])
    parser.add_argument("--snapshot", required=True, help="directory written by `retro-collect`")
    parser.add_argument("--output", required=True, help="new directory for run.json and dossiers")
    arguments = parser.parse_args(argv)
    try:
        run, dossiers = collect(Snapshot(arguments.snapshot))
        write(arguments.output, run, dossiers)
    except (ValueError, OSError) as error:
        print(f"retro: {error}", file=sys.stderr)
        return 1
    print(describe(run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
