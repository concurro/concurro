"""Language-independent path envelopes and generated-file policy."""

from fnmatch import fnmatchcase
from functools import lru_cache

SCOPE_KEYS = {"paths", "max_files", "max_changed_lines"}
MAX_SCOPE_PATHS, MAX_SCOPE_FILES, MAX_SCOPE_LINES = 8, 32, 3000
MAX_PLAN_FILES = 8
MAX_OVERLAY_FILES = MAX_SCOPE_FILES + MAX_PLAN_FILES


def glob_matches(path, pattern):
    """Match path segments with ** covering zero or more directories."""
    parts, patterns = path.split("/"), pattern.split("/")

    @lru_cache(maxsize=None)
    def match(index, pattern_index):
        """Memoize wildcard branches within this bounded path comparison."""
        if pattern_index == len(patterns):
            return index == len(parts)
        if patterns[pattern_index] == "**":
            return (match(index, pattern_index + 1)
                    or index < len(parts) and match(index + 1, pattern_index))
        return (index < len(parts) and fnmatchcase(parts[index], patterns[pattern_index])
                and match(index + 1, pattern_index + 1))

    return match(0, 0)

def _strings(value, minimum, maximum, length=200):
    """Return whether a value is a bounded list of distinct non-empty strings."""
    return (isinstance(value, list) and minimum <= len(value) <= maximum and len(set(value)) == len(value)
            and all(isinstance(v, str) and v.strip() and len(v) <= length for v in value))

def _relative(value):
    """Accept relative repository paths and globs without traversal or absolute roots."""
    return (isinstance(value, str) and bool(value) and not value.startswith("/") and "\\" not in value
            and all(p not in ("", ".", "..") for p in value.split("/")))

def scope_of(plan):
    """A plan's envelope and budget; a plan without one may change exactly its files, as before envelopes."""
    return plan.get("scope") or {"paths": list(plan["files"]), "max_files": len(plan["files"]), "max_changed_lines": None}

def in_scope(path, plan):
    """Whether a path lies inside the plan's envelope."""
    return any(glob_matches(path, pattern) for pattern in scope_of(plan)["paths"])

def scope_errors(plan):
    """Why a plan's `scope` is invalid, or None."""
    scope = plan.get("scope")
    if scope is None:
        return None
    if (not isinstance(scope, dict) or set(scope) != SCOPE_KEYS
            or not _strings(scope["paths"], 1, MAX_SCOPE_PATHS) or not all(_relative(v) for v in scope["paths"])
            or type(scope["max_files"]) is not int or not len(plan["files"]) <= scope["max_files"] <= MAX_SCOPE_FILES
            or type(scope["max_changed_lines"]) is not int or not 1 <= scope["max_changed_lines"] <= MAX_SCOPE_LINES):
        return "plan scope needs 1–8 path globs, a file budget covering its files (at most 32) and 1–3000 changed lines"
    if not all(in_scope(path, plan) for path in plan["files"]):
        return "plan files must lie inside the plan scope"
    return None

def generated(path, policy):
    """Whether target configuration marks a path as generated (aggregate bounds still apply)."""
    return any(glob_matches(path, pattern) for pattern in policy.get("generated", []))
