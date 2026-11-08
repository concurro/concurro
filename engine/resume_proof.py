"""Prove a retained Codex session resumes in a fresh native process from the runner's archive.

Optional diagnostic on a GitHub-hosted runner holding its issued session key. A
first turn runs in one native process; its retained directory is packed as runners pack it,
restored into a fresh directory, and the same thread is resumed in a new native process with
a question only the first turn's context answers. Prints what the archive held and what it
left out. Uses the agent's own Codex invocation (`agent_entry.command_for`).
"""

import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys

import agent_entry
import turns

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
          "additionalProperties": False}


class ProofFailed(Exception):
    """The resumed session did not carry the first turn's context."""


def prepare(directory):
    """A turn's directories: the retained root, a workspace and the proof's schema and result."""
    for name in ("retained", "work", "proof"):
        (directory / name).mkdir(parents=True)
    (directory / "proof/schema.json").write_text(json.dumps(SCHEMA))
    return directory


def turn(key, directory, request, prompt):
    """Run one Codex turn as the agent native process does; returns its thread ID and answer."""
    command = agent_entry.command_for(request, str(directory / "proof/schema.json"), str(directory / "proof/result.json"))
    (directory / "retained/codex").mkdir(exist_ok=True)
    result = subprocess.run(command, input=prompt, capture_output=True, text=True, timeout=900,
                            cwd=directory / "work", env=turns.agent_environment(directory, key, True))
    if result.returncode:
        raise ProofFailed(f"Codex exited {result.returncode}")
    events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    threads = [e["thread_id"] for e in events if e.get("type") == "thread.started"]
    if len(threads) != 1:
        raise ProofFailed(f"expected one thread, saw {len(threads)}")
    return threads[0], json.loads((directory / "proof/result.json").read_text())["answer"]


def report(directory):
    """Print each entry two levels down with its size, marking those the archive leaves out."""
    root = Path(directory)
    kept = set(turns.retained_entries(root))
    sizes = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            entry = (Path(*path.relative_to(root).parts[:2]), path in kept)
            sizes[entry] = sizes.get(entry, 0) + path.stat().st_size
    for (entry, archived), size in sorted(sizes.items(), key=lambda item: -item[1]):
        print(f"  {'kept   ' if archived else 'dropped'} {size // 1024:>8} KiB  {json.dumps(str(entry))}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", required=True, help="gateway base URL")
    parser.add_argument("--key", required=True, type=Path, help="file holding the session key")
    parser.add_argument("--model", required=True)
    parser.add_argument("--root", required=True, type=Path, help="empty directory for both turns")
    args = parser.parse_args(argv)
    key = args.key.read_text().strip()
    codeword = "concurro-" + secrets.token_hex(4)
    request = {"provider": "codex", "model": args.model, "session": {"scope": "proof"}, "resume_id": None,
               "gateway": {"url": args.gateway, "headers": {"codex": {"x-concurro-plan": "chatgpt"}}}}
    first, second = prepare(args.root / "first"), prepare(args.root / "second")

    thread, _ = turn(key, first, request,
                     f"Remember this codeword for a later turn: {codeword}. Answer with the single word: noted.")
    print(f"first turn: thread {thread}; retained directory:")
    report(first / "retained")
    archive = turns.pack(first / "retained")
    print(f"archive: {len(archive) * 3 // 4 // 1024} KiB compressed (limit {turns.MAX_RETAINED // 1024} KiB)")

    turns.unpack(archive, second / "retained")
    resumed, answer = turn(key, second, {**request, "resume_id": thread},
                           "What codeword did I give you in the earlier turn? Answer with the codeword alone.")
    print(f"resumed turn in a fresh native process and directory: thread {resumed}; answer {json.dumps(answer)}")
    if resumed != thread:
        raise ProofFailed("Codex resumed a different thread")
    if codeword not in answer:
        raise ProofFailed("the resumed turn did not recall the first turn's codeword")
    print("proof: the restored archive resumed the same thread with its context")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ProofFailed, turns.TurnError) as error:
        print(f"proof failed: {error}", file=sys.stderr)
        sys.exit(1)
