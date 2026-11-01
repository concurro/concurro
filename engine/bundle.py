"""Load an operator-owned, bounded instruction bundle without candidate resources."""

import hashlib
import json
from pathlib import Path
import re
import stat
import sys

import checks
from policy import Blocked, digest

STAGES = ("intake", "plan", "plan_qc", "develop", "local_review", "review")
SKILLS = dict(zip(STAGES, ("concurro-assess-scope", "concurro-design", "concurro-review-design",
                          "concurro-implement", "concurro-review-local", "concurro-review-pr")))
MANIFEST = "bundle/manifest.json"
CHARTER = ".concurro/charter.json"
VALIDATION_SKILL = "bundle/skills/concurro-validation/SKILL.md"
VALIDATION_STAGES = ("plan", "plan_qc", "develop", "local_review")
BUILTIN_PROFILES = ("rust",)
ENGINE_RESOURCES = {"bundle/instructions.md", "bundle/roles.json", VALIDATION_SKILL} | {
    f"bundle/schemas/{stage}.json" for stage in STAGES} | {
    f"bundle/skills/{name}/SKILL.md" for name in SKILLS.values()} | {
    f"bundle/profiles/{name}.json" for name in BUILTIN_PROFILES}
TARGET_PROFILE = re.compile(r"\.concurro/profiles/[a-z][a-z0-9-]{0,31}\.json")
SELECTION = re.compile(r"(target:)?([a-z][a-z0-9-]{0,31})")
PROFILE_SELECTION = "execution_profiles"
RESOURCE_LIMIT = 24000


def profile_paths(selection):
    """Map an ordered profile selection to its built-in or target resource paths."""
    if (not isinstance(selection, (list, tuple)) or not 1 <= len(selection) <= checks.MAX_PROFILES
            or len(set(selection)) != len(selection)):
        raise Blocked("execution_profiles must list one to four distinct profiles")
    paths = []
    for entry in selection:
        match = SELECTION.fullmatch(entry) if isinstance(entry, str) else None
        if not match:
            raise Blocked("invalid execution profile selection")
        name = match[2]
        if match[1]:
            paths.append((name, f".concurro/profiles/{name}.json"))
        elif name in BUILTIN_PROFILES:
            paths.append((name, f"bundle/profiles/{name}.json"))
        else:
            raise Blocked("unknown built-in execution profile: " + name)
    return paths


