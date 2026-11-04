"""Lockfile parsing: the dependency delta of a change, and the workspace members it affects."""

import re
import tomllib

# Delta entries kept in durable state and contexts; counts stay exact beyond it.
MAX_DELTA = 40
DEPENDENCY = re.compile(r"(?P<name>[^ ]+)(?: (?P<version>[^ ]+))?(?: \((?P<source>[^)]+)\))?")


def cargo(text):
    """A Cargo.lock's packages, keyed by (name, version, source), or None when it cannot be parsed."""
    if not isinstance(text, str):
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    items, packages = data.get("package", []), {}
    if not isinstance(items, list):
        return None
    for item in items:
        if (not isinstance(item, dict) or not all(isinstance(item.get(k), str) for k in ("name", "version"))
                or not all(isinstance(item.get(k, ""), str) for k in ("source", "checksum"))
                or not isinstance(item.get("dependencies", []), list)
                or not all(isinstance(d, str) for d in item.get("dependencies", []))):
            return None
        key = (item["name"], item["version"], item.get("source"))
        packages[key] = {"checksum": item.get("checksum"), "dependencies": tuple(item.get("dependencies", ()))}
    return packages


def resolve(packages, dependency):
    """The packages a lock `dependencies` entry names: by name, name and version, or all three."""
    match = DEPENDENCY.fullmatch(dependency)
    if not match:
        return []
    return [key for key in packages if key[0] == match["name"]
            and match["version"] in (None, key[1]) and match["source"] in (None, key[2])]


def delta(before, after):
    """Packages added, removed and changed (in version, source or checksum alone) between two parsed locks."""
    old, new = set(before), set(after)
    removed, added = sorted(old - new, key=str), sorted(new - old, key=str)
    changed = [{"name": k[0], "version": k[1], "source": k[2], "change": "checksum"}
               for k in sorted(old & new, key=str) if before[k]["checksum"] != after[k]["checksum"]]
    # A version or source move of one name reads as a change, not a removal and an addition.
    for name in sorted({k[0] for k in removed} & {k[0] for k in added}):
        gone = [k for k in removed if k[0] == name]
        came = [k for k in added if k[0] == name]
        if len(gone) == len(came) == 1:
            changed.append({"name": name, "version": came[0][1], "source": came[0][2],
                            "change": f"{gone[0][1]} → {came[0][1]}" if gone[0][1] != came[0][1] else "source"})
            removed.remove(gone[0])
            added.remove(came[0])
    entry = lambda k: {"name": k[0], "version": k[1], "source": k[2]}
    return {"added": [entry(k) for k in added], "removed": [entry(k) for k in removed],
            "changed": sorted(changed, key=lambda c: c["name"])}


def bounded(change):
    """A delta for durable state and model contexts: at most MAX_DELTA entries in all, with exact counts."""
    kept, budget = {}, MAX_DELTA
    for kind in ("changed", "added", "removed"):
        kept[kind] = change[kind][:budget]
        budget -= len(kept[kind])
    kept["counts"] = {kind: len(change[kind]) for kind in ("changed", "added", "removed")}
    return kept


def affected_members(before, after):
    """The nearest workspace members whose resolved dependencies changed, or None when none is reached.

    Starts from every package added, changed, or whose own dependency list changed, and walks the
    candidate's reverse dependencies through external crates, stopping at the first members (path
    packages, which have no source) on each path.
    """
    starts = {k for k in after if k not in before or before[k] != after[k]}
    dependents = {}
    for key, item in after.items():
        for dependency in item["dependencies"]:
            for target in resolve(after, dependency):
                dependents.setdefault(target, set()).add(key)
    members, seen, frontier = set(), set(), list(starts)
    while frontier:
        key = frontier.pop()
        if key in seen:
            continue
        seen.add(key)
        if key[2] is None:
            members.add(key[0])
            continue
        frontier.extend(dependents.get(key, ()))
    return sorted(members) or None
