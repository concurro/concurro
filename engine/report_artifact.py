"""Version-two immutable parts: frozen layout, hidden canonical data and a shared readable view."""

import json
import re

import protocol
from policy import digest
from report_artifact_view_v1 import submitted as submitted

MARKER = "<!-- concurro:report-data:v1:"
DATA = "\n<!-- concurro:report-part:v2\n"


def body(key, index, layout, part, view, numbers, repository, issue):
    """Render one frozen part; only the entry carries the view and continuation identities."""
    count = len(layout["ends"])
    record = {"layout": layout, "index": index, "part": part, "continuations": numbers if index == 1 else []}
    encoded = json.dumps(record, sort_keys=True).replace(">", "\\u003e")
    prefix = f"{MARKER}{key}:{index}:{count}:{digest(layout)} -->"
    readable = view if index == 1 else f"### Stored task data · part {index} of {count}"
    if index == 1:
        readable += continuation_links(numbers, repository, issue)
    return prefix + DATA + encoded + "\n-->\n" + readable + "\n"


def continuation_links(numbers, repository, issue):
    """Render the version-two entry trailer, separately from the committed readable view."""
    if not numbers:
        return ""
    root = protocol.artifact_url(repository, issue) if repository else ""
    return "\n\n" + "\n".join(
        f"- [Data part {i}]({root}{n})" if root else f"- Data part {i}: artifact {n}" for i, n in enumerate(numbers, 2)
    )


def stored_view(text, layout, numbers, repository, issue):
    """Verify the stored entry's view digest and exact trailer without invoking any renderer."""
    _, closing, tail = text.partition("\n-->\n")
    trailer = continuation_links(numbers, repository, issue) + "\n"
    if not closing or not tail.endswith(trailer):
        raise ValueError("Shared report continuation links changed")
    view = tail[: -len(trailer)]
    if digest(view) != layout["view"]:
        raise ValueError("Shared report view mismatch")
    return view


def parse(text, key):
    """Validate the entire self-contained descriptor before any continuation is read."""
    marker, separator, tail = text.partition(DATA)
    match = re.fullmatch(re.escape(MARKER + key + ":") + r"(\d+):(\d+):(" + protocol.DIGEST_PATTERN + r") -->", marker)
    encoded, closing, view = tail.partition("\n-->\n")
    if not separator or not match or not closing or ">" in encoded:
        raise ValueError("Malformed shared report part")
    record = json.loads(encoded)
    layout = record["layout"]
    if (
        set(record) != {"layout", "index", "part", "continuations"}
        or set(layout) != {"version", "renderer", "ends", "record", "view", "blocks"}
        or layout["version"] != 2
        or layout["renderer"] != 1
        or type(layout["version"]) is not int
        or type(layout["renderer"]) is not int
        or type(layout["blocks"]) is not bool
        or not isinstance(layout["ends"], list)
        or not layout["ends"]
        or any(type(n) is not int or n <= 0 for n in layout["ends"])
        or any(a >= b for a, b in zip(layout["ends"], layout["ends"][1:]))
        or not all(
            isinstance(layout[k], str) and re.fullmatch(protocol.DIGEST_PATTERN, layout[k]) for k in ("record", "view")
        )
        or digest(layout) != match[3]
        or type(record["index"]) is not int
        or record["index"] != int(match[1])
        or not 1 <= record["index"] <= int(match[2]) == len(layout["ends"])
        or not isinstance(record["part"], str)
        or not isinstance(record["continuations"], list)
        or any(type(n) is not int or n <= 0 for n in record["continuations"])
        or len(set(record["continuations"])) != len(record["continuations"])
        or len(record["continuations"]) != (len(layout["ends"]) - 1 if record["index"] == 1 else 0)
    ):
        raise ValueError("Invalid shared report layout")
    start = layout["ends"][record["index"] - 2] if record["index"] > 1 else 0
    if len(record["part"]) != layout["ends"][record["index"] - 1] - start or not view.endswith("\n"):
        raise ValueError("Invalid shared report slice")
    return record


def slices(encoded, layout):
    """Apply recorded character boundaries without consulting today's packing allowance."""
    if len(encoded) != layout["ends"][-1] or digest(encoded) != layout["record"]:
        raise ValueError("Shared report record changed")
    return [encoded[start:end] for start, end in zip([0, *layout["ends"][:-1]], layout["ends"])]


def pack(key, encoded, view, blocks, width, repository, issue):
    """Choose a complete-body layout under the existing allowance; phase three replaces this packing policy."""
    while True:
        ends = list(range(width, len(encoded), width)) + [len(encoded)]
        layout = {
            "version": 2,
            "renderer": 1,
            "ends": ends,
            "record": digest(encoded),
            "view": digest(view),
            "blocks": blocks,
        }
        # Reserve 19 decimal places per link; actual completed bodies are checked again before posting.
        numbers = [9223372036854775807] * (len(ends) - 1)
        bodies = [
            body(key, i, layout, part, view, numbers, repository, issue)
            for i, part in enumerate(slices(encoded, layout), 1)
        ]
        if max(len(b.encode()) for b in bodies) <= protocol.ARTIFACT_BODY_BYTES:
            return layout
        if width == 1:
            raise ValueError("Shared report layout exceeds artifact capacity")
        width = max(1, width // 2)
