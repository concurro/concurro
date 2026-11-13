"""Canonical immutable report artifacts shared by the audit and rounds table."""

import json
import re
import threading
import uuid
from collections import OrderedDict
from copy import deepcopy

import artifact_capacity
import markup
import protocol
import report_artifact
from github import NotFound
from policy import Blocked, digest
from review_state import source_value
from storage_helpers import graft_slot, owned

MARKER = "<!-- concurro:report-data:v1:"
CHUNK = protocol.ARTIFACT_BODY_BYTES
LEGACY_CHUNK = 16000
INLINE = 12000
# Fixed entry positions keep later blocks reusable when older metadata grows.
BLOCK_ENTRIES = 8
FORMAT = 7
EXTENDED_FIELDS = {
    "plan",
    "plan_draft",
    "intake_draft",
    "charter",
    "pr_template",
    "implementation_feedback",
    "requirements",
    "conversation",
    "candidate",
    "outcomes",
    "prior_outcomes",
    "reports",
}
HISTORY_FIELDS = {"qc_history", "design_history", "review_history"}
FIELDS = (
    EXTENDED_FIELDS
    | HISTORY_FIELDS
    | {
        "qc",
        "design_votes",
        "design_feedback",
        "objections",
        "reviews",
        "review_event",
        "findings",
        "pr_outbox",
        "reason",
        "reopen_history",
        "resume_history",
        "invalidations",
        "requests",
        "review_draft",
        "review_batch",
    }
)


class ReportDataError(Blocked):
    """Missing or changed durable review data must never authorise new work."""


ANNOTATIONS = {
    "classification",
    "agent",
    "head",
    "model",
    "input_revision",
    "sequence",
    "finding_ids",
    "review_binding",
}

ARCHIVE_JSON = re.compile(r"\n(`{3,})json\n(.*)\n\1$", re.S)


def report_content(value):
    """Copy canonical report evidence without any storage annotations, preserving key order."""
    return {k: v for k, v in value.items() if k not in ANNOTATIONS}


def report_annotations(value):
    """Copy the storage annotations that accompany a canonical report reference."""
    return {k: v for k, v in value.items() if k in ANNOTATIONS}


def is_report(value):
    """Recognise the engine's complete assessment records."""
    return isinstance(value, dict) and (
        {"decision", "rationale"} <= value.keys() or {"verdict", "summary"} <= value.keys()
    )


def projection(value):
    """Separate reproducible report annotations and rejected-output formatting from canonical content."""
    draft = None
    if (
        isinstance(value, dict)
        and set(value) == {"output", "reason", "attempt", "truncated"}
        and isinstance(value["output"], str)
    ):
        try:
            decoded = json.loads(value["output"])
            if json.dumps(decoded, indent=2, ensure_ascii=True) == value["output"]:
                order = []

                def collect(node, path):
                    """Record only object key order needed to reproduce a rejected output string exactly."""
                    if isinstance(node, dict):
                        order.append([path, list(node)])
                        for k, child in node.items():
                            collect(child, path + [k])
                    elif isinstance(node, list):
                        for i, child in enumerate(node):
                            collect(child, path + [i])

                collect(decoded, [])
                draft = {"fields": {k: v for k, v in value.items() if k != "output"}, "order": order}
                value = decoded
        except (ValueError, TypeError):
            pass
    annotations = report_annotations(value) if is_report(value) else {}
    core = report_content(value) if is_report(value) else value
    return core, annotations, draft


def annotated(value, annotations, draft=None):
    """Reconstruct a full logical report or exact rejected-output wrapper from its receipt."""
    if not isinstance(annotations, dict) or not set(annotations) <= ANNOTATIONS:
        raise ValueError("Invalid report annotations")
    if annotations:
        if not isinstance(value, dict) or set(value) & set(annotations):
            raise ValueError("Report annotations overwrite evidence")
        value = {**value, **deepcopy(annotations)}
    if draft is not None:
        if set(draft) != {"fields", "order"} or set(draft["fields"]) != {"reason", "attempt", "truncated"}:
            raise ValueError("Invalid rejected-output receipt")
        holder = [deepcopy(value)]
        seen = set()
        for path, keys in draft["order"]:
            location = tuple(path)
            if location in seen or not isinstance(keys, list) or len(set(keys)) != len(keys):
                raise ValueError("Invalid rejected-output key order")
            seen.add(location)
            parent, slot = (holder, 0) if not path else (source_value(holder[0], path[:-1]), path[-1])
            node = parent[slot]
            if not isinstance(node, dict) or set(node) != set(keys):
                raise ValueError("Rejected-output keys changed")
            parent[slot] = {key: node[key] for key in keys}
        value = {**draft["fields"], "output": json.dumps(holder[0], indent=2, ensure_ascii=True)}
    return value


