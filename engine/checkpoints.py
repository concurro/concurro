"""App-authored issue artifacts with legacy checkpoint migration and restart recovery."""

from copy import deepcopy
import json
import re

import protocol
import report_store

from github import Transient
from identities import Identities
from policy import Blocked, digest

MARKER = "<!-- concurro:v1 -->"
STATE_PREFIX = "\n```json\n"
AUDIT_MARKER = "<!-- concurro:audit:v1 -->"
PLAN_MARKER = "<!-- concurro:plan:v1 -->"
REF_MARKER = "<!-- concurro:checkpoint:v1 "
AUDIT_NAME = "Concurro audit state"
PLAN_NAME = "Concurro implementation plan"
LIMIT = 60000
FORMAT = 3
BATCH_FORMAT = 4


DIAGNOSTICS = {
    "conversation_publish": "Conversation publication failed; pending events are preserved for retry.",
    "conversation_conflict": "A pending conversation event was edited or duplicated; restore it before retrying.",
    "failure_display": "The controller stopped, but could not fully publish its failure status.",
    "artifact_request": "Issue artifact request failed; retry without resetting state.",
    "artifact_collection": "Cannot establish issue artifacts; retry without resetting state.",
    "artifact_identity": "Artifact identity, authorship or version is invalid; reconcile manually.",
    "plan_history": "Plan origin is outside the returned history and has no audit binding; reconcile manually.",
    "plan_origin": "Plan artifact origin is invalid; reconcile manually.",
    "duplicate_artifacts": "Multiple Concurro artifacts; reconcile manually.",
    "checkpoint_format": "Artifact checkpoint identity or format is invalid.",
    "state_identity": "state identity mismatch",
    "duplicate_comments": "multiple state comments; manual reconciliation required",
    "comment_size": "Checkpoint comment exceeds size limit.",
    "comment_format": "Malformed checkpoint comment; reconcile manually.",
    "audit_missing": "Referenced audit artifact is missing or replaced; do not reset state.",
    "audit_history": "Audit history conflicts with the saved reference.",
    "legacy_history": "Legacy checkpoint is not preserved in artifact history.",
    "audit_absent": "Plan artifact exists without audit state; reconcile manually.",
    "report_data": "Referenced report data is missing, changed or invalid; restore it before continuing.",
    "artifact_size": "Artifact checkpoint exceeds size limit.",
    "concurrent_change": "Artifact changed during this invocation; reconcile before retry.",
    "concurrent_create": "Artifact appeared during this invocation; reload before retry.",
    "write_unconfirmed": "Artifact write could not be confirmed; reload before retry.",
    "write_uncertain": "Checkpoint write was uncertain; reload before retry.",
}


class CheckpointError(Blocked):
    """Stop without replacing state; expose only a fixed diagnostic from the catalog."""

    def __init__(self, code):
        """Select a safe diagnostic; unknown inputs never become public error text."""
        self.code = code if isinstance(code, str) and code in DIAGNOSTICS else "unknown"
        super().__init__(DIAGNOSTICS.get(self.code, "Checkpoint verification failed; inspect runner health."))

    def public_detail(self):
        """Render a stable code and fixed text without exception arguments or causes."""
        return f"`{self.code}`: " + DIAGNOSTICS.get(self.code, "Checkpoint verification failed; inspect runner health.")


def legacy_state(body):
    """Read either expanded or collapsed v2 JSON from a historical bot comment."""
    return json.loads(body.rsplit(STATE_PREFIX, 1)[1].rsplit("\n```", 1)[0])


