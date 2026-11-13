"""App-authored issue artifacts with legacy checkpoint migration and restart recovery."""

import json
import re
from copy import deepcopy

import bundle_resume
import file_inventory
import markup
import protocol
import report_store
from github import Transient
from identities import Identities
from policy import Blocked, digest
from protocol import AUDIT_TARGET_BYTES as LIMIT
from repository_reader import RepositoryReader
from storage_helpers import owned

MARKER = "<!-- concurro:v1 -->"
STATE_PREFIX = "\n```json\n"
AUDIT_MARKER = "<!-- concurro:audit:v1 -->"
AUDIT_DATA = "\n<!-- concurro:audit-data:v1\n"
PLAN_MARKER = "<!-- concurro:plan:v1 -->"
REF_MARKER = "<!-- concurro:checkpoint:v1 "
AUDIT_NAME = "Concurro audit state"
PLAN_NAME = "Concurro implementation plan"

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
    """Decode comment-encoded audits or the original fenced audit/comment payload."""
    if body.startswith(AUDIT_MARKER + AUDIT_DATA):
        value, closing, _ = body[len(AUDIT_MARKER + AUDIT_DATA) :].partition("\n-->\n")
        if not closing or ">" in value:
            raise ValueError("Invalid audit data comment")
        payload = json.loads(value)
        if payload["state"]["version"] not in report_store.READ_FORMATS:
            raise ValueError("Invalid audit encoding version")
        return payload
    return json.loads(body.rsplit(STATE_PREFIX, 1)[1].rsplit("\n```", 1)[0])


def artifact_metadata(kind):
    """Select audit metadata for generic artifacts, retaining the plan fallback for every other kind."""
    return (AUDIT_NAME, AUDIT_MARKER) if kind == "generic" else (PLAN_NAME, PLAN_MARKER)


