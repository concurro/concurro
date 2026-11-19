"""Reject incompatible edits to an existing wire schema version."""

import argparse
import json
import os
import re
import subprocess
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANNOTATIONS = {"description", "examples", "title"}


def nullable(definition):
    """Check whether the generated property accepts JSON null."""
    kind = definition.get("type")
    return kind == "null" or (isinstance(kind, list) and "null" in kind) or any(
        item.get("type") == "null"
        for branch in ("anyOf", "oneOf")
        for item in definition.get(branch, [])
    )


def compatible_open_union(old, new, path, mode):
    """Permit an added known action while retaining the open fallback."""
    if new.get("x-concurro-open") is not True:
        return path
    before, after = old.get("anyOf", []), new.get("anyOf", [])
    if len(before) != 2 or len(after) != 2:
        return f"{path}.anyOf"
    previous, current = before[0].get("oneOf", []), after[0].get("oneOf", [])
    if len(current) < len(previous):
        return f"{path}.anyOf[0].oneOf"
    for index, branch in enumerate(previous):
        failure = compatible(branch, current[index], f"{path}.anyOf[0].oneOf[{index}]", mode)
        if failure:
            return failure
    old_head, new_head = deepcopy(before[0]), deepcopy(after[0])
    old_head.pop("oneOf", None)
    new_head.pop("oneOf", None)
    failure = compatible(old_head, new_head, f"{path}.anyOf[0]", mode)
    if failure:
        return failure
    prior_pattern = before[1].get("properties", {}).get("action", {}).get("pattern")
    next_pattern = after[1].get("properties", {}).get("action", {}).get("pattern")
    if not isinstance(prior_pattern, str) or not isinstance(next_pattern, str):
        return f"{path}.anyOf[1].properties.action.pattern"
    if len(current) > len(previous):
        additions = [item.get("properties", {}).get("action", {}).get("const") for item in current[len(previous):]]
        if any(not isinstance(action, str) or not re.fullmatch(prior_pattern, action) for action in additions):
            return f"{path}.anyOf[0].oneOf"
        for index, branch in enumerate(current[len(previous):], start=len(previous)):
            fallback_without_action = deepcopy(before[1])
            branch_without_action = deepcopy(branch)
            for item in (fallback_without_action, branch_without_action):
                item["properties"].pop("action", None)
                item["required"] = [field for field in item["required"] if field != "action"]
            failure = compatible(
                fallback_without_action,
                branch_without_action,
                f"{path}.anyOf[0].oneOf[{index}]",
                "de",
            )
            if failure:
                return failure
        prior_known = [item.get("properties", {}).get("action", {}).get("const") for item in previous]
        prior_prefix = "^(?!(?:" + "|".join(re.escape(action) for action in prior_known) + ")$)"
        known = [item.get("properties", {}).get("action", {}).get("const") for item in current]
        prefix = "^(?!(?:" + "|".join(re.escape(action) for action in known) + ")$)"
        if not prior_pattern.startswith(prior_prefix) or next_pattern != prefix + prior_pattern[len(prior_prefix):]:
            return f"{path}.anyOf[1].properties.action.pattern"
    elif prior_pattern != next_pattern:
        return f"{path}.anyOf[1].properties.action.pattern"
    fallback = deepcopy(after[1])
    fallback["properties"]["action"]["pattern"] = prior_pattern
    return compatible(before[1], fallback, f"{path}.anyOf[1]", mode)


def compatible(old, new, path="$", mode="de"):
    """Return the first incompatible path, or None for an allowed additive edit."""
    if old == new:
        return None
    if not isinstance(old, dict) or not isinstance(new, dict):
        return path
    if old.get("x-concurro-open") is True and "anyOf" in old:
        failure = compatible_open_union(old, new, path, mode)
        if failure:
            return failure
        old = {key: value for key, value in old.items() if key != "anyOf"}
        new = {key: value for key, value in new.items() if key != "anyOf"}
    for key, previous in old.items():
        if key in ANNOTATIONS:
            continue
        if key not in new:
            return f"{path}.{key}"
        current = new[key]
        if key == "enum" and old.get("x-concurro-open") is True:
            if not isinstance(current, list) or not set(map(json.dumps, previous)) <= set(map(json.dumps, current)):
                return f"{path}.enum"
        elif key == "properties":
            if not isinstance(current, dict):
                return f"{path}.properties"
            for name, definition in previous.items():
                if name not in current:
                    return f"{path}.properties.{name}"
                failure = compatible(definition, current[name], f"{path}.properties.{name}", mode)
                if failure:
                    return failure
            for name in set(current) - set(previous):
                if name in new.get("required", []) and (mode != "ser" or not nullable(current[name])):
                    return f"{path}.required"
        elif key == "required":
            if not isinstance(current, list) or not set(previous) <= set(current):
                return f"{path}.required"
            added = set(current) - set(previous)
            if added and (mode != "ser" or any(
                name in old.get("properties", {}) or not nullable(new.get("properties", {}).get(name, {}))
                for name in added
            )):
                return f"{path}.required"
        elif key == "$defs":
            if not isinstance(current, dict):
                return f"{path}.$defs"
            for name, definition in previous.items():
                if name not in current:
                    return f"{path}.$defs.{name}"
                failure = compatible(definition, current[name], f"{path}.$defs.{name}", mode)
                if failure:
                    return failure
        elif isinstance(previous, list) and isinstance(current, list) and key != "enum":
            if len(previous) != len(current):
                return f"{path}.{key}"
            for index, (before, after) in enumerate(zip(previous, current)):
                failure = compatible(before, after, f"{path}.{key}[{index}]", mode)
                if failure:
                    return failure
        else:
            failure = compatible(previous, current, f"{path}.{key}", mode)
            if failure:
                return failure
    for key in set(new) - set(old):
        if key not in ANNOTATIONS and key not in {"properties", "$defs"}:
            return f"{path}.{key}"
    return None


def main():
    """Compare schemas committed at the merge base with the current files."""
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    base = parser.parse_args().base
    if base == "0" * 40:
        print("No previous commit; schema compatibility has no baseline")
        return
    merge_base = (
        base
        if os.environ.get("GITHUB_EVENT_NAME") == "push"
        else subprocess.run(
            ["git", "merge-base", base, "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", merge_base, "--", "contracts/schemas"],
        cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    failed = False
    for relative in listed:
        if not relative.endswith(".json"):
            continue
        path = ROOT / relative
        if not path.is_file():
            print(f"{relative}: committed schema was deleted; use a new version")
            failed = True
            continue
        previous = subprocess.run(
            ["git", "show", f"{merge_base}:{relative}"],
            cwd=ROOT, capture_output=True, text=True, check=True
        )
        old = json.loads(previous.stdout)
        new = json.loads(path.read_text())
        mode = "ser" if relative.endswith(".ser.json") else "de"
        failure = compatible(old, new, mode=mode)
        if failure:
            print(f"{relative}: incompatible change at {failure}; use a new version")
            failed = True
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