class Checkpoints:
    """Persist one authoritative audit artifact and a derived human-readable plan."""

    def __init__(self, github, config, issue, report_cache=None):
        """Bind storage to one repository, issue and configured App identity."""
        self.gh, self.config, self.issue = github, config, issue
        self.author = Identities(config).author("state")
        self.audit = self.plan = self.comment = self.legacy = None
        self.plan_binding = None
        self.dirty = False
        self.uncertain = False
        self.reports = report_store.Store(github, issue, self.author, config["repository"], cache=report_cache)

    def request(self, path, method="GET", data=None):
        """Sanitize storage errors; never infer absence from an API failure."""
        try:
            return self.gh.repo(path, method, data)
        except Transient:
            raise
        except Exception:
            raise CheckpointError("artifact_request") from None

    def collection(self):
        """Return the bounded, issue-scoped artifact collection."""
        try:
            return self.gh.pages(f"issues/{self.issue}/artifacts")
        except Transient:
            raise
        except Exception:
            raise CheckpointError("artifact_collection") from None

    def read(self, number, kind):
        """Verify artifact identity and current version attribution before trusting its body."""
        try:
            response = self.request(f"issues/{self.issue}/artifacts/{number}")
            artifact, versions = response["artifact"], response["versions"]
            name, marker = (AUDIT_NAME, AUDIT_MARKER) if kind == "generic" else (PLAN_NAME, PLAN_MARKER)
            bot = self.author
            latest = versions[0]
            if (
                artifact["number"] != number
                or type(artifact["id"]) is not int
                or artifact["type"] != kind
                or artifact["creator"]["login"] != bot
                or latest["body"] != artifact["body"]
                or latest["name"] != artifact["name"]
                or latest["actor"]["login"] != artifact["updated_by_actor"]["login"]
                or type(latest["version"]) is not int
                or latest["version"] < 1
                or not isinstance(artifact["body"], str)
            ):
                raise ValueError()
            if kind == "generic" and (
                artifact["name"] != name
                or artifact["updated_by_actor"]["login"] != bot
                or not artifact["body"].startswith(marker)
                or len(artifact["body"].encode()) > LIMIT
            ):
                raise ValueError()
            if (
                kind == "plan"
                and self.plan_binding
                and self.plan_binding["number"] == number
                and self.plan_binding["id"] != artifact["id"]
            ):
                raise CheckpointError("artifact_identity")
            bound_plan = kind == "plan" and self.plan_binding == self.plan_identity(artifact)
            if kind == "plan" and not bound_plan and not any(v.get("version") == 1 for v in versions):
                raise CheckpointError("plan_history")
            managed_plan = (
                bound_plan
                or kind == "plan"
                and any(
                    v.get("version") == 1
                    and v.get("name") == PLAN_NAME
                    and v.get("actor", {}).get("login") == bot
                    and v.get("body", "").startswith(PLAN_MARKER)
                    for v in versions
                )
            )
            return {**artifact, "versions": versions, "revision": latest["version"], "managed_plan": managed_plan}
        except CheckpointError:
            raise
        except (KeyError, IndexError, TypeError, ValueError):
            raise CheckpointError("artifact_identity") from None

    def discover(self, items, kind):
        """Find a unique App-owned artifact, ignoring unrelated human artifacts."""
        name, marker = (AUDIT_NAME, AUDIT_MARKER) if kind == "generic" else (PLAN_NAME, PLAN_MARKER)
        matches = []
        for item in items:
            named = item.get("name") == name or item.get("body", "").startswith(marker)
            if item.get("creator", {}).get("login") != self.author:
                continue
            if not named and not (kind == "plan" and item.get("type") == "plan"):
                continue
            artifact = self.read(item["number"], kind)
            if kind == "plan" and not artifact["managed_plan"]:
                if named:
                    raise CheckpointError("plan_origin")
                continue
            matches.append(artifact)
        if len(matches) > 1:
            raise CheckpointError("duplicate_artifacts")
        return matches[0] if matches else None

    @staticmethod
    def plan_identity(artifact):
        """Select the immutable identity of a plan already verified as App-owned."""
        return {key: artifact[key] for key in ("id", "number")}

    def audit_body(self, state, bind_plan=True, external=True):
        """Encode repository-bound state in a public Markdown artifact."""
        compact, detached = report_store.packed(state) if external else (state, [])
        # The wire version changes; the engine's in-memory v2 state contract stays unchanged.
        payload = {
            "repository": self.config["repository"],
            "state": {**compact, "version": BATCH_FORMAT if state.get("review_batch_version") else FORMAT}
            if external
            else compact,
        }
        if detached:
            payload["report_data"] = [
                {
                    **{k: item[k] for k in ("path", "digest", "select", "annotations")},
                    "artifacts": self.reports.numbers.get(item["digest"], []),
                }
                for item in detached
            ]
        if bind_plan and self.plan_binding:
            payload["plan_artifact"] = self.plan_binding
        links = sorted({n for ref in payload.get("report_data", []) for n in ref["artifacts"]})
        footer = "".join(
            f"\n[Report data {n}]({protocol.artifact_url(self.config['repository'], self.issue, n)})" for n in links
        )
        return AUDIT_MARKER + STATE_PREFIX + json.dumps(payload, sort_keys=True) + "\n```\n" + footer

    def decode(self, artifact):
        """Validate the checkpoint's repository, issue and schema identities."""
        try:
            payload = legacy_state(artifact["body"])
            state = payload["state"]
            if payload["repository"] != self.config["repository"]:
                raise ValueError()
            self.validate_state(state)
            refs = payload.get("report_data", [])
            if refs and state["version"] not in (FORMAT, BATCH_FORMAT):
                raise ValueError()
            if not isinstance(refs, list):
                raise ValueError()
            seen = set()
            for ref in refs:
                if (
                    not isinstance(ref, dict)
                    or set(ref) != {"path", "digest", "artifacts", "select", "annotations"}
                    or not report_store.allowed_path(ref["path"])
                    or not isinstance(ref["select"], list)
                    or len(ref["select"]) > 32
                    or not isinstance(ref["annotations"], dict)
                    or not set(ref["annotations"]) <= report_store.ANNOTATIONS
                ):
                    raise ValueError()
                path = tuple(ref["path"])
                if any(path[: len(previous)] == previous or previous[: len(path)] == path for previous in seen):
                    raise ValueError()
                seen.add(path)
                parent = state
                for component in path[:-1]:
                    parent = parent[component]
                if parent[path[-1]] is not None:
                    raise ValueError()
                value = self.reports.get(ref["digest"], ref["artifacts"])
                for component in ref["select"]:
                    if not (isinstance(component, str) or type(component) is int and component >= 0):
                        raise ValueError()
                    value = value[component]
                if ref["annotations"]:
                    if not isinstance(value, dict) or set(value) & set(ref["annotations"]):
                        raise ValueError()
                    value = {**value, **ref["annotations"]}
                parent[path[-1]] = value
            binding = payload.get("plan_artifact")
            if binding is not None and (
                not isinstance(binding, dict)
                or set(binding) != {"id", "number"}
                or any(type(value) is not int or value < 1 for value in binding.values())
            ):
                raise ValueError()
            self.plan_binding = binding
            state["version"] = 2
            return state
        except report_store.ReportDataError:
            raise CheckpointError("report_data") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise CheckpointError("checkpoint_format") from None

    def validate_state(self, state):
        """Reject checkpoints belonging to another issue or schema."""
        if (
            state["version"] not in (2, FORMAT, BATCH_FORMAT)
            or state["issue"] != self.issue
            or (state.get("review_batch_version") or state.get("review_batch"))
            and state["version"] != BATCH_FORMAT
            or state.get("review_batch_version", 1) != 1
        ):
            raise CheckpointError("state_identity")

    def reference(self):
        """Pin the comment to an artifact identity and a known durable version."""
        return {key: self.audit[key] for key in ("id", "number", "revision")} | {"digest": digest(self.audit["body"])}

    def load(self, comments):
        """Prefer verified artifact state; use a legacy comment only before migration."""
        matches = [
            c
            for c in comments
            if c.get("user", {}).get("login") == self.author and c.get("body", "").startswith(MARKER)
        ]
        if len(matches) > 1:
            raise CheckpointError("duplicate_comments")
        self.comment = matches[0] if matches else None
        pointer = None
        if self.comment:
            body = self.comment["body"]
            if len(body.encode()) > LIMIT:
                raise CheckpointError("comment_size")
            try:
                if REF_MARKER in body:
                    pointers = re.findall(re.escape(REF_MARKER) + r"(\{[^\n]*\}) -->", body)
                    if len(pointers) != 1:
                        raise ValueError()
                    pointer = json.loads(pointers[0])
                    if (
                        set(pointer) != {"id", "number", "revision", "digest"}
                        or any(type(pointer[k]) is not int or pointer[k] < 1 for k in ("id", "number", "revision"))
                        or not re.fullmatch(protocol.DIGEST_PATTERN, pointer["digest"])
                    ):
                        raise ValueError()
                else:
                    self.legacy = legacy_state(body)
                    self.validate_state(self.legacy)
            except (ValueError, IndexError, KeyError, TypeError):
                raise CheckpointError("comment_format") from None
        items = None
        if pointer:
            self.audit = self.read(pointer["number"], "generic")
        else:
            items = self.collection()
            self.reports.items = items
            self.audit = self.discover(items, "generic")
        if pointer:
            if not self.audit or any(pointer[k] != self.audit[k] for k in ("id", "number")):
                raise CheckpointError("audit_missing")
            known = [v for v in self.audit["versions"] if v.get("version") == pointer["revision"]]
            if (
                len(known) != 1
                or known[0].get("actor", {}).get("login") != self.author
                or digest(known[0].get("body")) != pointer["digest"]
            ):
                raise CheckpointError("audit_history")
        if self.audit:
            if self.legacy is not None and not any(
                v.get("body") == self.audit_body(self.legacy, bind_plan=False, external=False)
                and v.get("actor", {}).get("login") == self.author
                for v in self.audit["versions"]
            ):
                raise CheckpointError("legacy_history")
            state = self.decode(self.audit)
            if self.plan_binding:
                try:
                    self.plan = self.read(self.plan_binding["number"], "plan")
                except CheckpointError as error:
                    if error.code != "artifact_request":
                        raise
                    # A deleted generated view is recoverable, but only a successful listing proves absence.
                    self.plan = self.discover(self.collection(), "plan")
            else:
                self.plan = self.discover(items if items is not None else self.collection(), "plan")
            self.dirty = (
                pointer != self.reference()
                or (self.plan or state.get("plan") or state.get("plan_draft"))
                and (
                    not self.plan
                    or self.plan["name"] != PLAN_NAME
                    or self.plan["body"] != self.plan_body(state)
                    or self.plan_binding != self.plan_identity(self.plan)
                )
            )
            return state
        self.plan = self.discover(items, "plan")
        if self.plan:
            raise CheckpointError("audit_absent")
        self.dirty = self.legacy is not None
        return deepcopy(self.legacy)

    def plan_body(self, state):
        """Render the current plan as a view of the authoritative checkpoint."""
        lines = [
            PLAN_MARKER,
            "# Concurro implementation plan",
            "",
            "Generated view; edits are overwritten. Request changes in issue comments.",
            "",
            "Approval is recorded in the audit state; editing this document does not approve work.",
            "",
        ]
        if state.get("plan_draft"):
            draft = state["plan_draft"]
            fence = min(
                (
                    char * max(3, 1 + max((len(v) for v in re.findall(char + r"+", draft["output"])), default=0))
                    for char in ("`", "~")
                ),
                key=len,
            )
            lines += [
                "## Rejected draft — not approved",
                "",
                draft["reason"],
                "",
                "The planner is asked to correct this output; it does not authorize implementation.",
                "",
                fence + "json",
                draft["output"],
                fence,
                "",
            ]
            if draft.get("truncated"):
                lines += ["Output excerpted to fit the checkpoint; the rejection reason is complete.", ""]
        elif state.get("plan"):
            lines += [
                "Plan: `" + state["plan_id"] + "`",
                "Base: `" + state["base"] + "`",
                "",
                state["plan"]["summary"],
                "",
            ]
            for key in ("questions", "files", "steps", "tests"):
                lines += ["## " + key.capitalize(), ""] + ["- " + item for item in state["plan"][key]] + [""]
            if state["plan"].get("responses"):
                lines += (
                    ["## Review responses", ""]
                    + [f"- **{item['id']}**: {item['response']}" for item in state["plan"]["responses"]]
                    + [""]
                )
            if state["plan"].get("validation"):
                lines += [
                    "## Executable validation",
                    "",
                    "```json",
                    json.dumps(state["plan"]["validation"], indent=2, sort_keys=True),
                    "```",
                    "",
                ]
        else:
            lines += ["The previous plan is superseded. A new plan is required before implementation.", ""]
        return "\n".join(lines)

    def write(self, existing, kind, body):
        """Write and read back one version; reject changed snapshots and ambiguous responses."""
        if len(body.encode()) > LIMIT:
            raise CheckpointError("artifact_size")
        name = AUDIT_NAME if kind == "generic" else PLAN_NAME
        path = f"issues/{self.issue}/artifacts"
        if existing:
            current = self.read(existing["number"], kind)
            if any(current[k] != existing[k] for k in ("id", "revision", "body")):
                raise CheckpointError("concurrent_change")
            if current["body"] == body and current["name"] == name:
                return current
            result = self.request(path + "/" + str(existing["number"]), "PATCH", {"name": name, "body": body})
            expected = existing["revision"] + 1
        else:
            if self.discover(self.collection(), kind):
                raise CheckpointError("concurrent_create")
            result = self.request(path, "POST", {"type": kind, "name": name, "body": body})
            expected = 1
        try:
            saved = self.read(result["number"], kind)
            if (
                saved["id"] != result["id"]
                or saved["body"] != body
                or saved["revision"] != expected
                or existing
                and any(saved[k] != existing[k] for k in ("id", "number"))
            ):
                raise ValueError()
            return saved
        except (KeyError, TypeError, ValueError):
            raise CheckpointError("write_unconfirmed") from None

    def save(self, state):
        """Preserve a legacy snapshot first, then persist audit state before updating its views."""
        if self.uncertain:
            raise CheckpointError("write_uncertain")
        try:
            _, detached = report_store.packed(state)
            for item in detached:
                self.reports.put(item["value"])
            if self.audit is None and self.legacy is not None:
                self.audit = self.write(None, "generic", self.audit_body(self.legacy, bind_plan=False, external=False))
            self.audit = self.write(self.audit, "generic", self.audit_body(state))
            if self.plan or state.get("plan") or state.get("plan_draft"):
                body = self.plan_body(state)
                # A derived view is verified on load; unchanged views need no further reads this invocation.
                if not self.plan or self.plan["body"] != body or self.plan["name"] != PLAN_NAME:
                    self.plan = self.write(self.plan, "plan", body)
                binding = self.plan_identity(self.plan)
                if self.plan_binding != binding:
                    self.plan_binding = binding
                    self.audit = self.write(self.audit, "generic", self.audit_body(state))
        except Exception:
            self.uncertain = True
            raise

    def links(self):
        """Return the public audit link and hidden recovery reference, using issue-local URLs."""
        root = protocol.artifact_url(self.config["repository"], self.issue)
        return [
            f"[Automation audit state]({root}{self.audit['number']}) (public).",
            REF_MARKER + json.dumps(self.reference(), sort_keys=True, separators=(",", ":")) + " -->",
        ]
