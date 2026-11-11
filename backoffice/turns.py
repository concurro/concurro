"""Read run diagnostics recorded by the service (`service.diagnostics`) into the frontend's `Turn` shape."""

import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from service import diagnostics

ROLES = {
    "intake": "triage",
    "plan": "planner",
    "plan_qc": "plan_reviewer",
    "develop": "implementer",
    "review": "pr_reviewer",
}
STAGES = {"develop": "implement"}
TEXT_LIMIT = 20000
TRANSCRIPT_ITEMS = 2000


def iso(stamp: float | None) -> str | None:
    return datetime.fromtimestamp(stamp, UTC).isoformat().replace("+00:00", "Z") if stamp else None


def clip(text: str) -> str:
    return text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + f"\n… ({len(text) - TEXT_LIMIT} more characters)"


def pretty(value) -> str:
    return "" if value is None else json.dumps(value, indent=2, sort_keys=True)


class Diagnostics:
    """Read-only access to the diagnostics directory; absent until the service has recorded a turn."""

    def __init__(self, root: Path | None):
        self.root = root

    def rows(self, repository: str, issue: int) -> list:
        if not self.root or not (self.root / diagnostics.DATABASE).exists():
            return []
        db = diagnostics.connect(self.root, readonly=True)
        try:
            return db.execute(
                "SELECT * FROM turns WHERE repository = ? AND issue = ? ORDER BY submitted, id", (repository, issue)
            ).fetchall()
        finally:
            db.close()

    def turns(self, repository: str, issue: int, detail: bool = True) -> list[dict]:
        return [self.turn(n, row, detail) for n, row in enumerate(self.rows(repository, issue), start=1)]

    def transcript(self, sha: str | None) -> list[dict]:
        if not self.root or not sha:
            return []
        path = diagnostics.blob_path(self.root, sha)
        return normalize(path.read_bytes()) if path.exists() else []

    def turn(self, n: int, row, detail: bool) -> dict:
        agent = json.loads(row["agent"]) if row["agent"] else {}
        prompt = json.loads(row["prompt"]) if row["prompt"] else {}
        started, created, finished = row["started"], row["created"], row["finished"]
        workspace = json.loads(row["workspace"]) if row["workspace"] else None
        sessions = json.loads(row["runner_sessions"] or "[]")
        outcome = row["outcome"] or ("queued" if row["status"] == "queued" else "running")
        result = {
            "id": row["id"],
            "n": n,
            "stage": STAGES.get(row["stage"], row["stage"]),
            "role": ROLES.get(row["stage"], row["stage"]),
            "agent": agent.get("profile") or row["provider"] or "",
            # A profile name the frontend resolves against the target's profiles.
            "profile": agent.get("profile") or row["provider"] or "",
            "outcome": outcome,
            "startedAt": iso(started or created or row["submitted"]),
            "queuedMs": round((started - created) * 1000) if started and created else 0,
            "durationMs": round((finished - started) * 1000)
            if finished and started
            else round((row["elapsed_seconds"] or 0) * 1000),
            "tokens": {"input": 0, "cached": 0, "output": 0},
            "run": {
                "id": 0,
                "attempt": 1,
                "workflow": "agent.yml",
                "runner": sessions[-1] if sessions else "",
                "jobs": [],
                **({"diagnosis": diagnosis(row)} if row["termination_reason"] else {}),
            },
            "session": {"scope": row["session_scope"], "turn": row["session_turn"]} if row["session_scope"] else None,
            "transcriptBytes": row["transcript_bytes"],
        }
        if detail:
            result |= {
                "prompt": {
                    "instructions": clip(pretty(prompt.get("instructions"))),
                    "snapshot": pretty(prompt.get("repository_snapshot")),
                    "schema": clip(pretty(prompt.get("response_schema"))),
                    "untrusted": clip(pretty(prompt.get("untrusted_context"))),
                },
                "transcript": self.transcript(row["transcript"]),
                "response": clip(pretty(json.loads(row["response"]) if row["response"] else None)),
                "validationErrors": [],
                **({"diff": workspace_diff(workspace)} if workspace else {}),
            }
        return result


