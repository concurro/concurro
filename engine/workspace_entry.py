"""Trusted local Git operations inside a credential-free container, never on the host."""

import difflib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tarfile

sys.path.insert(0, "/opt/concurro")
from policy import path_safe
from checks import NAME, file_rule_errors, glob_matches

ROOT = Path("/workspace")
STATE = Path("/state")
STAGE = Path("/stage")
INPUT = Path("/input")
CACHE = Path("/cache")


def git(*args):
    """Use isolated Git metadata and disable hooks, global configuration and signing."""
    env = {**os.environ, "GIT_DIR": str(STATE / "git"), "GIT_WORK_TREE": str(ROOT),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_AUTHOR_NAME": "Concurro local", "GIT_AUTHOR_EMAIL": "concurro@localhost",
           "GIT_COMMITTER_NAME": "Concurro local", "GIT_COMMITTER_EMAIL": "concurro@localhost"}
    if args[0] == "init":
        env.pop("GIT_DIR")
        env.pop("GIT_WORK_TREE")
    return subprocess.check_output(["git", "-c", "core.bare=false", "-c", "core.hooksPath=/dev/null",
                                    "-c", "commit.gpgsign=false", *args], env=env, text=True).strip()


def safe_file(name):
    """Reject symlink ancestors and non-regular files before reading or editing source."""
    if not path_safe(name):
        raise ValueError("forbidden source path")
    path = ROOT
    for part in Path(name).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlink source path")
    if path.exists() and not path.is_file():
        raise ValueError("non-regular source file")
    return path


def extract_member(archive, member, base=None):
    """Extract allowed entries without ownership changes or version-dependent tar filters."""
    base = ROOT if base is None else base
    root = base.resolve()
    destination = base / member.name
    resolved = destination.resolve()
    if not resolved.is_relative_to(root) or ".git" in resolved.relative_to(root).parts:
        raise ValueError("archive destination escapes source")
    if destination.is_symlink():
        raise ValueError("archive replaces a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if member.issym():
        target = (destination.parent / member.linkname).resolve()
        if (Path(member.linkname).is_absolute() or not target.is_relative_to(root)
                or ".git" in target.relative_to(root).parts):
            raise ValueError("unsafe archive symlink")
        destination.symlink_to(member.linkname)
    elif member.isdir():
        destination.mkdir(exist_ok=True)
    else:
        with archive.extractfile(member) as source, destination.open("xb") as output:
            shutil.copyfileobj(source, output, length=65536)
        destination.chmod(0o755 if member.mode & 0o111 else 0o644)


def extract(stream, base):
    """Safely extract one single-root GitHub archive into a source tree."""
    total = 0
    prefix = None
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        for member in archive:
            parts = Path(member.name).parts
            if not parts or Path(member.name).is_absolute() or ".." in parts or ".git" in parts:
                raise ValueError("unsafe archive path")
            prefix = prefix or parts[0]
            if prefix != parts[0] or member.islnk() or not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError("unsupported archive entry")
            if len(parts) == 1:
                continue
            total += member.size
            if total > 500_000_000:
                raise ValueError("archive expansion limit exceeded")
            member.name = "/".join(parts[1:])
            extract_member(archive, member, base)


def initialize(branch, *caches):
    """Safely extract the GitHub archive and create a local-only baseline branch."""
    if not branch.startswith("concurro/") or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789/-" for c in branch):
        raise ValueError("invalid local branch")
    extract(sys.stdin.buffer, ROOT)
    for cache in caches:
        # Engine-assigned mounts only: /cache/<profile>/<name>, writable by the unprivileged runner.
        parts = Path(cache).relative_to(CACHE).parts
        if len(parts) != 2 or not all(NAME.fullmatch(part) for part in parts):
            raise ValueError("invalid cache mount")
        CACHE.joinpath(*parts).chmod(0o777)
    git("init", "--bare", "--initial-branch=main", str(STATE / "git"))
    git("symbolic-ref", "HEAD", "refs/heads/" + branch)
    git("add", "--force", "-A")
    git("commit", "-m", "Local baseline")
    STATE.joinpath("base").write_text(git("rev-parse", "HEAD"))
    return snapshot()


def stage():
    """Extract the approved base commit, separate from the possibly published workbench parent."""
    extract(sys.stdin.buffer, STAGE)
    return {"staged": True}


def inputs(profile, paths):
    """Copy only a profile's protected preparation inputs from the approved base snapshot."""
    if not NAME.fullmatch(profile) or not isinstance(paths, list) or not 1 <= len(paths) <= 16:
        raise ValueError("invalid preparation inputs")
    destination = INPUT / profile
    for name in paths:
        if not isinstance(name, str) or not name or name.startswith("/") or "\\" in name or any(
                part in ("", ".", "..", ".git") for part in name.split("/")):
            raise ValueError("invalid preparation input path")
        source = STAGE
        for part in name.split("/"):
            source = source / part
            if source.is_symlink():
                raise ValueError("preparation input is a symlink")
        if not source.is_file():
            raise ValueError("preparation input is missing at the approved base")
        target = destination.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o444)
    for directory in [destination, *destination.rglob("*")]:
        if directory.is_dir():
            directory.chmod(0o755)
    return {"inputs": sorted(paths)}