def packed(state):
    """Reference evidence and oversized collections, leaving small metadata inline."""
    if len(json.dumps(state).encode()) <= INLINE:
        return state, []
    sources, fragments = {}, {}

    def index(value, root, path):
        """Locate reproducible fragments inside a report instead of storing copies."""
        if isinstance(value, str) and len(value.encode()) >= 2000:
            fragments.setdefault(digest(value), (root, path))
        elif isinstance(value, dict):
            for key, child in value.items():
                index(child, root, path + [key])
        elif isinstance(value, list):
            for i, child in enumerate(value):
                index(child, root, path + [i])

    def collect(value):
        """Find complete reports before replacing any of their derived fields."""
        if is_report(value):
            core = report_content(value)
            if len(json.dumps(core).encode()) >= 2000:
                key = digest(core)
                sources[key] = core
                index(core, key, [])
        elif isinstance(value, dict):
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    roots = {key: state[key] for key in sorted(FIELDS) if key in state}
    if "response" in state.get("operation", {}):
        roots["operation"] = {"response": state["operation"]["response"]}
    collect(roots)
    detached = []

    def visit(value, path):
        """Detach canonical reports, large state roots and large leaves."""
        core, annotations, draft = projection(value)
        if (
            draft is not None
            or is_report(value)
            and (digest(core) in sources or len(json.dumps(core).encode()) > INLINE)
        ):
            ref = {"path": path, "digest": digest(core), "value": core, "select": [], "annotations": annotations}
            if draft is not None:
                ref["draft"] = draft
            detached.append(ref)
            return None
        if (
            path == ["review_draft"]
            or path[0] == "review_batch"
            and (path[-1] == "review_draft" or path[-2:] == ["operation", "response"] and not is_report(value))
        ):
            detached.append({"path": path, "digest": digest(value), "value": value, "select": [], "annotations": {}})
            return None
        if (path == ["operation", "response"] or len(path) == 1 and path[0] in EXTENDED_FIELDS) and len(
            json.dumps(value).encode()
        ) > INLINE:
            detached.append({"path": path, "digest": digest(value), "value": value, "select": [], "annotations": {}})
            return None
        if is_report(value):
            core = report_content(value)
            key = digest(core)
            if key in sources:
                detached.append(
                    {
                        "path": path,
                        "digest": key,
                        "value": core,
                        "select": [],
                        "annotations": report_annotations(value),
                    }
                )
                return None
        if isinstance(value, str) and len(value.encode()) >= 2000:
            key, select = fragments.get(digest(value), (digest(value), []))
            detached.append(
                {"path": path, "digest": key, "value": sources.get(key, value), "select": select, "annotations": {}}
            )
            return None
        if isinstance(value, dict):
            return {k: visit(v, path + [k]) for k, v in value.items()}
        if isinstance(value, list):
            start = len(detached)
            compact = [visit(v, path + [i]) for i, v in enumerate(value)]
            if len(path) == 1 and path[0] in HISTORY_FIELDS and len(json.dumps(compact).encode()) > INLINE:
                del detached[start:]
                detached.append(
                    {"path": path, "digest": digest(value), "value": value, "select": [], "annotations": {}}
                )
                return None
            return compact
        return value

    compact = deepcopy(state)
    for key, value in roots.items():
        if key == "operation":
            compact[key]["response"] = visit(value["response"], [key, "response"])
        else:
            compact[key] = visit(value, [key])
    return compact, detached


def allowed_path(path):
    """Only report subtrees may be restored; traversal never creates missing containers."""
    return (
        isinstance(path, list)
        and 0 < len(path) <= 32
        and all(isinstance(p, str) or type(p) is int and p >= 0 for p in path)
        and (
            path[0] in FIELDS - {"review_batch"}
            or path[:2] == ["operation", "response"]
            or path[:2] == ["review_batch", "members"]
            and len(path) >= 4
            and (path[3] == "review_draft" or path[3:5] == ["operation", "response"])
        )
    )


def inline_fields(state, names):
    """Locate original metadata fields retained in the audit or its reference annotations."""
    compact, refs = packed(state)
    # Published annotation snapshots are the archive now, including while the index is inline.
    published = list(state.get("reports", [])) + list(state.get("conversation", {}).get("receipts", {}).values())
    snapshots = {
        id(entry["data"])
        for entry in published
        if isinstance(entry.get("data"), dict) and "annotations" in entry["data"]
    }
    snapshots.update(id(entry) for entry in published if entry.get("event") and "data" in entry)
    result = set()

    def visit(original, view):
        """Pair the packed layout with original holders, skipping detached subtrees."""
        if id(original) in snapshots:
            return
        if isinstance(view, dict):
            for key, child in view.items():
                if key in names and child is not None:
                    result.add((id(original), key))
                visit(original[key], child)
        elif isinstance(view, list):
            for original_child, child in zip(original, view):
                visit(original_child, child)

    visit(state, compact)
    for ref in refs:
        if "draft" not in ref:
            visit(source_value(state, ref["path"]), ref["annotations"])
    return result


def record_json(value):
    """Serialize immutable record JSON identically for publication and encoding detection."""
    return json.dumps(value, sort_keys=True, indent=2)