def diagnosis(row) -> dict:
    return {
        "tone": "caution",
        "title": f"Turn ended: {row['termination_reason'].replace('_', ' ')}",
        "body": f"Elapsed {row['elapsed_seconds'] or 0:.0f}s before the runner stopped the turn.",
    }


def workspace_diff(workspace: dict) -> str:
    """A readable summary of the implementer's extracted changes (full file contents, not a patch)."""
    if workspace.get("error"):
        return "Workspace extraction failed: " + workspace["error"]
    parts = []
    for edit in workspace.get("edits", []):
        body = edit.get("content")
        parts.append(f"=== {edit.get('path')}\n" + (clip(body) if isinstance(body, str) else "(binary or encoded)"))
    parts += [f"=== {path} (removed)" for path in workspace.get("removed", [])]
    return "\n\n".join(parts)


def normalize(archive: bytes) -> list[dict]:
    """Messages, tool calls and tool results from Claude Code or Codex session logs in an archive."""
    items: list[dict] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            members = sorted((m for m in bundle.getmembers() if m.isfile() and m.name.endswith(".jsonl")), key=order)
            for member in members:
                stream = bundle.extractfile(member)
                if stream is None:
                    continue
                parse = codex if member.name.startswith("codex/") else claude
                for line in io.TextIOWrapper(stream, encoding="utf-8", errors="replace"):
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        items += parse(event)
                    if len(items) >= TRANSCRIPT_ITEMS:
                        return items[:TRANSCRIPT_ITEMS]
    except (tarfile.TarError, OSError, EOFError):
        return [{"kind": "assistant", "text": "(transcript archive could not be read)"}]
    return items


def order(member: tarfile.TarInfo) -> tuple:
    return (member.mtime, member.name)


def text(value) -> str:
    if isinstance(value, str):
        return clip(value)
    if isinstance(value, list):
        return clip(
            "\n".join(text(v.get("text", v.get("content", ""))) if isinstance(v, dict) else text(v) for v in value)
        )
    return clip(json.dumps(value)) if value is not None else ""


def claude(event: dict) -> list[dict]:
    message = event.get("message")
    if event.get("type") not in ("assistant", "user") or not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"kind": "assistant", "text": clip(content)}] if event["type"] == "assistant" else []
    items = []
    for block in content if isinstance(content, list) else []:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text" and event["type"] == "assistant":
            items.append({"kind": "assistant", "text": clip(block.get("text", ""))})
        elif kind == "tool_use":
            items.append(
                {
                    "kind": "tool_call",
                    "id": block.get("id", ""),
                    "tool": block.get("name", ""),
                    "input": text(block.get("input")),
                }
            )
        elif kind == "tool_result":
            items.append(
                {
                    "kind": "tool_result",
                    "id": block.get("tool_use_id", ""),
                    "output": text(block.get("content")),
                    "isError": bool(block.get("is_error")),
                }
            )
    return items


def codex(event: dict) -> list[dict]:
    payload = event.get("payload")
    if event.get("type") != "response_item" or not isinstance(payload, dict):
        return []
    kind = payload.get("type")
    if kind == "message" and payload.get("role") == "assistant":
        return [{"kind": "assistant", "text": text(payload.get("content"))}]
    if kind in ("function_call", "custom_tool_call", "local_shell_call"):
        arguments = payload.get("arguments", payload.get("input", payload.get("action")))
        return [
            {
                "kind": "tool_call",
                "id": payload.get("call_id", ""),
                "tool": payload.get("name", kind),
                "input": text(arguments),
            }
        ]
    if kind in ("function_call_output", "custom_tool_call_output"):
        output = payload.get("output")
        if isinstance(output, dict):
            output = output.get("content", output)
        return [{"kind": "tool_result", "id": payload.get("call_id", ""), "output": text(output)}]
    return []