def snapshot():
    """Identify both the local checkpoint and its complete source tree."""
    return {"local_commit": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
            "branch": git("symbolic-ref", "--short", "HEAD")}


def clean_checkpoint():
    """Reject untracked, staged or unstaged content omitted by checkpoint-based checks."""
    if git('status', '--porcelain', '--untracked-files=all'):
        raise ValueError('candidate has uncommitted files')


def read_under(root, name):
    """A regular file's text under `root`, or None when absent; symlinked ancestors are refused."""
    target = root
    for part in name.split("/"):
        target = target / part
        if target.is_symlink():
            raise ValueError("path crosses a symlink")
    if not target.exists():
        return None
    if not target.is_file():
        raise ValueError("path is not a regular file")
    return target.read_text()


def lines_of(text):
    """LF-delimited lines; a final newline ends the last line rather than starting another."""
    lines = (text or "").split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def changed_lines(before, after):
    """Lines added plus lines removed between two versions of a file; any other difference counts as one."""
    matcher = difflib.SequenceMatcher(None, lines_of(before), lines_of(after), autojunk=False)
    count = sum(i2 - i1 + j2 - j1 for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal")
    return max(count, int(before != after))


def check_scope(payload):
    """Judge the whole candidate against the staged approved base: envelope, budget, profiles, planned files.

    The local Git base is the parent, which may be an earlier published round; the changed set adds
    the files those rounds changed, so budgets and the envelope always cover the cumulative change.
    """
    clean_checkpoint()
    scope = payload['scope']
    names = set(git('diff', '--name-only', STATE.joinpath('base').read_text(), 'HEAD').splitlines())
    actual, lines = {}, 0
    for path in sorted(names | set(scope['accumulated'])):
        before, after = read_under(STAGE, path), read_under(ROOT, path)
        if before != after:
            actual[path] = 'A' if before is None else 'D' if after is None else 'M'
            lines += changed_lines(before, after)
    if not actual:
        raise ValueError('candidate changes nothing')
    outside = [p for p in actual if not any(glob_matches(p, g) for g in scope['paths'])]
    if outside:
        raise ValueError('candidate changes paths outside the approved scope: ' + ', '.join(outside))
    unprepared = [p for p in actual if any(glob_matches(p, g) for g in scope['unapproved'])]
    if unprepared:
        raise ValueError('candidate triggers an execution profile the design did not approve: ' + ', '.join(unprepared))
    if len(actual) > scope['max_files']:
        raise ValueError(f"candidate changes {len(actual)} files, over the approved budget of {scope['max_files']}")
    if scope['max_changed_lines'] is not None and lines > scope['max_changed_lines']:
        raise ValueError(f"candidate changes {lines} lines, over the approved budget of {scope['max_changed_lines']}")
    for rule in payload['spec']['files']:
        if actual.get(rule['path']) != ('A' if rule['change'] == 'add' else 'M'):
            raise ValueError('candidate does not make the planned change to ' + rule['path'])


def check_text(payload):
    """Enforce literal text assertions and newline-based line limits on actual candidate files."""
    clean_checkpoint()
    for rule in payload['spec']['files']:
        text = safe_file(rule['path']).read_text()
        lines = text.count('\n') + int(bool(text) and not text.endswith('\n'))
        if rule['max_lines'] is not None and lines > rule['max_lines']:
            raise ValueError('candidate exceeds declared line limit')
        if any(value not in text for value in rule['required_text']):
            raise ValueError('candidate is missing required text')


def scope_valid(scope):
    """The scope payload: envelope globs, unapproved triggers, accumulated paths and a budget."""
    globs = lambda v: isinstance(v, list) and all(isinstance(g, str) and g and not g.startswith("/") for g in v)
    return (isinstance(scope, dict) and set(scope) == {'paths', 'unapproved', 'accumulated', 'max_files', 'max_changed_lines'}
            and globs(scope['paths']) and globs(scope['unapproved'])
            and isinstance(scope['accumulated'], list) and all(path_safe(p) for p in scope['accumulated'])
            and type(scope['max_files']) is int and scope['max_files'] >= 1
            and (scope['max_changed_lines'] is None or type(scope['max_changed_lines']) is int))


def main():
    """Perform one trusted operation; candidate test execution uses another container."""
    operation = sys.argv[1]
    if operation == "init":
        result = initialize(sys.argv[2], *sys.argv[3:])
    elif operation == "stage":
        result = stage()
    elif operation == "inputs":
        result = inputs(sys.argv[2], json.load(sys.stdin))
    elif operation == "checkpoint":
        payload = json.load(sys.stdin)
        if git("rev-parse", "HEAD") != payload["expected"]:
            raise ValueError("local head moved")
        for edit in payload["edits"]:
            path = safe_file(edit["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(edit["content"])
        if payload["edits"]:
            git("add", "--force", "-A")
            git("commit", "-m", "Agent local checkpoint")
        result = snapshot()
    elif operation == "files":
        result = {name: safe_file(name).read_text() if safe_file(name).exists() else None
                  for name in json.load(sys.stdin)}
    elif operation == "export":
        base = STATE.joinpath("base").read_text()
        names = git("diff", "--name-only", base, "HEAD").splitlines()
        result = {**snapshot(), "edits": [{"path": name, "content": safe_file(name).read_text()} for name in names]}
    elif operation == "diff":
        clean_checkpoint()
        # Git output is diagnostic data. Disable repository-defined external diff tools.
        git("diff", "--no-ext-diff", "--no-textconv", "--check", STATE.joinpath("base").read_text(), "HEAD")
        result = snapshot()
    elif operation == 'validation':
        payload = json.load(sys.stdin)
        if (set(payload) != {'spec', 'originals', 'scope'} or not scope_valid(payload['scope'])
                or not isinstance(payload['originals'], dict)
                or not all(path_safe(p) and (v is None or isinstance(v, str)) for p, v in payload['originals'].items())
                or file_rule_errors({'files': list(payload['originals']), 'validation': payload['spec']})):
            raise ValueError('invalid validation contract')
        STATE.joinpath('validation.json').write_text(json.dumps(payload))
        result = snapshot()
    elif operation in ('scope', 'text'):
        payload = json.loads(STATE.joinpath('validation.json').read_text())
        (check_scope if operation == 'scope' else check_text)(payload)
        result = snapshot()
    else:
        raise ValueError("unknown operation")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
