"""Load an operator-owned, bounded instruction bundle without candidate resources."""

import classification
import hashlib
import json
from pathlib import Path
import stat
import sys

from policy import Blocked, digest

STAGES = ("intake", "plan", "plan_qc", "develop", "review")
SKILLS = dict(zip(STAGES, ("concurro-assess-scope", "concurro-design", "concurro-review-design",
                          "concurro-implement", "concurro-review-pr")))
MANIFEST = "bundle/manifest.json"
CHARTER = ".concurro/charter.json"
ENGINE_RESOURCES = {"bundle/instructions.md", "bundle/roles.json", "bundle/schemas/classification.json"} | {
    f"bundle/schemas/{stage}.json" for stage in STAGES} | {
    f"bundle/skills/{name}/SKILL.md" for name in SKILLS.values()}
INSTRUCTION_ROLES = {"intake": None, "plan": "planner", "plan_qc": "design-reviewer",
                     "develop": "implementer", "review": "reviewer"}
TARGET_INSTRUCTIONS = {f".concurro/instructions/{role}.md" for role in
                       ("shared", "planner", "design-reviewer", "implementer", "reviewer")}
RESOURCE_LIMIT = 24000


class Bundle:
    """Freeze engine instructions and the target's charter from explicitly allowlisted roots."""

    def __init__(self, root=None, target=None, charter_text=None, instruction_texts=None):
        """Freeze bounded engine instructions and the charter supplied from the target config commit."""
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
        if charter_text is None:
            self.target = Path(target) if target is not None else Path.cwd()
            charter_text = read(self.target, CHARTER)
            instruction_texts = {path: read(self.target, path) for path in sorted(TARGET_INSTRUCTIONS)
                                 if (self.target / path).exists() or (self.target / path).is_symlink()}
        elif not isinstance(charter_text, str) or len(charter_text.encode()) > RESOURCE_LIMIT:
            raise Blocked("bundle resource must be a bounded regular file")
        instruction_texts = {} if instruction_texts is None else instruction_texts
        if (not isinstance(instruction_texts, dict) or not set(instruction_texts) <= TARGET_INSTRUCTIONS
                or not all(isinstance(v, str) and len(v.encode()) <= RESOURCE_LIMIT for v in instruction_texts.values())):
            raise Blocked("target instructions must be bounded approved resources")
        self.resources.update(instruction_texts)
        self.resources[CHARTER] = charter_text
        if sum(len(v.encode()) for v in self.resources.values()) > 100000:
            raise Blocked("instruction bundle too large")
        self.hashes = hashes(self.resources)
        self.identity = digest(self.hashes)
        self.charter = json.loads(self.resources[CHARTER])
        if (self.charter.get("version") != 1 or not self.charter.get("id")
                or not self.charter.get("goals") or not self.charter.get("non_goals")
                or not self.charter.get("constraints") or not self.charter.get("acceptance")):
            raise Blocked("incomplete project charter")
        if (not isinstance(self.charter['goals'], dict)
                or not all(isinstance(key, str) and key.strip() for key in self.charter['goals'])):
            raise Blocked("charter goals must be keyed by nonempty IDs")
        roles = json.loads(self.resources["bundle/roles.json"])
        if (roles.get("version") != 1 or roles.get("shared") != "bundle/instructions.md"
                or set(roles.get("roles", {})) != set(STAGES)):
            raise Blocked("invalid role manifest")
        self.roles = roles["roles"]
        for stage, role in self.roles.items():
            expected = {"skill": f"bundle/skills/{SKILLS[stage]}/SKILL.md",
                        "schema": f"bundle/schemas/{stage}.json"}
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
    def instruction_texts(self):
        """Exact target instruction bytes for reconstructing the bundle in a runner."""
        return {p: self.resources[p] for p in sorted(TARGET_INSTRUCTIONS) if p in self.resources}

    def schema(self, stage):
        """Return the external response contract for one role."""
        schema = json.loads(self.resources[self.roles[stage]["schema"]])
        if stage in ('intake', 'plan_qc'):
            schema['properties']['scope']['properties']['goal_ids']['items']['enum'] = sorted(self.charter['goals'])
        if stage in classification.STAGES:
            schema["properties"]["classification"] = json.loads(self.resources["bundle/schemas/classification.json"])["schema"]
            schema["required"].append("classification")
        return schema

    def instructions(self, stage):
        """Load shared governance once and only the selected stage's skill."""
        return {"shared": self.resources["bundle/instructions.md"],
                "charter": self.charter, "role": stage,
                "skill": self.resources[self.roles[stage]["skill"]],
                "target": {name: self.resources[path] for name in ("shared", INSTRUCTION_ROLES[stage])
                           if name is not None and (path := f".concurro/instructions/{name}.md") in self.resources}}


def hashes(resources):
    """Hash each resource by its bundle-relative path."""
    return {p: hashlib.sha256(v.encode()).hexdigest() for p, v in resources.items()}


def read(root, path):
    """Read only regular bounded files under a real directory chain."""
    if path not in ENGINE_RESOURCES | TARGET_INSTRUCTIONS | {MANIFEST, CHARTER}:
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
    bundle = Bundle(target=target)
    print(json.dumps({"bundle": bundle.identity, "engine": bundle.engine_identity,
                      "resources": bundle.hashes}, sort_keys=True))


if __name__ == "__main__":
    main()