class Bundle:
    """Freeze engine instructions and the target's charter from explicitly allowlisted roots."""

    def __init__(self, root=None, target=None, charter_text=None, profiles=None, profile_texts=None):
        """Reject links, traversal, unexpected resources and oversized packages.

        `root` is the engine checkout. The charter and target-defined profiles come
        from the target repository checkout (default: the working directory) or,
        inside an agent container, as controller-supplied text that must reproduce
        the controller's bundle identity. `profiles` is the ordered selection.
        """
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        self.resources = {}
        manifest_text = read(self.root, MANIFEST)
        manifest = json.loads(manifest_text)
        if (set(manifest) != {"version", "resources"} or manifest["version"] != 1
                or len(manifest["resources"]) != len(ENGINE_RESOURCES)
                or set(manifest["resources"]) != ENGINE_RESOURCES):
            raise Blocked("instruction bundle has unapproved resources")
        self.resources[MANIFEST] = manifest_text
        for path in sorted(ENGINE_RESOURCES):
            self.resources[path] = read(self.root, path)
        self.engine_identity = digest(hashes(self.resources))
        selected = profile_paths(profiles)
        targeted = [path for _, path in selected if path.startswith(".concurro/")]
        if charter_text is None:
            self.target = Path(target) if target is not None else Path.cwd()
            charter_text = read(self.target, CHARTER)
            profile_texts = {path: read(self.target, path) for path in targeted}
        elif not isinstance(charter_text, str) or len(charter_text.encode()) > RESOURCE_LIMIT:
            raise Blocked("bundle resource must be a bounded regular file")
        if (not isinstance(profile_texts, dict) or set(profile_texts) != set(targeted)
                or not all(isinstance(v, str) and len(v.encode()) <= RESOURCE_LIMIT for v in profile_texts.values())):
            raise Blocked("target execution profiles do not match the selection")
        self.resources[CHARTER] = charter_text
        self.resources.update(profile_texts)
        self.resources[PROFILE_SELECTION] = json.dumps(list(profiles))
        try:
            self.execution = checks.compose([checks.load_profile(self.resources[path], name)
                                             for name, path in selected])
        except checks.ProfileError as error:
            raise Blocked("invalid execution profile: " + str(error)) from None
        self.execution_summary = checks.summary(self.execution)
        if sum(len(v.encode()) for v in self.resources.values()) > 100000:
            raise Blocked("instruction bundle too large")
        self.hashes = hashes(self.resources)
        self.identity = digest(self.hashes)
        self.charter = json.loads(self.resources[CHARTER])
        if (self.charter.get("version") != 1 or not self.charter.get("id")
                or not self.charter.get("goals") or not self.charter.get("non_goals")
                or not self.charter.get("constraints") or not self.charter.get("acceptance")):
            raise Blocked("incomplete project charter")
        roles = json.loads(self.resources["bundle/roles.json"])
        if (roles.get("version") != 1 or roles.get("shared") != "bundle/instructions.md"
                or set(roles.get("roles", {})) != set(STAGES)):
            raise Blocked("invalid role manifest")
        self.roles = roles["roles"]
        if not self.resources[VALIDATION_SKILL].startswith('---\nname: concurro-validation\ndescription: '):
            raise Blocked('invalid supporting skill frontmatter')
        for stage, role in self.roles.items():
            expected = {"skill": f"bundle/skills/{SKILLS[stage]}/SKILL.md",
                        "schema": f"bundle/schemas/{stage}.json"}
            if stage in VALIDATION_STAGES:
                expected["supporting_skills"] = [VALIDATION_SKILL]
            if role != expected:
                raise Blocked("role references an unapproved resource")
            skill = self.resources[role["skill"]]
            if not skill.startswith("---\nname: " + SKILLS[stage] + "\ndescription: "):
                raise Blocked("skill frontmatter does not match its role")
            schema = json.loads(self.resources[role["schema"]])
            if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
                raise Blocked("role requires a strict object schema")

    @property
    def charter_text(self):
        """Return the exact charter text that agent containers must hash identically."""
        return self.resources[CHARTER]

    @property
    def target_profile_texts(self):
        """Return target-defined profile texts that agent containers must hash identically."""
        return {path: text for path, text in self.resources.items() if TARGET_PROFILE.fullmatch(path)}

    def schema(self, stage):
        """Return the external response contract for one role."""
        return json.loads(self.resources[self.roles[stage]["schema"]])

    def instructions(self, stage):
        """Load shared governance once and only the selected stage's skill."""
        return {"shared": self.resources["bundle/instructions.md"],
                "charter": self.charter, "role": stage,
                "skill": self.resources[self.roles[stage]["skill"]],
                "supporting_skills": [self.resources[p] for p in self.roles[stage].get("supporting_skills", [])]}


def hashes(resources):
    """Hash each resource by its bundle-relative path."""
    return {p: hashlib.sha256(v.encode()).hexdigest() for p, v in resources.items()}


def read(root, path):
    """Read only regular bounded files under a real directory chain."""
    if path not in ENGINE_RESOURCES | {MANIFEST, CHARTER} and not TARGET_PROFILE.fullmatch(path):
        raise Blocked("unapproved bundle resource")
    current = Path(root)
    if current.is_symlink():
        raise Blocked("bundle root cannot be a symlink")
    for part in Path(path).parts:
        current = current / part
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise Blocked("bundle resources cannot contain symlinks")
    metadata = current.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > RESOURCE_LIMIT:
        raise Blocked("bundle resource must be a bounded regular file")
    return current.read_text(encoding="utf-8")


def main():
    """Validate trusted packaging for CI without model calls; argument: target checkout."""
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    selection = json.loads((target / ".concurro/config.json").read_text())["execution_profiles"]
    bundle = Bundle(target=target, profiles=selection)
    print(json.dumps({"bundle": bundle.identity, "engine": bundle.engine_identity,
                      "resources": bundle.hashes}, sort_keys=True))


if __name__ == "__main__":
    main()
