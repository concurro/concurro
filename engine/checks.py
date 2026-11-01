"""Execution profiles: validated check registries and the capability contract shared with models."""

from fnmatch import fnmatchcase
from functools import lru_cache
import json
import re

CONTROL_CHECKS = ("diff", "scope", "text")
CONTROL_DESCRIPTIONS = {
    "diff": "Whitespace validation of the complete checkpoint diff against the synthetic local baseline; requires a clean worktree.",
    "scope": "Require exactly the declared changed paths and addition/modification status against approved original contents, including across repair attempts; reject empty diffs and uncommitted files.",
    "text": "Check each declared file's optional line limit and required literal text in the checkpoint.",
}
REPAIR_DETAILS = {
    'validation_shape': 'Return a validation object containing checks and files.',
    'validation_checks': 'Select distinct available check IDs and include every mandatory check for the planned paths.',
    'validation_files': 'Declare exactly the planned paths with add/modify status, an optional positive line limit and bounded literal text assertions.',
    'validation_change': 'The declared add/modify status disagrees with file existence at the approved source base.',
}
PROFILE_KEYS = {"version", "id", "triggers", "protected", "caches", "env", "prepare", "checks"}
CHECK_KEYS = {"id", "purpose", "command", "prepared", "mandatory"}
NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
RESERVED_ENV = {"PATH", "HOME"}
RESERVED_ENV_PREFIXES = ("LD_", "GIT_", "CONCURRO_")
PLACEHOLDER = re.compile(r"\{cache:([a-z][a-z0-9-]{0,31})\}")
CACHE_ROOT = "/cache"
MAX_PROFILES = 4


class ProfileError(ValueError):
    """A profile document or composition is invalid; configuration fails closed."""


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


def _templated(value, caches):
    """Allow braces only as placeholders for this profile's declared caches."""
    stripped = PLACEHOLDER.sub("", value)
    return "{" not in stripped and "}" not in stripped and all(
        name in caches for name in PLACEHOLDER.findall(value))


def _argv(value, caches):
    """Require a fixed argv: bounded strings, a program, and placeholders only for own caches."""
    return (isinstance(value, list) and 1 <= len(value) <= 64 and isinstance(value[0], str) and bool(value[0])
            and all(isinstance(v, str) and len(v) <= 1000 and "\x00" not in v and _templated(v, caches)
                    for v in value))


def load_profile(text, expected_id):
    """Parse and strictly validate one version-1 profile document."""
    try:
        profile = json.loads(text)
    except ValueError:
        raise ProfileError("profile is not valid JSON") from None
    if not isinstance(profile, dict) or set(profile) != PROFILE_KEYS or profile["version"] != 1:
        raise ProfileError("profile fields are invalid")
    if profile["id"] != expected_id or not isinstance(profile["id"], str) or not NAME.fullmatch(profile["id"]):
        raise ProfileError("profile id does not match its selection")
    if not _strings(profile["triggers"], 1, 32) or not all(_relative(v) for v in profile["triggers"]):
        raise ProfileError("profile triggers are invalid")
    if not _strings(profile["protected"], 0, 64) or not all(_relative(v) for v in profile["protected"]):
        raise ProfileError("profile protected paths are invalid")
    caches = profile["caches"]
    if not _strings(caches, 0, 8, 32) or not all(NAME.fullmatch(v) for v in caches):
        raise ProfileError("profile caches are invalid")
    env = profile["env"]
    if (not isinstance(env, dict) or len(env) > 32
            or not all(isinstance(k, str) and ENV_NAME.fullmatch(k) and k not in RESERVED_ENV
                       and not k.startswith(RESERVED_ENV_PREFIXES) for k in env)
            or not all(isinstance(v, str) and len(v) <= 500 and "\x00" not in v and _templated(v, caches)
                       for v in env.values())):
        raise ProfileError("profile environment is invalid")
    prepare = profile["prepare"]
    if prepare is not None:
        if (not isinstance(prepare, dict) or set(prepare) != {"requires", "source", "steps"}
                or type(prepare["source"]) is not bool
                or not _strings(prepare["requires"], 1, 16)
                or not all(_relative(v) and "*" not in v for v in prepare["requires"])
                or not all(any(glob_matches(v, p) for p in profile["protected"]) for v in prepare["requires"])
                or not isinstance(prepare["steps"], list) or not 1 <= len(prepare["steps"]) <= 8
                or not all(_argv(step, caches) for step in prepare["steps"])):
            raise ProfileError("profile preparation is invalid; inputs must be protected")
    items = profile["checks"]
    if not isinstance(items, list) or not 1 <= len(items) <= 16:
        raise ProfileError("profile checks are invalid")
    seen = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) != CHECK_KEYS or not isinstance(item["id"], str)
                or not NAME.fullmatch(item["id"]) or item["id"] in CONTROL_CHECKS or item["id"] in seen
                or not isinstance(item["purpose"], str) or not 1 <= len(item["purpose"].strip()) <= 300
                or not _argv(item["command"], caches)
                or type(item["prepared"]) is not bool or type(item["mandatory"]) is not bool
                or item["prepared"] and prepare is None):
            raise ProfileError("profile check is invalid")
        seen.add(item["id"])
    return profile