def restore(state, refs, store, *, fields=None):
    """Validate reference slots and hydrate all evidence, or selected roots for read-only views."""
    if not isinstance(refs, list):
        raise ValueError()
    seen = set()
    for ref in refs:
        if (
            not isinstance(ref, dict)
            or set(ref)
            not in (
                {"path", "digest", "artifacts", "select", "annotations"},
                {"path", "digest", "history", "select", "annotations"},
                {"path", "digest", "artifacts", "select", "annotations", "draft"},
            )
            or "draft" in ref
            and (state.get("version") != FORMAT or ref.get("select"))
            or "history" in ref
            and (state.get("version") != FORMAT or ref.get("select") or ref.get("annotations"))
            or not allowed_path(ref["path"])
            or not isinstance(ref["select"], list)
            or len(ref["select"]) > 32
            or not isinstance(ref["annotations"], dict)
            or not set(ref["annotations"]) <= ANNOTATIONS
        ):
            raise ValueError()
        path = tuple(ref["path"])
        if any(path[: len(previous)] == previous or previous[: len(path)] == path for previous in seen):
            raise ValueError()
        seen.add(path)
        parent, slot = graft_slot(state, path, ValueError())
        if fields is not None and path[0] not in fields:
            continue
        value = (
            store.restore_history(ref["digest"], ref["history"])
            if "history" in ref
            else store.get(ref["digest"], ref["artifacts"])
        )
        for component in ref["select"]:
            if not (isinstance(component, str) or type(component) is int and component >= 0):
                raise ValueError()
            value = value[component]
        value = annotated(value, ref["annotations"], ref.get("draft"))
        parent[slot] = value
        if "history" in ref:
            store.history_paths.add(path)


class Cache:
    """Bounded process-local LRU of verified immutable bytes, scoped to their provenance."""

    def __init__(self, max_bytes=32 * 1024 * 1024):
        """Keep only accepted content; eviction and process restart simply cause re-verification."""
        self.limit, self.size = max_bytes, 0
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def get(self, scope, key, numbers):
        """Return a copy only for the exact issue, author, digest and artifact identities."""
        identity = (*scope, key, tuple(numbers))
        with self.lock:
            if identity not in self.entries:
                return None
            value, _ = self.entries[identity]
            self.entries.move_to_end(identity)
            return deepcopy(value)

    def put(self, scope, key, numbers, value):
        """Remember already verified evidence without retaining mutable caller-owned objects."""
        size = len(json.dumps(value).encode())
        if size > self.limit:
            return
        identity = (*scope, key, tuple(numbers))
        with self.lock:
            old = self.entries.pop(identity, None)
            self.size -= old[1] if old else 0
            self.entries[identity] = (deepcopy(value), size)
            self.size += size
            while self.size > self.limit:
                _, (_, removed) = self.entries.popitem(last=False)
                self.size -= removed


def part_body(key, index, count, part, footer, language):
    """Render a complete immutable part, including its verification marker and links."""
    fence = markup.fence(part)
    return (
        f"{MARKER}{key}:{index}:{count} -->\n### Report data · part {index} of {count}\n\n"
        f"{fence}{language}\n{part}\n{fence}\n{footer}"
    )


def parse_part(body, key):
    """Read the existing part layout for decoding and exact interrupted-write recovery."""
    match = re.fullmatch(
        re.escape(MARKER + key + ":")
        + r"(\d+):(\d+) -->\n### Report data · part \d+ of \d+\n\n(`{3,})(json|text)\n(.*?)\n\3\n(?:\nReferences:\n.*)?",
        body,
        re.S,
    )
    if not match:
        raise ReportDataError("Malformed review data part.")
    return int(match[1]), int(match[2]), match[5], match[4]