class Checkpoints:
    """Persist one authoritative audit artifact and a derived human-readable plan."""

    def __init__(self, github, config, issue, report_cache=None, reader=None):
        """Bind storage to one repository, issue and configured App identity."""
        self.gh, self.config, self.issue = github, config, issue
        self.reader = reader or RepositoryReader(github, github)
        self.author = Identities(config).author("state")
        self.audit = self.plan = self.comment = self.legacy = None
        self.plan_binding = None
        self.dashboard = {}
        self.plan_replacement = None
        self.preserved_plans = []
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
            response = self.request(f"issues/{self.issue}/artifacts/{number}?per_page=1&page=1")
            artifact, versions = response["artifact"], response["versions"]
            name, marker = artifact_metadata(kind)
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
            artifact = {**artifact, "versions": versions, "revision": latest["version"]}
            origin = None
            if kind == "plan" and not bound_plan:
                origin = next((v for v in self.history(artifact) if v.get("version") == 1), None)
            artifact.update(
                managed_plan=bool(
                    bound_plan
                    or origin
                    and origin.get("name") == PLAN_NAME
                    and origin.get("actor", {}).get("login") == bot
                    and origin.get("body", "").startswith(PLAN_MARKER)
                ),
                origin_missing=kind == "plan" and not bound_plan and origin is None,
                origin_digest=digest(origin["body"]) if origin else None,
            )
            return artifact
        except CheckpointError:
            raise
        except (KeyError, IndexError, TypeError, ValueError):
            raise CheckpointError("artifact_identity") from None

    def history(self, artifact):
        """Read older versions lazily, refusing pages from a changed current artifact."""
        yield artifact["versions"][0]
        if artifact["revision"] == 1:
            return
        previous = artifact["revision"]
        page = 1
        while True:
            response = self.request(f"issues/{self.issue}/artifacts/{artifact['number']}?per_page=30&page={page}")
            try:
                current, versions = response["artifact"], response["versions"]
                if any(
                    current[k] != artifact[k]
                    for k in ("id", "number", "type", "name", "body", "creator", "updated_by_actor")
                ):
                    raise CheckpointError("concurrent_change")
                if not isinstance(versions, list) or type(response["has_next_page"]) is not bool:
                    raise ValueError()
                if page == 1:
                    if not versions or versions[0] != artifact["versions"][0]:
                        raise CheckpointError("concurrent_change")
                    versions = versions[1:]
                for version in versions:
                    if type(version["version"]) is not int or not 0 < version["version"] < previous:
                        raise CheckpointError("concurrent_change")
                    previous = version["version"]
                    yield version
                if not response["has_next_page"]:
                    return
                if not versions:
                    raise ValueError()
                page += 1
            except (KeyError, IndexError, TypeError, ValueError):
                raise CheckpointError("artifact_identity") from None

    def discover(self, items, kind):
        """Find a unique App-owned artifact, ignoring unrelated human artifacts."""
        name, marker = artifact_metadata(kind)
        matches = []
        for item in items:
            named = item.get("name") == name or item.get("body", "").startswith(marker)
            if item.get("creator", {}).get("login") != self.author:
                continue
            if not named and not (kind == "plan" and item.get("type") == "plan"):
                continue
            if kind == "plan" and self.plan_identity(item) in self.preserved_plans:
                continue
            artifact = self.read(item["number"], kind)
            if kind == "plan" and artifact["origin_missing"]:
                self.preserved_plans.append(self.plan_identity(artifact))
                continue
            if kind == "plan" and not artifact["managed_plan"]:
                if named:
                    raise CheckpointError("plan_origin")
                continue
            if (
                kind == "plan"
                and self.plan_replacement
                and not self.plan_binding
                and artifact["origin_digest"] != self.plan_replacement["digest"]
            ):
                raise CheckpointError("duplicate_artifacts")
            matches.append(artifact)
        if len(matches) > 1:
            raise CheckpointError("duplicate_artifacts")
        return matches[0] if matches else None

    @staticmethod
    def plan_identity(artifact):
        """Select the immutable identity of a plan already verified as App-owned."""
        return {key: artifact[key] for key in ("id", "number")}

    def audit_payload(self, state, bind_plan=True, external=True):
        """Build the repository-bound envelope with commitments to detached evidence."""
        compact, inventories = file_inventory.pack(state) if external else (state, [])
        compact, detached = report_store.packed(compact) if external else (compact, [])
        # Frozen admission identities need the v9 reader; older records retain their wire format.
        wire = (
            report_store.SPONSORSHIP_FORMAT
            if any(
                k.startswith(("sponsorship", "funding_initialization")) or k == "pending_sponsorships" for k in state
            )
            else report_store.FORMAT
        )
        if state.get("admission_version"):
            wire = report_store.ADMISSION_FORMAT
        payload = {
            "repository": self.config["repository"],
            "state": {**compact, "version": wire} if external else compact,
        }
        if inventories:
            payload["file_inventories"] = inventories
        if detached:
            payload["report_data"] = [self.reports.checkpoint_reference(item) for item in detached]
        if bind_plan and self.plan_binding:
            payload["plan_artifact"] = self.plan_binding
        if external and self.dashboard:
            payload["dashboard"] = deepcopy(self.dashboard)
        if external and self.plan_replacement:
            payload["plan_replacement"] = deepcopy(self.plan_replacement)
        return payload

    def render_audit(self, payload, *, legacy=False):
        """Render a compact mutable audit and links to its immutable evidence."""
        refs = payload.get("report_data", []) + ([payload["audit_data"]] if "audit_data" in payload else [])
        links = set()
        for ref in refs:
            links.update(ref.get("artifacts", []))
            history = ref.get("history", {})
            for child in history.get("blocks", []) + history.get("tail", {}).get("references", []):
                links.update(child["artifacts"])
        links = sorted(links)
        footer = "".join(
            f"\n[Report data {n}]({protocol.artifact_url(self.config['repository'], self.issue, n)})" for n in links
        )
        data = json.dumps(payload, sort_keys=True)
        if legacy:
            return AUDIT_MARKER + STATE_PREFIX + data + "\n```\n" + footer
        return AUDIT_MARKER + AUDIT_DATA + data.replace(">", "\\u003e") + "\n-->\n" + footer

    def audit_body(self, state, bind_plan=True, external=True, *, prepare=False, target=LIMIT):
        """Keep mutable versions compact; optionally persist overflow metadata before writing."""
        payload = self.audit_payload(state, bind_plan, external)
        body = self.render_audit(payload, legacy=not external)
        if external and len(body.encode()) > target:
            wire = payload["state"]["version"]
            key = digest(payload)
            if prepare:
                self.reports.put(payload)
            body = self.render_audit(
                {
                    "repository": self.config["repository"],
                    "state": {"version": wire, "issue": self.issue},
                    "audit_data": {"digest": key, "artifacts": self.reports.numbers.get(key, [])},
                }
            )
        return body

    def decode(self, artifact):
        """Validate the checkpoint's repository, issue and schema identities."""
        try:
            payload = report_store.audit_envelope(
                legacy_state(artifact["body"]), self.reports, self.config["repository"], self.issue
            )
            state = payload["state"]
            if payload["repository"] != self.config["repository"]:
                raise ValueError()
            self.validate_state(state)
            self.hydrate(state, payload)
            binding = payload.get("plan_artifact")
            if binding is not None and (
                not isinstance(binding, dict)
                or set(binding) != {"id", "number"}
                or any(type(value) is not int or value < 1 for value in binding.values())
            ):
                raise ValueError()
            if ("dashboard" in payload or "plan_replacement" in payload) and state[
                "version"
            ] not in report_store.READ_FORMATS:
                raise ValueError()
            self.plan_binding = binding
            self.dashboard = deepcopy(payload.get("dashboard", {}))
            self.validate_dashboard()
            self.plan_replacement = deepcopy(payload.get("plan_replacement"))
            if self.plan_replacement is not None:
                intent = self.plan_replacement
                if (
                    not isinstance(intent, dict)
                    or set(intent) != {"preserved", "digest"}
                    or not isinstance(intent["preserved"], list)
                    or not re.fullmatch(protocol.DIGEST_PATTERN, intent["digest"])
                ):
                    raise ValueError()
                for item in intent["preserved"]:
                    if (
                        not isinstance(item, dict)
                        or set(item) != {"id", "number"}
                        or any(type(v) is not int or v < 1 for v in item.values())
                    ):
                        raise ValueError()
                self.preserved_plans = deepcopy(intent["preserved"])
            state["version"] = 2
            return state
        except report_store.ReportDataError:
            raise CheckpointError("report_data") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise CheckpointError("checkpoint_format") from None

    def restore_reports(self, state, payload, *, fields=None):
        """Validate the checkpoint format and restore selected or complete report evidence."""
        refs = payload.get("report_data", [])
        if refs and state["version"] not in (
            FORMAT,
            BATCH_FORMAT,
            file_inventory.FORMAT,
            bundle_resume.FORMAT,
            report_store.SPONSORSHIP_FORMAT,
            report_store.ADMISSION_FORMAT,
            report_store.FORMAT,
        ):
            raise ValueError()
        report_store.restore(state, refs, self.reports, fields=fields)

    def hydrate(self, state, payload):
        """Restore the full checkpoint evidence needed by the controller."""
        self.restore_reports(state, payload)
        inventories = payload.get("file_inventories", [])
        if (inventories or state.get("candidate", {}).get("file_inventory")) and state["version"] not in (
            file_inventory.FORMAT,
            bundle_resume.FORMAT,
            report_store.SPONSORSHIP_FORMAT,
            report_store.ADMISSION_FORMAT,
            report_store.FORMAT,
        ):
            raise ValueError()
        file_inventory.restore(state, inventories, self.reader)
        self.reports.bind_receipts(state)

    def validate_state(self, state):
        """Reject checkpoints belonging to another issue or schema."""
        if (
            state["version"]
            not in (
                2,
                FORMAT,
                BATCH_FORMAT,
                file_inventory.FORMAT,
                bundle_resume.FORMAT,
                report_store.SPONSORSHIP_FORMAT,
                report_store.ADMISSION_FORMAT,
                report_store.FORMAT,
            )
            or state.get("bundle_resume")
            and state["version"]
            not in (
                bundle_resume.FORMAT,
                report_store.SPONSORSHIP_FORMAT,
                report_store.ADMISSION_FORMAT,
                report_store.FORMAT,
            )
            or state["issue"] != self.issue
            or (state.get("review_batch_version") or state.get("review_batch"))
            and state["version"]
            not in (
                BATCH_FORMAT,
                file_inventory.FORMAT,
                bundle_resume.FORMAT,
                report_store.SPONSORSHIP_FORMAT,
                report_store.ADMISSION_FORMAT,
                report_store.FORMAT,
            )
            or state.get("review_batch_version", 1) != 1
        ):
            raise CheckpointError("state_identity")

    def reference(self):
        """Pin the comment to an artifact identity and a known durable version."""
        return {key: self.audit[key] for key in ("id", "number", "revision")} | {"digest": digest(self.audit["body"])}

    @staticmethod
    def pointer_valid(pointer):
        """Recognize the exact recovery-reference schema without coercing identities."""
        return (
            isinstance(pointer, dict)
            and set(pointer) == {"id", "number", "revision", "digest"}
            and all(type(pointer[k]) is int and pointer[k] > 0 for k in ("id", "number", "revision"))
            and isinstance(pointer["digest"], str)
            and re.fullmatch(protocol.DIGEST_PATTERN, pointer["digest"]) is not None
        )

    @classmethod
    def comment_pointer(cls, body):
        """Parse the single recovery reference carried by a dashboard comment."""
        matches = re.findall(re.escape(REF_MARKER) + r"(\{[^\n]*\}) -->", body)
        if len(matches) != 1:
            raise ValueError("Invalid dashboard reference")
        pointer = json.loads(matches[0])
        if not cls.pointer_valid(pointer):
            raise ValueError("Invalid dashboard reference")
        return pointer

    @classmethod
    def display_body(cls, body):
        """Exclude only a valid recovery marker when comparing rendered dashboard content."""
        cls.comment_pointer(body)
        return re.sub(re.escape(REF_MARKER) + r"\{[^\n]*\} -->", "", body, count=1)

    def validate_dashboard(self):
        """Validate bounded publication receipts before they can witness a historical pointer."""
        if not isinstance(self.dashboard, dict) or not set(self.dashboard) <= {"confirmed", "pending"}:
            raise ValueError()
        for kind, record in self.dashboard.items():
            if (
                not isinstance(record, dict)
                or set(record) != {"pointer", "display"}
                or not self.pointer_valid(record["pointer"])
                or not (kind == "pending" and record["display"] is None)
                and (
                    not isinstance(record["display"], str)
                    or not re.fullmatch(protocol.DIGEST_PATTERN, record["display"])
                )
            ):
                raise ValueError()
            if self.audit and (
                any(record["pointer"][k] != self.audit[k] for k in ("id", "number"))
                or record["pointer"]["revision"] > self.audit["revision"]
            ):
                raise ValueError()

    def verify_pointer(self, pointer):
        """Require a verified historical version or a carried-forward publication receipt."""
        if not self.audit or any(pointer[k] != self.audit[k] for k in ("id", "number")):
            raise CheckpointError("audit_missing")
        if any(record["pointer"] == pointer for record in self.dashboard.values()):
            return
        known = next((v for v in self.history(self.audit) if v.get("version") == pointer["revision"]), None)
        if (
            known is None
            or known.get("actor", {}).get("login") != self.author
            or digest(known.get("body")) != pointer["digest"]
        ):
            raise CheckpointError("audit_history")

    def observe_dashboard(self, comment):
        """Confirm an observed pointer without retiring a different uncertain publication."""
        if comment.get("user", {}).get("login") != self.author:
            raise CheckpointError("comment_format")
        try:
            pointer = self.comment_pointer(comment["body"])
            display = digest(self.display_body(comment["body"]))
        except (KeyError, TypeError, ValueError):
            raise CheckpointError("comment_format") from None
        self.verify_pointer(pointer)
        pending = self.dashboard.get("pending")
        if pending and pending["pointer"] == pointer:
            self.dashboard.pop("pending")
        self.dashboard["confirmed"] = {"pointer": pointer, "display": display}
        self.comment = deepcopy(comment)

    def dashboard_dirty(self, body):
        """Refresh changed presentation, not audit revisions containing only receipts."""
        if not self.comment or REF_MARKER not in self.comment["body"]:
            return True
        return self.display_body(self.comment["body"]) != self.display_body(body)

    def publish_dashboard(self, body):
        """Publish one pointer at a time; persist its outcome in the next ordinary checkpoint."""
        if not self.dashboard_dirty(body):
            return False
        pending = self.dashboard.get("pending")
        pointer = pending["pointer"] if pending else self.reference()
        # A restarted task may have newer display content, but cannot retire an uncertain
        # pointer by publishing a third one. Reuse the pending pointer until observed.
        body = re.sub(
            re.escape(REF_MARKER) + r"\{[^\n]*\} -->",
            lambda _: REF_MARKER + json.dumps(pointer, sort_keys=True, separators=(",", ":")) + " -->",
            body,
            count=1,
        )
        self.dashboard["pending"] = {"pointer": deepcopy(pointer), "display": digest(self.display_body(body))}
        if self.comment:
            number = self.comment["id"]
            self.request(f"issues/comments/{number}", "PATCH", {"body": body})
        else:
            result = self.request(f"issues/{self.issue}/comments", "POST", {"body": body})
            number = result["id"]
        observed = self.request(f"issues/comments/{number}")
        if observed.get("body") != body:
            raise CheckpointError("write_unconfirmed")
        self.observe_dashboard(observed)
        return True

    def load(self, comments):
        """Prefer verified artifact state; use a legacy comment only before migration."""
        matches = owned(comments, self.author, MARKER, "user")
        if len(matches) > 1:
            raise CheckpointError("duplicate_comments")
        self.comment = matches[0] if matches else None
        pointer = None
        if self.comment:
            body = self.comment["body"]
            if len(body.encode()) > protocol.PUBLICATION_BODY_BYTES:
                raise CheckpointError("comment_size")
            try:
                if REF_MARKER in body:
                    pointer = self.comment_pointer(body)
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
        if self.audit:
            state = self.decode(self.audit)
            if pointer:
                self.verify_pointer(pointer)
                had_pending = "pending" in self.dashboard
                self.observe_dashboard(self.comment)
                # The process may have died after sending the current revision's dashboard,
                # before any receipt save. Preserve that possible write alongside the observed one.
                if not had_pending and pointer != self.reference():
                    self.dashboard["pending"] = {"pointer": self.reference(), "display": None}
            if self.legacy is not None and not any(
                v.get("body") == self.audit_body(self.legacy, bind_plan=False, external=False)
                and v.get("actor", {}).get("login") == self.author
                for v in self.history(self.audit)
            ):
                raise CheckpointError("legacy_history")
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
                not pointer
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
        if self.plan or self.preserved_plans:
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
            fence = min((markup.fence(draft["output"], char) for char in ("`", "~")), key=len)
            lines += [
                "## Rejected draft — not approved",
                "",
                markup.block(draft["reason"]),
                "",
                "The planner must correct this output before implementation.",
                "",
                markup.details("Rejected output", fence + "json\n" + draft["output"] + "\n" + fence),
                "",
            ]
            if draft.get("truncated"):
                lines += ["Output excerpted to fit the checkpoint; the rejection reason is complete.", ""]
        elif state.get("plan"):
            plan = state["plan"]
            lines += [markup.block(plan["summary"]), ""]
            for key in ("questions", "files", "steps", "tests"):
                if plan[key]:
                    entries = [
                        (f"{i}. " if key == "steps" else "- ") + markup.text(item)
                        for i, item in enumerate(plan[key], 1)
                    ]
                    lines += ["## " + key.capitalize(), "", *entries, ""]
            if plan.get("responses"):
                lines += (
                    ["## Review responses", ""]
                    + [
                        f"- **{markup.text(item['id'])}**: {markup.text(item['response'])}"
                        for item in plan["responses"]
                    ]
                    + [""]
                )
            diagnostics = "Plan: " + markup.code(state["plan_id"]) + "\n\nBase: " + markup.code(state["base"])
            if plan.get("validation"):
                diagnostics += (
                    "\n\nExecutable validation\n\n```json\n"
                    + json.dumps(plan["validation"], indent=2, sort_keys=True)
                    + "\n```"
                )
            lines += [markup.details("Diagnostics", diagnostics), ""]
        else:
            lines += ["The previous plan is superseded. A new plan is required before implementation.", ""]
        body = "\n".join(lines)
        if len(body.encode()) <= protocol.ARTIFACT_VIEW_BYTES:
            return body
        draft = state.get("plan_draft")
        key = digest(report_store.projection(draft or state["plan"])[0])
        numbers = self.reports.numbers.get(key, [])
        if not numbers:
            raise CheckpointError("artifact_size")
        root = protocol.artifact_url(self.config["repository"], self.issue)
        links = "\n".join(f"[Complete plan data, part {i}]({root}{n})" for i, n in enumerate(numbers, 1))
        prefix = PLAN_MARKER + "\n# Concurro implementation plan\n\n"
        note = "Complete plan retained in the verified data artifacts:\n\n" + links
        footer = "\n\n" + note
        if draft:
            prefix += "## Rejected draft — not approved\n\n"
            prefix += (
                markup.excerpt(
                    draft["reason"],
                    protocol.ARTIFACT_VIEW_BYTES // 4,
                    "Full rejection reason in the data artifacts.",
                )
                + "\n\n"
            )

            def render_output(value):
                """Rebuild the disclosure and JSON fence around each raw output excerpt."""
                fence = min((markup.fence(value, char) for char in ("`", "~")), key=len)
                return markup.details("Rejected output", fence + "json\n" + value + "\n" + fence)

            excerpt = markup.excerpt(
                draft["output"],
                protocol.ARTIFACT_VIEW_BYTES - len((prefix + footer).encode()),
                "Full rejected output in the data artifacts.",
                render=render_output,
            )
        else:
            footer += "\n\n" + markup.details(
                "Diagnostics", "Plan: " + markup.code(state["plan_id"]) + "\n\nBase: " + markup.code(state["base"])
            )
            excerpt = markup.excerpt(
                state["plan"]["summary"],
                protocol.ARTIFACT_VIEW_BYTES - len((prefix + footer).encode()),
                "Full plan in the data artifacts.",
            )
        return prefix + excerpt + footer

    def write(self, existing, kind, body):
        """Write and read back one version; reject changed snapshots and ambiguous responses."""
        if len(body.encode()) > (LIMIT if kind == "generic" else protocol.ARTIFACT_BODY_BYTES):
            raise CheckpointError("artifact_size")
        name, _ = artifact_metadata(kind)
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

    def preflight(self, states, reserve=4096):
        """Prove publication states fit, reserving space for bounded receipts and failure diagnostics."""
        for state in states:
            compact, refs = file_inventory.pack(state)
            file_inventory.restore(deepcopy(compact), refs, self.reader)
            _, detached = report_store.packed(compact)
            for item in detached:
                self.reports.checkpoint_reference(item, prepare=True)
            if len(self.audit_body(state, prepare=True, target=LIMIT - reserve).encode()) > LIMIT - reserve:
                raise Blocked("publication checkpoint exceeds available capacity; split the change before publishing")

    def save(self, state):
        """Preserve a legacy snapshot first, then persist audit state before updating its views."""
        if self.uncertain:
            raise CheckpointError("write_uncertain")
        compact, refs = file_inventory.pack(state)
        # This proof only reads Git; failure cannot make a checkpoint write uncertain.
        file_inventory.restore(deepcopy(compact), refs, self.reader)
        _, detached = report_store.packed(compact)
        self.reports.bind_receipts(state)
        if not self.plan and self.preserved_plans and (state.get("plan") or state.get("plan_draft")):
            self.plan_replacement = {
                "preserved": deepcopy(self.preserved_plans),
                "digest": digest(self.plan_body(state)),
            }
        try:
            for item in detached:
                self.reports.checkpoint_reference(item, prepare=True)
            if self.audit is None and self.legacy is not None:
                self.audit = self.write(None, "generic", self.audit_body(self.legacy, bind_plan=False, external=False))
            self.audit = self.write(self.audit, "generic", self.audit_body(state, prepare=True))
            if self.plan or state.get("plan") or state.get("plan_draft"):
                body = self.plan_body(state)
                # A derived view is verified on load; unchanged views need no further reads this invocation.
                if not self.plan or self.plan["body"] != body or self.plan["name"] != PLAN_NAME:
                    self.plan = self.write(self.plan, "plan", body)
                binding = self.plan_identity(self.plan)
                if self.plan_binding != binding:
                    self.plan_binding = binding
                    self.audit = self.write(self.audit, "generic", self.audit_body(state, prepare=True))
        except Exception:
            # Any failure here may follow a successful remote write; retain uncertainty for recovery.
            self.uncertain = True
            raise

    def links(self):
        """Return the public audit link and hidden recovery reference, using issue-local URLs."""
        root = protocol.artifact_url(self.config["repository"], self.issue)
        return [
            f"[Automation audit state]({root}{self.audit['number']}) (public).",
            REF_MARKER + json.dumps(self.reference(), sort_keys=True, separators=(",", ":")) + " -->",
        ]
