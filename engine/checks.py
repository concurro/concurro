"""Execution profiles: validated check registries and the capability contract shared with models."""

from fnmatch import fnmatchcase
from functools import lru_cache
import json
import re
import tomllib

CONTROL_CHECKS = ("diff", "scope", "text")
CONTROL_DESCRIPTIONS = {
    "diff": "Whitespace validation of the complete checkpoint diff against the synthetic local baseline; requires a clean worktree.",
    "scope": "Require exactly the declared changed paths and addition/modification status against approved original contents, including across repair attempts; reject empty diffs and uncommitted files.",
    "text": "Check each declared file's optional line limit and required literal text in the checkpoint.",
}
REPAIR_DETAILS = {
    'validation_shape': 'Return a validation object containing checks and files.',
    'validation_checks': 'Select distinct available check IDs and include every mandatory check of the profiles the planned files or the scope envelope trigger.',
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
# Package scoping: a check's `{scope}` argument becomes its `each` arguments per changed package, or
# `all` when any changed file is outside a package or too many packages changed.
SCOPE, PACKAGE_PLACEHOLDER = "{scope}", "{package}"
MANIFEST = re.compile(r"[A-Za-z0-9._-]{1,64}")
PACKAGE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")
MAX_PACKAGES = 8
# A plan's scope envelope: the paths its change may touch and its size budget, under engine caps.
SCOPE_KEYS = {"paths", "max_files", "max_changed_lines"}
MAX_SCOPE_PATHS, MAX_SCOPE_FILES, MAX_SCOPE_LINES = 8, 32, 3000
MAX_PLAN_FILES = 8
# Inspection overlays the primary files and every changed file.
MAX_OVERLAY_FILES = MAX_SCOPE_FILES + MAX_PLAN_FILES


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


def _scope(item, manifest, caches):
    """A scoped check: exactly one `{scope}` argument, `each` naming `{package}` once, a manifest to find packages."""
    scope = item["scope"]
    return (manifest is not None and isinstance(scope, dict) and set(scope) - {"library"} == {"each", "all"}
            and type(scope.get("library", False)) is bool
            and item["command"].count(SCOPE) == 1 and item["command"][0] != SCOPE
            and isinstance(scope["each"], list) and [v for v in scope["each"] if PACKAGE_PLACEHOLDER in v] == [PACKAGE_PLACEHOLDER]
            and all(_argv([v], caches) for v in scope["each"] if v != PACKAGE_PLACEHOLDER)
            and isinstance(scope["all"], list) and len(scope["all"]) <= 8
            and all(_argv([v], caches) for v in scope["all"]))


def load_profile(text, expected_id):
    """Parse and strictly validate one version-1 profile document."""
    try:
        profile = json.loads(text)
    except ValueError:
        raise ProfileError("profile is not valid JSON") from None
    if (not isinstance(profile, dict) or set(profile) - {"package_manifest"} != PROFILE_KEYS
            or profile["version"] != 1):
        raise ProfileError("profile fields are invalid")
    manifest = profile.get("package_manifest")
    if manifest is not None and not (isinstance(manifest, str) and MANIFEST.fullmatch(manifest)
                                     and manifest not in (".", "..")):
        raise ProfileError("profile package manifest is invalid")
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
        if (not isinstance(item, dict) or set(item) - {"scope"} != CHECK_KEYS or not isinstance(item["id"], str)
                or not NAME.fullmatch(item["id"]) or item["id"] in CONTROL_CHECKS or item["id"] in seen
                or not isinstance(item["purpose"], str) or not 1 <= len(item["purpose"].strip()) <= 300
                or not _argv([v for v in item["command"] if v != SCOPE] if "scope" in item else item["command"], caches)
                or type(item["prepared"]) is not bool or type(item["mandatory"]) is not bool
                or item["prepared"] and prepare is None
                or "scope" in item and not _scope(item, manifest, caches)):
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


def approved_profiles(plan, tree, execution):
    """Profiles the design may trigger: those a primary file, or a base file inside the envelope, triggers."""
    inside = [path for path in tree if in_scope(path, plan)]
    return [p["id"] for p in execution["profiles"] if triggered(p, list(plan["files"]) + inside)]


def mandatory(profiles, execution):
    """Control checks always; mandatory checks of the named profiles."""
    return list(CONTROL_CHECKS) + [c["id"] for p in execution["profiles"] if p["id"] in profiles
                                   for c in p["checks"] if c["mandatory"]]


def unapproved_triggers(profiles, execution):
    """Trigger globs of the profiles a candidate must not newly trigger."""
    return [pattern for p in execution["profiles"] if p["id"] not in profiles for pattern in p["triggers"]]


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


def manifest_data(text):
    """A manifest's TOML as a table, or None when it is missing, unreadable or malformed."""
    if not isinstance(text, str):
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def library(manifest, data, tree):
    """Whether a Cargo package has a library target: a `[lib]` table, or `src/lib.rs` unless `autolib` is off."""
    directory = manifest.rsplit("/", 1)[0] + "/" if "/" in manifest else ""
    return isinstance(data.get("lib"), dict) or (
        data["package"].get("autolib", True) is not False and directory + "src/lib.rs" in tree)


def packages(profile, paths, tree, read):
    """The packages owning a profile's changed paths, or None when the check must cover everything.

    `tree` is the set of paths at the approved base and `read(path)` its text, or None when it
    cannot be read. A path belongs to the nearest enclosing directory with the profile's manifest,
    named by its `[package] name`. When a scoped check runs library targets only, a changed
    package without one also falls back to everything, since scoping to it would select nothing.
    """
    manifest = profile.get("package_manifest")
    if manifest is None:
        return None
    needs_library = any(c.get("scope", {}).get("library") for c in profile["checks"])
    found = set()
    for path in paths:
        if not triggered(profile, [path]):
            continue
        parts = path.split("/")[:-1]
        owner = next(("/".join(parts[:i] + [manifest]) for i in range(len(parts), -1, -1)
                      if "/".join(parts[:i] + [manifest]) in tree), None)
        data = manifest_data(read(owner)) if owner else None
        package = data.get("package") if data else None
        name = package.get("name") if isinstance(package, dict) else None
        if not isinstance(name, str) or not PACKAGE.fullmatch(name):
            return None
        if needs_library and not library(owner, data, tree):
            return None
        found.add(name)
    return sorted(found) if 0 < len(found) <= MAX_PACKAGES else None


def command(check, packages):
    """A check's argv with its `{scope}` argument expanded for `packages` (None: the `all` form)."""
    if "scope" not in check:
        return list(check["command"])
    scope = check["scope"]
    expanded = scope["all"] if packages is None else [
        v.replace(PACKAGE_PLACEHOLDER, name) for name in packages for v in scope["each"]]
    index = check["command"].index(SCOPE)
    return check["command"][:index] + expanded + check["command"][index + 1:]


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


def contract_errors(plan, execution, profiles=None):
    """Return fixed repair codes for check selections and declarative file rules.

    `profiles` are the plan's approved profiles; without them, those its files trigger.
    """
    errors = file_rule_errors(plan)
    if errors:
        return errors
    names = plan['validation']['checks']
    needed = mandatory(profiles, execution) if profiles is not None else required(plan['files'], execution)
    if (len(names) != len(set(names)) or not set(names).issubset(available(execution))
            or not set(needed).issubset(names)):
        return ['validation_checks']
    return []