def part_bodies(key, encoded, footer, first=None):
    """Fit new parts by complete UTF-8 size; preserve an existing write's exact layout."""
    if first and (first[0] != 1 or first[1] < 1 or not first[2]):
        raise ReportDataError("Invalid first review data part.")
    width = len(first[2]) if first else CHUNK
    while True:
        parts = [encoded[i : i + width] for i in range(0, len(encoded), width)]
        language = first[3] if first else "json" if len(parts) == 1 and len(encoded) <= LEGACY_CHUNK else "text"
        bodies = [part_body(key, i, len(parts), part, footer, language) for i, part in enumerate(parts, 1)]
        if first:
            if first[0] != 1 or first[1] != len(parts) or first[2] != parts[0]:
                raise ReportDataError("Stored review data conflicts with the pending write.")
            return bodies
        largest = max(len(body.encode()) for body in bodies)
        if largest <= protocol.ARTIFACT_BODY_BYTES:
            return bodies
        width = max(1, width * (protocol.ARTIFACT_BODY_BYTES - 1024) // largest)


def audit_envelope(payload, store, repository, issue):
    """Hydrate optional immutable audit metadata after checking the outer task identity."""
    if payload["repository"] != repository or payload["state"]["issue"] != issue:
        raise ValueError("Audit identity mismatch")
    if "audit_data" not in payload:
        return payload
    ref = payload["audit_data"]
    if (
        set(payload) != {"repository", "state", "audit_data"}
        or payload["state"] != {"version": FORMAT, "issue": issue}
        or not isinstance(ref, dict)
        or set(ref) != {"digest", "artifacts"}
    ):
        raise ValueError("Invalid audit metadata reference")
    restored = store.get(ref["digest"], ref["artifacts"])
    if (
        not isinstance(restored, dict)
        or "audit_data" in restored
        or restored.get("repository") != repository
        or not isinstance(restored.get("state"), dict)
        or restored["state"].get("issue") != issue
        or restored["state"].get("version") != FORMAT
    ):
        raise ValueError("Invalid audit metadata identity")
    return restored


class Store:
    """Write once, recover by marker, and verify every part before decoding a value."""

    def __init__(self, github, issue, author, repository=None, cache=None, *, write_version=3):
        """Bind all storage and discovery to this task's issue and controller identity."""
        self.gh, self.issue, self.author = github, issue, author
        self.repository = (
            repository or getattr(github, "repository", None) or getattr(github, "config", {}).get("repository")
        )
        self.cache = cache
        self.scope = (self.repository, issue, author)
        self.archive_index = None
        self.items = None
        self.verified = {}
        self.numbers = {}
        self.reading = set()
        self.history_paths = set()
        self.receipt_references = {}
        self.write_version = write_version
        self.completion = None

    def collection(self):
        """Lazily discover artifacts once per controller invocation."""
        if self.items is None:
            self.items = self.gh.pages(f"issues/{self.issue}/artifacts")
        return self.items

    def read(self, item):
        """Reject edits, replacements, and unexpected authorship of an immutable part."""
        response = self.gh.repo(f"issues/{self.issue}/artifacts/{item['number']}")
        return self.verify(response, item)

    def verify(self, response, item=None):
        """Verify a direct reference without listing all historical artifacts."""
        artifact, version = response["artifact"], response["versions"][0]
        item = item or artifact
        if (
            artifact["id"] != item["id"]
            or artifact["number"] != item["number"]
            or artifact["type"] != "generic"
            or artifact["creator"]["login"] != self.author
            or artifact["updated_by_actor"]["login"] != self.author
            or version["version"] != 1
            or version["actor"]["login"] != self.author
            or version["body"] != artifact["body"]
            or version["name"] != artifact["name"]
            or artifact["name"] != item["name"]
            or artifact["body"] != item["body"]
        ):
            raise ReportDataError("Stored review data changed; restore it before continuing.")
        return artifact

    def matches(self, prefix):
        """Ignore user imitations; duplicate controller markers are conflicts."""
        return owned(self.collection(), self.author, prefix, "creator")

    def shared_reference(self, value, *, prepare=True):
        """Keep per-attempt annotations in the caller's receipt, sharing just immutable evidence."""
        core, annotations, draft = projection(value)
        ref = self.reference(core, prepare=prepare) | {"annotations": deepcopy(annotations)}
        if draft is not None:
            ref["draft"] = deepcopy(draft)
        return ref

    def restore_reference(self, ref):
        """Read canonical data and reconstruct the receipt's exact annotation snapshot."""
        return annotated(self.get(ref["digest"], ref["artifacts"]), ref.get("annotations", {}), ref.get("draft"))

    def bind_receipts(self, state):
        """Index controller-owned publication references; verify their bytes only when reused."""
        entries = list(state.get("reports", [])) + list(state.get("conversation", {}).get("receipts", {}).values())
        for entry in entries:
            ref = entry.get("data") if isinstance(entry, dict) else None
            if (
                isinstance(ref, dict)
                and isinstance(ref.get("digest"), str)
                and re.fullmatch(protocol.DIGEST_PATTERN, ref["digest"])
            ):
                self.receipt_references.setdefault(ref["digest"], deepcopy(ref.get("artifacts")))

    def reference(self, value, *, prepare, legacy=False):
        """Describe immutable evidence, optionally confirming it before a checkpoint write."""
        key = self.put(value, legacy=legacy) if prepare else digest(value)
        return {"digest": key, "artifacts": self.numbers.get(key, [])}

    def checkpoint_reference(self, item, *, prepare=False):
        """Keep a growing list's directory and unfinished tail in the mutable checkpoint."""
        ref = {k: item[k] for k in ("path", "digest", "select", "annotations")}
        if "draft" in item:
            ref["draft"] = item["draft"]
        value = item["value"]
        if prepare and item["digest"] in self.receipt_references:
            self.put(value)
        if (
            prepare
            and self.write_version == 2
            and isinstance(value, list)
            and tuple(item["path"]) not in self.history_paths
            and not self.numbers.get(item["digest"])
            and self.matches(f"{MARKER}{item['digest']}:")
        ):
            self.put(value)
        if isinstance(value, list) and not self.numbers.get(item["digest"]):
            end = len(value) // BLOCK_ENTRIES * BLOCK_ENTRIES
            blocks = [
                self.reference(value[i : i + BLOCK_ENTRIES], prepare=prepare) | {"count": BLOCK_ENTRIES}
                for i in range(0, end, BLOCK_ENTRIES)
            ]
            result = ref | {
                "history": {
                    "blocks": blocks,
                    "length": end,
                    "size": BLOCK_ENTRIES,
                    "tail": self.encode(value[end:], blocks=False, prepare=prepare),
                }
            }
            if prepare:
                self.history_paths.add(tuple(item["path"]))
            return result
        return ref | self.reference(value, prepare=prepare)

    def restore_history(self, key, record):
        """Verify the closed block directory, short inline tail and complete history digest."""
        invalid = ReportDataError("Malformed checkpoint history.")
        if (
            not isinstance(record, dict)
            or set(record) != {"blocks", "length", "size", "tail"}
            or type(record["size"]) is not int
            or record["size"] <= 0
            or type(record["length"]) is not int
            or record["length"] < 0
            or record["length"] % record["size"]
            or not record["length"]
            and record["blocks"] != []
            or not isinstance(record["tail"], dict)
            or set(record["tail"]) != {"value", "references"}
            or not isinstance(record["tail"]["value"], list)
            or len(record["tail"]["value"]) >= record["size"]
            or not isinstance(record["tail"]["references"], list)
        ):
            raise invalid
        # Validate tail slots before fetching any closed blocks or report evidence.
        tail = record["tail"]
        slots = set()
        for ref in tail["references"]:
            if not isinstance(ref, dict) or set(ref) not in (
                {"path", "digest", "artifacts"},
                {"path", "digest", "artifacts", "annotations"},
                {"path", "digest", "artifacts", "annotations", "draft"},
            ):
                raise invalid
            path = tuple(ref["path"])
            if any(path[: len(old)] == old or old[: len(path)] == path for old in slots):
                raise invalid
            slots.add(path)
            try:
                graft_slot(tail["value"], ref["path"], invalid)
            except (KeyError, IndexError, TypeError):
                raise invalid from None
        value = self.restore_blocks({k: record[k] for k in ("blocks", "length", "size")}) if record["length"] else []
        value.extend(self.decode_record(deepcopy(tail)))
        if digest(value) != key:
            raise ReportDataError("Checkpoint history failed digest verification.")
        return value

    def encode(self, value, *, blocks=True, prepare=True, projected=True):
        """Reference canonical reports and fixed-position blocks of detached collections."""
        if blocks and isinstance(value, list) and len(value) > BLOCK_ENTRIES:
            refs = []
            for start in range(0, len(value), BLOCK_ENTRIES):
                block = value[start : start + BLOCK_ENTRIES]
                refs.append(self.reference(block, prepare=prepare, legacy=not projected) | {"count": len(block)})
            return {"blocks": refs, "length": len(value), "size": BLOCK_ENTRIES}
        if is_report(value):
            annotations = report_annotations(value)
            if annotations:
                core = report_content(value)
                return {
                    "report": self.reference(core, prepare=prepare, legacy=not projected),
                    "annotations": annotations,
                }
        refs = []

        def visit(child, path):
            """Keep containers inline, extracting whole nested reports once."""
            if path and (is_report(child) or projected and projection(child)[2] is not None):
                ref = (
                    self.shared_reference(child, prepare=prepare)
                    if projected
                    else self.reference(child, prepare=prepare, legacy=True)
                )
                refs.append({"path": path} | ref)
                return None
            if isinstance(child, dict):
                return {k: visit(v, path + [k]) for k, v in child.items()}
            if isinstance(child, list):
                return [visit(v, path + [i]) for i, v in enumerate(child)]
            return child

        return {"value": visit(value, []), "references": refs}

    def put(self, value, *, legacy=False):
        """Reuse verified references, or upload fresh content without scanning for old copies."""
        key = digest(value)
        if key in self.verified:
            return key
        if key in self.receipt_references:
            numbers = self.receipt_references[key]
            if numbers is None:
                raise ReportDataError("Published review data reference is missing its artifact identities.")
            self.get(key, numbers)
            return key
        if not legacy and self.write_version == 3:
            return self.put_shared(value, key, [], fresh=True)
        # Parse old archive bodies once per invocation, only when discovering a new value.
        if self.archive_index is None:
            self.archive_index = {}
            for item in self.collection():
                body = item.get("body", "")
                if item.get("creator", {}).get("login") != self.author or not body.startswith(protocol.EVENT_MARKER):
                    continue
                match = ARCHIVE_JSON.search(body)
                if match:
                    try:
                        archived = json.loads(match[2])
                        self.archive_index.setdefault(digest(archived), item)
                        if is_report(archived):
                            core = report_content(archived)
                            self.archive_index.setdefault(digest(core), item)
                    except ValueError:
                        pass
        if key in self.archive_index:
            item = self.read(self.archive_index[key])
            self.remember(key, [item["number"]], value)
            return key
        matches = self.matches(f"{MARKER}{key}:")
        uploads = [item for item in matches if report_artifact.ATTEMPT_DATA in item["body"]]
        if uploads:
            complete = self.upload_sets(key, uploads)
            if complete:
                numbers, stored, _ = min(complete, key=lambda result: result[0][0])
                self.remember(key, numbers, stored)
                return key
            # An old queued publication may outlive a partial new upload of the same source.
            matches = [item for item in matches if report_artifact.ATTEMPT_DATA not in item["body"]]
            if not matches:
                return self.put_shared(value, key, [], fresh=True)
        modern = [item for item in matches if report_artifact.DATA in item["body"]]
        if modern or not matches and not legacy:
            if modern and len(modern) != len(matches):
                raise ReportDataError("Mixed review data encodings.")
            return self.put_shared(value, key, modern)
        first = self.matches(f"{MARKER}{key}:1:")
        if len(first) > 1:
            raise ReportDataError("Duplicate review data parts.")
        if not first and matches:
            raise ReportDataError("Stored review data is missing its first part.")
        layout = parse_part(self.read(first[0])["body"], key) if first else None
        block_prefix = record_json({"blocks": []}).partition("]")[0]
        blocks = not first or "\n" + block_prefix in first[0]["body"]
        record = self.encode(value, blocks=blocks, projected=False)
        refs = record.get("references", []) + record.get("blocks", [])
        refs += [record["report"]] if "report" in record else []
        links = []
        if self.repository:
            root = protocol.artifact_url(self.repository, self.issue)
            for ref in refs:
                links += [
                    f"- [Referenced data {ref['digest'][:12]}, part {i}]({root}{n})"
                    for i, n in enumerate(ref["artifacts"], 1)
                ]
        footer = ""
        for line in dict.fromkeys(links):
            if len((footer + line).encode()) > 8000:
                footer += "Additional references are listed in the JSON record.\n"
                break
            footer += line + "\n"
        footer = "\nReferences:\n" + footer if footer else ""
        encoded = record_json(record)
        bodies = part_bodies(key, encoded, footer, layout)
        numbers = []
        for index, body in enumerate(bodies, 1):
            prefix = f"{MARKER}{key}:{index}:"
            name = f"Concurro review data {key[:32]} part {index}"
            matches = self.matches(prefix)
            if len(matches) > 1 or matches and (matches[0]["body"] != body or matches[0]["name"] != name):
                raise ReportDataError("Stored review data conflicts with the pending write.")
            if matches:
                item = matches[0]
            else:
                item = self.gh.repo(
                    f"issues/{self.issue}/artifacts", "POST", {"type": "generic", "name": name, "body": body}
                )
                self.collection().append(item)
            self.read(item)
            numbers.append(item["number"])
        self.remember(key, numbers, value)
        return key

    def shared_parts(self, key, items, *, verified=False):
        """Verify a consistent layout and accept only byte-identical unedited duplicate parts."""
        parts, layout = {}, None
        for candidate in items:
            item = candidate if verified else self.read(candidate)
            part = report_artifact.parse(item["body"], key)
            if layout is not None and layout != part["layout"]:
                raise ReportDataError("Conflicting shared report layouts.")
            layout = part["layout"]
            index = part["index"]
            if item["name"] != f"Concurro review data {key[:32]} part {index}":
                raise ReportDataError("Shared report name changed.")
            old = parts.get(index)
            if old and any(old[0][k] != item[k] for k in ("body", "name")):
                raise ReportDataError("Conflicting shared report copies.")
            if old is None or item["number"] < old[0]["number"]:
                parts[index] = (item, part)
        return layout, parts

    def put_shared(self, value, key, matches, *, fresh=False):
        """Upload a fresh nonce or finish a legacy continuation-first layout."""
        try:
            layout, parts = self.shared_parts(key, matches)
            record = self.encode(value, blocks=layout["blocks"] if layout else True)
            completion = self.completion[1] if fresh and self.completion and self.completion[0] == key else None
            if completion is not None:
                record["completion"] = deepcopy(completion)
            encoded = record_json(record)
            view = (
                report_artifact.stored_view(
                    parts[1][0]["body"], layout, parts[1][1]["continuations"], self.repository, self.issue
                )
                if 1 in parts
                else report_artifact.view_for(value, layout)
            )
            if layout is None:
                upload = (
                    {"nonce": uuid.uuid4().hex, "attempt": digest(completion["binding"]) if completion else None}
                    if fresh
                    else {}
                )
                layout = report_artifact.pack(key, encoded, view, True, self.repository, self.issue, **upload)
                if layout["renderer"] == 2:
                    view = report_artifact.COMPACT_VIEW
            if digest(view) != layout["view"]:
                raise ReportDataError("Shared report renderer changed.")
            if not all(
                artifact_capacity.fits(b, v)
                for b, v in report_artifact.packing_bodies(key, encoded, layout, view, self.repository, self.issue)
            ):
                raise artifact_capacity.CapacityError(
                    "Frozen artifact layout exceeds the current response budget; restore its original allowance."
                )
            slices = report_artifact.slices(encoded, layout)
            numbers = [None] * len(slices)
            if 1 in parts:
                # Existing entry links pin continuation identities, including valid duplicate copies.
                for index, number in enumerate(parts[1][1]["continuations"], 2):
                    item = self.verify(self.gh.repo(f"issues/{self.issue}/artifacts/{number}"))
                    parsed = report_artifact.parse(item["body"], key)
                    if parsed["index"] != index or parsed["layout"] != layout:
                        raise ReportDataError("Shared report continuation changed.")
                    parts[index] = (item, parsed)
            for index in [*range(2, len(slices) + 1), 1]:
                body = report_artifact.body(
                    key, index, layout, slices[index - 1], view, numbers[1:], self.repository, self.issue
                )
                visible = report_artifact.readable(index, len(slices), view, numbers[1:], self.repository, self.issue)
                if not artifact_capacity.fits(body, visible):
                    raise artifact_capacity.CapacityError("Shared report part exceeds the artifact response budget.")
                name = f"Concurro review data {key[:32]} part {index}"
                if index in parts:
                    item = parts[index][0]
                    if item["body"] != body or item["name"] != name:
                        raise ReportDataError("Shared report bytes changed.")
                else:
                    item = self.gh.repo(
                        f"issues/{self.issue}/artifacts", "POST", {"type": "generic", "name": name, "body": body}
                    )
                    if self.items is not None:
                        self.items.append(item)
                self.read(item)
                numbers[index - 1] = item["number"]
            self.remember(key, numbers, value)
            return key
        except artifact_capacity.CapacityError as error:
            raise ReportDataError(str(error)) from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise ReportDataError("Malformed or conflicting shared report data.") from None

    def load_shared(self, key, items, numbers, *, verified=False, completion=False):
        """Read a complete shared artifact without confusing content identity with event identity."""
        layout, parts = self.shared_parts(key, items, verified=verified)
        if layout is None or set(parts) != set(range(1, len(layout["ends"]) + 1)):
            raise ReportDataError("Shared report data is incomplete.")
        actual = [parts[1][0]["number"], *parts[1][1]["continuations"]]
        if numbers is not None and actual != numbers:
            raise ReportDataError("Shared report identities changed.")
        for index, number in enumerate(actual, 1):
            if parts[index][0]["number"] != number:
                item = self.verify(self.gh.repo(f"issues/{self.issue}/artifacts/{number}"))
                parsed = report_artifact.parse(item["body"], key)
                if parsed["index"] != index or parsed["layout"] != layout:
                    raise ReportDataError("Shared report continuation changed.")
                parts[index] = (item, parsed)
        encoded = "".join(parts[i][1]["part"] for i in range(1, len(actual) + 1))
        report_artifact.slices(encoded, layout)
        record = json.loads(encoded)
        receipt = record.get("completion")
        if layout["version"] == 3 and layout["attempt"] != (digest(receipt["binding"]) if receipt else None):
            raise ReportDataError("Attempt completion identity changed.")
        value = self.decode_record(record)
        if digest(value) != key:
            raise ReportDataError("Shared report digest mismatch.")
        view = report_artifact.stored_view(parts[1][0]["body"], layout, actual[1:], self.repository, self.issue)
        for index, (item, record) in parts.items():
            expected = report_artifact.body(
                key, index, layout, record["part"], view, actual[1:], self.repository, self.issue
            )
            if item["body"] != expected:
                raise ReportDataError("Shared report body mismatch.")
        self.remember(key, actual, value)
        return (value, receipt) if completion else value

    def upload_sets(self, key, items):
        """Verify independent uploads; partial sets never borrow parts from another nonce."""
        groups, legacy = {}, []
        for item in items:
            if report_artifact.ATTEMPT_DATA in item["body"] or report_artifact.DATA in item["body"]:
                parsed = report_artifact.parse(item["body"], key)
                groups.setdefault(parsed["layout"].get("nonce"), []).append(item)
            else:
                legacy.append(item)
        results = []
        for group in groups.values():
            layout, parts = self.shared_parts(key, group)
            if set(parts) != set(range(1, len(layout["ends"]) + 1)):
                continue
            value, receipt = self.load_shared(key, [p[0] for p in parts.values()], None, verified=True, completion=True)
            results.append((self.numbers[key], value, receipt))
        if legacy:
            ordered = sorted(legacy, key=lambda item: parse_part(item["body"], key)[0])
            numbers = [item["number"] for item in ordered]
            results.append((numbers, self.load(key, numbers), None))
        return results

    def recover_attempt(self, binding):
        """Discover a completed paid result once per invocation, never from partial evidence."""
        tag = digest(binding)
        grouped = {}
        try:
            for item in self.collection():
                body = item.get("body", "")
                marker = body.partition(report_artifact.ATTEMPT_DATA)[0]
                if (
                    item.get("creator", {}).get("login") == self.author
                    and body.startswith(MARKER)
                    and report_artifact.ATTEMPT_DATA in body
                    and marker.endswith(":" + tag + " -->")
                ):
                    key = marker[len(MARKER) :].split(":", 1)[0]
                    grouped.setdefault(key, []).append(item)
            found = []
            for key, items in grouped.items():
                for numbers, value, receipt in self.upload_sets(key, items):
                    if receipt is None or receipt["binding"] != binding:
                        raise ReportDataError("Attempt completion binding changed.")
                    found.append((numbers, key, value, receipt))
            if not found:
                return None
            if len({digest([key, receipt]) for _, key, _, receipt in found}) != 1:
                raise ReportDataError("Conflicting results for one model attempt.")
            numbers, key, value, receipt = min(found, key=lambda result: result[0][0])
            self.remember(key, numbers, value)
            return value, receipt
        except (KeyError, IndexError, TypeError, ValueError, NotFound):
            raise ReportDataError("Malformed or incomplete attempt artifacts.") from None

    def remember(self, key, numbers, value):
        """Share a verified immutable value across ticks without sharing mutable state."""
        self.verified[key], self.numbers[key] = deepcopy(value), numbers
        if self.cache is not None:
            self.cache.put(self.scope, key, numbers, value)

    def get(self, key, numbers=None):
        """Verify parts and nested references before exposing any recovered report."""
        if not isinstance(key, str) or not re.fullmatch(protocol.DIGEST_PATTERN, key):
            raise ReportDataError("Invalid review data reference.")
        if numbers is not None and (
            not isinstance(numbers, list)
            or not numbers
            or any(type(n) is not int or n <= 0 for n in numbers)
            or len(set(numbers)) != len(numbers)
        ):
            raise ReportDataError("Invalid review artifact identities.")
        if key not in self.verified and self.cache is not None and numbers:
            cached = self.cache.get(self.scope, key, numbers)
            if cached is not None:
                self.verified[key], self.numbers[key] = cached, numbers
        prior = None
        if key in self.verified:
            if numbers is None or numbers == self.numbers[key]:
                return deepcopy(self.verified[key])
            # Two durable references may name independent uploads of identical content.
            # Verify the second identity without replacing the first pin or trusting its digest alone.
            prior = (self.verified[key], self.numbers[key])
        if key in self.reading or len(self.reading) >= 16:
            raise ReportDataError("Cyclic or excessively nested review references.")
        self.reading.add(key)
        try:
            return self.load(key, numbers)
        except (KeyError, IndexError, TypeError, ValueError, NotFound):
            raise ReportDataError("Malformed or incomplete review data.") from None
        finally:
            self.reading.remove(key)
            if prior is not None:
                self.verified[key], self.numbers[key] = prior

    def load(self, key, numbers):
        """Decode verified immutable parts, including earlier format-3 report archives."""
        items = None
        if numbers:
            items = []
            for number in numbers:
                item = self.verify(self.gh.repo(f"issues/{self.issue}/artifacts/{number}"))
                if item["number"] != number:
                    raise ReportDataError("Review artifact identities changed.")
                items.append(item)
            if len(items) == 1 and items[0]["body"].startswith(protocol.EVENT_MARKER):
                body = items[0]["body"]
                match = ARCHIVE_JSON.search(body)
                value = json.loads(match[2]) if match else None
                if digest(value) != key and is_report(value):
                    value = report_content(value)
                if digest(value) != key:
                    raise ReportDataError("Stored report failed digest verification.")
                self.remember(key, numbers, value)
                return value
        discovered = items if items is not None else self.matches(MARKER + key + ":")
        if any(report_artifact.ATTEMPT_DATA in item["body"] for item in discovered):
            if numbers is not None:
                return self.load_shared(key, discovered, numbers, verified=True)
            candidates = self.upload_sets(key, discovered)
            if not candidates:
                raise ReportDataError("Shared report data is incomplete.")
            actual, value, _ = min(candidates, key=lambda result: result[0][0])
            self.remember(key, actual, value)
            return value
        if any(report_artifact.DATA in item["body"] for item in discovered):
            return self.load_shared(key, discovered, numbers, verified=items is not None)
        chunks, identities, count = {}, {}, None
        for item in discovered:
            body = item["body"] if items is not None else self.read(item)["body"]
            index, total, part, _ = parse_part(body, key)
            if not 1 <= index <= total or count not in (None, total) or index in chunks:
                raise ReportDataError("Duplicate or inconsistent review data parts.")
            count, chunks[index], identities[index] = total, part, item["number"]
        if count is None or set(chunks) != set(range(1, count + 1)):
            raise ReportDataError("Stored review data is missing; restore it before continuing.")
        actual = [identities[i] for i in range(1, count + 1)]
        if numbers is not None and actual != numbers:
            raise ReportDataError("Review artifact identities changed.")
        record = json.loads("".join(chunks[i] for i in range(1, count + 1)))
        value = self.decode_record(record)
        if digest(value) != key:
            raise ReportDataError("Stored review data failed digest verification.")
        self.remember(key, actual, value)
        return value

    def decode_record(self, record):
        """Restore the shared nested-report encoding used by artifacts and inline history tails."""
        if "blocks" in record:
            value = self.restore_blocks(record)
        elif "report" in record:
            ref = record["report"]
            value = self.get(ref["digest"], ref["artifacts"])
            if not isinstance(value, dict) or set(value) & set(record["annotations"]):
                raise ReportDataError("Report annotations overwrite evidence.")
            value.update(record["annotations"])
        else:
            value = record["value"]
        for ref in record.get("references", []):
            parent, slot = graft_slot(value, ref["path"], ReportDataError("Overlapping report references."))
            parent[slot] = self.restore_reference(ref)
        return value

    def restore_blocks(self, record):
        """Validate the whole layout before fetching and concatenating verified blocks."""
        invalid = ReportDataError("Malformed review data blocks.")
        if (
            set(record) != {"blocks", "length", "size"}
            or type(record["size"]) is not int
            or record["size"] <= 0
            or type(record["length"]) is not int
            or record["length"] <= 0
            or not isinstance(record["blocks"], list)
            or len(record["blocks"]) != (record["length"] + record["size"] - 1) // record["size"]
        ):
            raise invalid
        for index, ref in enumerate(record["blocks"]):
            if (
                not isinstance(ref, dict)
                or set(ref) != {"digest", "artifacts", "count"}
                or type(ref["count"]) is not int
                or ref["count"] != min(record["size"], record["length"] - index * record["size"])
            ):
                raise invalid
        value = []
        for ref in record["blocks"]:
            block = self.get(ref["digest"], ref["artifacts"])
            if not isinstance(block, list) or len(block) != ref["count"]:
                raise invalid
            value.extend(block)
        return value
