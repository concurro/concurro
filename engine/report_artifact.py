"""Versioned immutable parts with frozen layout, hidden data and optional attempt identity."""

import json
import re

import artifact_capacity
import protocol
from policy import digest
from report_artifact_view_v1 import submitted as submitted

MARKER = "<!-- concurro:report-data:v1:"
DATA = "\n<!-- concurro:report-part:v2\n"
ATTEMPT_DATA = "\n<!-- concurro:report-part:v3\n"
# Renderer 2 is a frozen neutral fallback; keep these bytes stable for continuation recovery.
COMPACT_VIEW = "### Submitted model output\n\nCanonical data is available in the raw API body and any linked parts."


def body(key, index, layout, part, view, numbers, repository, issue):
    """Render one frozen part; only the entry carries the view and continuation identities."""
    count = len(layout["ends"])
    record = {"layout": layout, "index": index, "part": part, "continuations": numbers if index == 1 else []}
    encoded = json.dumps(record, sort_keys=True).replace(">", "\\u003e")
    prefix = f"{MARKER}{key}:{index}:{count}:{digest(layout)} -->"
    if layout["version"] == 3:
        prefix = prefix.removesuffix(" -->") + f":{layout['nonce']}:{layout['attempt'] or '-'} -->"
    return (
        prefix
        + (ATTEMPT_DATA if layout["version"] == 3 else DATA)
        + encoded
        + "\n-->\n"
        + readable(index, count, view, numbers, repository, issue)
    )


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
    modern = ATTEMPT_DATA in text
    marker, separator, tail = text.partition(ATTEMPT_DATA if modern else DATA)
    suffix = r":([0-9a-f]{32}):(" + protocol.DIGEST_PATTERN + r"|-)" if modern else ""
    match = re.fullmatch(
        re.escape(MARKER + key + ":") + r"(\d+):(\d+):(" + protocol.DIGEST_PATTERN + ")" + suffix + " -->", marker
    )
    encoded, closing, view = tail.partition("\n-->\n")
    if not separator or not match or not closing or ">" in encoded:
        raise ValueError("Malformed shared report part")
    record = json.loads(encoded)
    layout = record["layout"]
    if (
        set(record) != {"layout", "index", "part", "continuations"}
        or set(layout)
        != {"version", "renderer", "ends", "record", "view", "blocks"} | ({"nonce", "attempt"} if modern else set())
        or layout["version"] != (3 if modern else 2)
        or modern
        and (layout["nonce"] != match[4] or layout["attempt"] != (None if match[5] == "-" else match[5]))
        or layout["renderer"] not in (1, 2)
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


def view_for(value, layout):
    """Reproduce the frozen view when only continuation parts survived."""
    return COMPACT_VIEW if layout and layout["renderer"] == 2 else submitted(value)


def readable(index, count, view, numbers, repository, issue):
    """Return only visible Markdown, including continuation links and the final newline."""
    if index == 1:
        return view + continuation_links(numbers, repository, issue) + "\n"
    return f"### Stored task data · part {index} of {count}\n"


def packing_bodies(key, encoded, layout, view, repository, issue):
    """Account for complete parts with 19-digit continuation IDs before uploading anything."""
    numbers = [9223372036854775807] * (len(layout["ends"]) - 1)
    return [
        (
            body(key, i, layout, part, view, numbers, repository, issue),
            readable(i, len(numbers) + 1, view, numbers, repository, issue),
        )
        for i, part in enumerate(slices(encoded, layout), 1)
    ]


def pack(key, encoded, view, blocks, repository, issue, *, nonce=None, attempt=None):
    """Fit complete encoded responses, shortening only presentation before splitting canonical data."""
    layout = {
        "version": 2,
        "renderer": 1,
        "ends": [len(encoded)],
        "record": digest(encoded),
        "view": digest(view),
        "blocks": blocks,
    }
    if nonce is not None:
        layout.update(version=3, nonce=nonce, attempt=attempt)
    if all(artifact_capacity.fits(b, v) for b, v in packing_bodies(key, encoded, layout, view, repository, issue)):
        return layout
    view = COMPACT_VIEW
    layout.update(renderer=2, view=digest(view))
    count = 1
    while True:
        # Use maximum-width offsets/IDs while choosing boundaries. The real descriptor
        # is no larger; repeat if its part count needs more room than this reservation.
        layout["ends"] = [len(encoded)] * count
        numbers = [9223372036854775807] * (count - 1)
        ends, start = [], 0
        while start < len(encoded):
            index = len(ends) + 1
            visible = readable(index, count, view, numbers, repository, issue)

            def fits_end(end):
                """Test the exact encoded slice with the reserved descriptor and links."""
                text = body(key, index, layout, encoded[start:end], view, numbers, repository, issue)
                return artifact_capacity.fits(text, visible)

            low, high = start, len(encoded)
            while low < high:
                middle = (low + high + 1) // 2
                if fits_end(middle):
                    low = middle
                else:
                    high = middle - 1
            if low == start:
                raise artifact_capacity.CapacityError("Artifact response budget cannot hold its layout")
            ends.append(low)
            start = low
        if len(ends) > count:
            count = len(ends)
            continue
        layout["ends"] = ends
        return layout
