"""Canonical immutable report artifacts shared by the audit and rounds table."""

from collections import OrderedDict
from copy import deepcopy
import json
import re
import threading

from policy import Blocked, digest
from github import NotFound

MARKER = "<!-- concurro:report-data:v1:"
CHUNK = 16000
MAX_PARTS = 128
INLINE = 12000
FIELDS = {
    "qc",
    "qc_history",
    "design_votes",
    "design_history",
    "design_feedback",
    "objections",
    "reviews",
    "review_history",
    "review_event",
    "findings",
    "pr_outbox",
    "reason",
    "outcomes",
    "prior_outcomes",
    "reopen_history",
    "invalidations",
    "requests",
    "review_draft",
    "review_batch",
}


class ReportDataError(Blocked):
    """Missing or changed durable review data must never authorise new work."""


ANNOTATIONS = {"classification", "agent", "head", "model", "input_revision", "sequence", "finding_ids"}


def is_report(value):
    """Recognise the engine's complete assessment records."""
    return isinstance(value, dict) and (
        {"decision", "rationale"} <= value.keys() or {"verdict", "summary"} <= value.keys()
    )


def packed(state):
    """Reference evidence, keeping changing collections and report annotations inline."""
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
            core = {k: v for k, v in value.items() if k not in ANNOTATIONS}
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
    if state.get("operation", {}).get("stage") in ("plan_qc", "review") and "response" in state["operation"]:
        roots["operation"] = {"response": state["operation"]["response"]}
    collect(roots)
    detached = []

    def visit(value, path):
        """Detach reports or large leaves, never a mutable history collection."""
        if (
            path == ["review_draft"]
            or path[0] == "review_batch"
            and (path[-1] == "review_draft" or path[-2:] == ["operation", "response"] and not is_report(value))
        ):
            detached.append({"path": path, "digest": digest(value), "value": value, "select": [], "annotations": {}})
            return None
        if is_report(value):
            core = {k: v for k, v in value.items() if k not in ANNOTATIONS}
            key = digest(core)
            if key in sources:
                detached.append(
                    {
                        "path": path,
                        "digest": key,
                        "value": core,
                        "select": [],
                        "annotations": {k: v for k, v in value.items() if k in ANNOTATIONS},
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
            return [visit(v, path + [i]) for i, v in enumerate(value)]
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


class Store:
    """Write once, recover by marker, and verify every part before decoding a value."""

    def __init__(self, github, issue, author, repository=None, cache=None):
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
        return [
            item
            for item in self.collection()
            if item.get("creator", {}).get("login") == self.author and item.get("body", "").startswith(prefix)
        ]

    def encode(self, value):
        """Replace nested report copies with references to their canonical artifacts."""
        if is_report(value):
            annotations = {k: v for k, v in value.items() if k in ANNOTATIONS}
            if annotations:
                core = {k: v for k, v in value.items() if k not in annotations}
                key = self.put(core)
                return {"report": {"digest": key, "artifacts": self.numbers[key]}, "annotations": annotations}
        refs = []

        def visit(child, path):
            """Keep containers inline, extracting whole nested reports once."""
            if path and is_report(child):
                key = self.put(child)
                refs.append({"path": path, "digest": key, "artifacts": self.numbers[key]})
                return None
            if isinstance(child, dict):
                return {k: visit(v, path + [k]) for k, v in child.items()}
            if isinstance(child, list):
                return [visit(v, path + [i]) for i, v in enumerate(child)]
            return child

        return {"value": visit(value, []), "references": refs}

    def put(self, value):
        """Confirm immutable parts before referencing them; reuse earlier report archives."""
        key = digest(value)
        if key in self.verified:
            return key
        # Parse old archive bodies once per invocation, only when discovering a new value.
        if self.archive_index is None:
            self.archive_index = {}
            for item in self.collection():
                body = item.get("body", "")
                if item.get("creator", {}).get("login") != self.author or not body.startswith(
                    "<!-- concurro:event:v1:"
                ):
                    continue
                match = re.search(r"\n(`{3,})json\n(.*)\n\1$", body, re.S)
                if match:
                    try:
                        archived = json.loads(match[2])
                        self.archive_index.setdefault(digest(archived), item)
                        if is_report(archived):
                            core = {k: v for k, v in archived.items() if k not in ANNOTATIONS}
                            self.archive_index.setdefault(digest(core), item)
                    except ValueError:
                        pass
        if key in self.archive_index:
            item = self.read(self.archive_index[key])
            self.remember(key, [item["number"]], value)
            return key
        record = self.encode(value)
        refs = record.get("references", []) + ([record["report"]] if "report" in record else [])
        links = []
        if self.repository:
            root = f"https://github.com/{self.repository}/issues/{self.issue}/artifacts/"
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
        encoded = json.dumps(record, sort_keys=True, indent=2)
        parts = [encoded[i : i + CHUNK] for i in range(0, len(encoded), CHUNK)]
        if len(parts) > MAX_PARTS:
            raise ReportDataError("Review data exceeds the storage transport capacity.")
        numbers = []
        for index, part in enumerate(parts, 1):
            prefix = f"{MARKER}{key}:{index}:"
            fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", part)), default=0) + 1)
            body = (
                f"{prefix}{len(parts)} -->\n### Report data · part {index} of {len(parts)}\n\n"
                + fence
                + ("json" if len(parts) == 1 else "text")
                + "\n"
                + part
                + "\n"
                + fence
                + "\n"
                + footer
            )
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

    def remember(self, key, numbers, value):
        """Share a verified immutable value across ticks without sharing mutable state."""
        self.verified[key], self.numbers[key] = deepcopy(value), numbers
        if self.cache is not None:
            self.cache.put(self.scope, key, numbers, value)

    def get(self, key, numbers=None):
        """Verify parts and nested references before exposing any recovered report."""
        if not isinstance(key, str) or not re.fullmatch("[a-f0-9]{64}", key):
            raise ReportDataError("Invalid review data reference.")
        if numbers is not None and (
            not isinstance(numbers, list)
            or not 1 <= len(numbers) <= MAX_PARTS
            or any(type(n) is not int or n <= 0 for n in numbers)
            or len(set(numbers)) != len(numbers)
        ):
            raise ReportDataError("Invalid review artifact identities.")
        if key not in self.verified and self.cache is not None and numbers:
            cached = self.cache.get(self.scope, key, numbers)
            if cached is not None:
                self.verified[key], self.numbers[key] = cached, numbers
        if key in self.verified:
            if numbers is not None and numbers != self.numbers[key]:
                raise ReportDataError("Review artifact identities changed.")
            return deepcopy(self.verified[key])
        if key in self.reading or len(self.reading) >= 16:
            raise ReportDataError("Cyclic or excessively nested review references.")
        self.reading.add(key)
        try:
            return self.load(key, numbers)
        except (KeyError, IndexError, TypeError, ValueError, NotFound):
            raise ReportDataError("Malformed or incomplete review data.") from None
        finally:
            self.reading.remove(key)

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
            if len(items) == 1 and items[0]["body"].startswith("<!-- concurro:event:v1:"):
                body = items[0]["body"]
                match = re.search(r"\n(`{3,})json\n(.*)\n\1$", body, re.S)
                value = json.loads(match[2]) if match else None
                if digest(value) != key and is_report(value):
                    value = {k: v for k, v in value.items() if k not in ANNOTATIONS}
                if digest(value) != key:
                    raise ReportDataError("Stored report failed digest verification.")
                self.remember(key, numbers, value)
                return value
        chunks, identities, count = {}, {}, None
        for item in items if items is not None else self.matches(MARKER + key + ":"):
            body = item["body"] if items is not None else self.read(item)["body"]
            match = re.fullmatch(
                re.escape(MARKER + key + ":")
                + r"(\d+):(\d+) -->\n### Report data · part \d+ of \d+\n\n(`{3,})(?:json|text)\n(.*?)\n\3\n(?:\nReferences:\n.*)?",
                body,
                re.S,
            )
            if not match:
                raise ReportDataError("Malformed review data part.")
            index, total = int(match[1]), int(match[2])
            if not 1 <= index <= total <= MAX_PARTS or count not in (None, total) or index in chunks:
                raise ReportDataError("Duplicate or inconsistent review data parts.")
            count, chunks[index], identities[index] = total, match[4], item["number"]
        if count is None or set(chunks) != set(range(1, count + 1)):
            raise ReportDataError("Stored review data is missing; restore it before continuing.")
        actual = [identities[i] for i in range(1, count + 1)]
        if numbers is not None and actual != numbers:
            raise ReportDataError("Review artifact identities changed.")
        record = json.loads("".join(chunks[i] for i in range(1, count + 1)))
        if "report" in record:
            ref = record["report"]
            value = self.get(ref["digest"], ref["artifacts"])
            if not isinstance(value, dict) or set(value) & set(record["annotations"]):
                raise ReportDataError("Report annotations overwrite evidence.")
            value.update(record["annotations"])
        else:
            value = record["value"]
        for ref in record.get("references", []):
            parent = value
            for component in ref["path"][:-1]:
                parent = parent[component]
            if parent[ref["path"][-1]] is not None:
                raise ReportDataError("Overlapping report references.")
            parent[ref["path"][-1]] = self.get(ref["digest"], ref["artifacts"])
        if digest(value) != key:
            raise ReportDataError("Stored review data failed digest verification.")
        self.remember(key, actual, value)
        return value