def compose(profiles):
    """Combine ordered profiles, rejecting collisions instead of renaming or merging them."""
    if not 1 <= len(profiles) <= MAX_PROFILES or len({p["id"] for p in profiles}) != len(profiles):
        raise ProfileError("select one to four distinct execution profiles")
    identifiers = [c["id"] for p in profiles for c in p["checks"]]
    if len(identifiers) != len(set(identifiers)):
        raise ProfileError("check IDs must be unique across selected profiles")
    return {"profiles": profiles}


def summary(execution):
    """Return the compact, argv-free view that durable state and policy decisions use."""
    return {"profiles": [{"id": p["id"], "triggers": p["triggers"], "protected": p["protected"],
                          "checks": [{k: c[k] for k in ("id", "prepared", "mandatory")} for c in p["checks"]]}
                         for p in execution["profiles"]]}


def triggered(profile, paths):
    """Return whether any planned path selects a profile's mandatory checks."""
    return any(glob_matches(path, pattern) for path in paths for pattern in profile["triggers"])


def required(paths, execution):
    """Control checks always; mandatory checks of every profile a planned path triggers."""
    return list(CONTROL_CHECKS) + [c["id"] for p in execution["profiles"] if triggered(p, paths)
                                   for c in p["checks"] if c["mandatory"]]


def available(execution):
    """Every control and profile check is selectable; preparation follows the approved selection."""
    return list(CONTROL_CHECKS) + [c["id"] for p in execution["profiles"] for c in p["checks"]]


def executable(execution, prepared):
    """Checks a workbench can run once the named profiles have completed preparation."""
    return list(CONTROL_CHECKS) + [c["id"] for p in execution["profiles"] for c in p["checks"]
                                   if not c["prepared"] or p["id"] in prepared]


def preparing(selected, execution):
    """Profiles whose preparation the approved selection requires, in composition order."""
    return [p["id"] for p in execution["profiles"]
            if any(c["prepared"] and c["id"] in selected for c in p["checks"])]


def build_checked(path, execution):
    """Whether a path triggers any profile and therefore needs execution checks."""
    return any(triggered(p, [path]) for p in execution["profiles"])


def protected(path, execution):
    """Profile-protected paths are never editable, whatever the target's own policy says."""
    return any(glob_matches(path, pattern) for p in execution["profiles"] for pattern in p["protected"])


def catalog(paths, execution):
    """Describe capabilities from the same identifiers used by execution dispatch."""
    offered = [{"id": name, "profile": "engine", "purpose": CONTROL_DESCRIPTIONS[name], "prepared": False}
               for name in CONTROL_CHECKS]
    offered += [{"id": c["id"], "profile": p["id"], "purpose": c["purpose"], "prepared": c["prepared"]}
                for p in execution["profiles"] for c in p["checks"]]
    return {"version": 2, "checks": offered, "mandatory": required(paths, execution),
            "baseline": "Archive content is imported into a new local Git repository. The remote commit SHA is provenance, not a usable local Git revision.",
            "execution": "Request named checks and file rules, never shell commands. Checks that need preparation run only when the approved plan selects them; preparation happens before any candidate edit. The controller checkpoints all edits before checks; inspection exposes read-only file tools and no Git history.",
            "limits": "Named checks do not prove prose accuracy or requirement coverage. Reviewers must assess those against source evidence."}


def file_rule_errors(plan):
    """Return fixed repair codes for the validation object's shape and declarative file rules."""
    spec = plan.get('validation')
    if not isinstance(spec, dict) or set(spec) != {'checks', 'files'}:
        return ['validation_shape']
    if not isinstance(spec['checks'], list) or not all(isinstance(n, str) for n in spec['checks']):
        return ['validation_checks']
    rules = spec['files']
    if not isinstance(rules, list) or len(rules) != len(plan['files']):
        return ['validation_files']
    paths = []
    for rule in rules:
        if (not isinstance(rule, dict) or set(rule) != {'path', 'change', 'max_lines', 'required_text'}
                or not isinstance(rule['path'], str) or rule['change'] not in ('add', 'modify')
                or rule['max_lines'] is not None and (type(rule['max_lines']) is not int or not 1 <= rule['max_lines'] <= 100000)
                or not isinstance(rule['required_text'], list) or len(rule['required_text']) > 16
                or not all(isinstance(t, str) and 0 < len(t) <= 300 for t in rule['required_text'])):
            return ['validation_files']
        paths.append(rule['path'])
    if len(set(paths)) != len(paths) or set(paths) != set(plan['files']):
        return ['validation_files']
    return []


def contract_errors(plan, execution):
    """Return fixed repair codes for check selections and declarative file rules."""
    errors = file_rule_errors(plan)
    if errors:
        return errors
    names = plan['validation']['checks']
    if (len(names) != len(set(names)) or not set(names).issubset(available(execution))
            or not set(required(plan['files'], execution)).issubset(names)):
        return ['validation_checks']
    return []
