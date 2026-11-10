"""Compact, verifiable inventories derived only from immutable Git objects."""

import json
import re
from copy import deepcopy

import protocol
from policy import digest

# A storage threshold, not a file-count admission limit.
INLINE_BYTES = 4096
FORMAT = 5
SLOTS = (("changed_files",), ("candidate", "changed_files"), ("approved_accumulated_files",))


def valid_commitment(ref):
    """Require fixed-format object identities before issuing any Git request."""
    return all(
        isinstance(ref.get(key), str) and re.fullmatch(pattern, ref[key])
        for key, pattern in (
            ("base", protocol.SHA_PATTERN),
            ("head", protocol.SHA_PATTERN),
            ("digest", protocol.DIGEST_PATTERN),
        )
    )


def binding(state, path):
    """Bind approval inventories to the design parent, never the current PR head."""
    if path == ("candidate", "changed_files"):
        candidate = state.get("candidate", {})
        return candidate.get("base"), candidate.get("head")
    if path == ("approved_accumulated_files",):
        return state.get("base"), state.get("approved_inventory_parent", state.get("design_parent"))
    return state.get("base"), state.get("head")


def pack(state):
    """Replace large reproducible lists with commitments without changing the in-memory approval."""
    result, refs = deepcopy(state), []
    for path in SLOTS:
        parent = result.get(path[0], {}) if len(path) == 2 else result
        value = parent.get(path[-1]) if isinstance(parent, dict) else None
        base, head = binding(state, path)
        if not isinstance(value, list) or not base or not head or len(json.dumps(value).encode()) <= INLINE_BYTES:
            continue
        refs.append({"path": list(path), "base": base, "head": head, "digest": digest(value)})
        parent[path[-1]] = None
    return result, refs


def restore(state, refs, reader):
    """Verify exact slots and commitments before restoring approval-bound lists."""
    if not isinstance(refs, list) or len(refs) > len(SLOTS):
        raise ValueError("invalid inventories")
    seen = set()
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"path", "base", "head", "digest"} or not valid_commitment(ref):
            raise ValueError("invalid inventory reference")
        path = tuple(ref["path"])
        if path not in SLOTS or path in seen or binding(state, path) != (ref["base"], ref["head"]):
            raise ValueError("invalid inventory binding")
        seen.add(path)
        parent = state[path[0]] if len(path) == 2 else state
        if parent[path[-1]] is not None:
            raise ValueError("inventory slot is not empty")
        value = reader.changed_paths(state, ref["base"], ref["head"])
        if digest(value) != ref["digest"]:
            raise ValueError("inventory commitment mismatch")
        parent[path[-1]] = value
    if state.get("candidate", {}).get("file_inventory"):
        candidate_paths(state, reader)


def compact_candidate(candidate):
    """Archive a large candidate's Git commitment rather than another copy of its path list."""
    paths = candidate["changed_files"]
    if len(json.dumps(paths).encode()) > INLINE_BYTES:
        candidate["file_inventory"] = {"base": candidate["base"], "head": candidate["head"], "digest": digest(paths)}
        del candidate["changed_files"]


def candidate_paths(state, reader):
    """Resolve a candidate's exact validated inventory, accepting legacy inline candidates."""
    candidate = state["candidate"]
    ref = candidate.get("file_inventory")
    if ref is None:
        return candidate.get("changed_files", state.get("changed_files", []))
    if (
        not isinstance(ref, dict)
        or set(ref) != {"base", "head", "digest"}
        or not valid_commitment(ref)
        or (ref["base"], ref["head"]) != (candidate["base"], candidate["head"])
    ):
        raise ValueError("invalid candidate inventory binding")
    paths = reader.changed_paths(state, ref["base"], ref["head"])
    if digest(paths) != ref["digest"]:
        raise ValueError("candidate inventory commitment mismatch")
    return paths


def retain_approval_parent(state):
    """Keep the old approval's derivation pin when reopening advances the planning parent."""
    if (
        state.get("design_parent")
        and len(json.dumps(state.get("approved_accumulated_files", [])).encode()) > INLINE_BYTES
    ):
        state.setdefault("approved_inventory_parent", state["design_parent"])
