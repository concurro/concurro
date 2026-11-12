"""Commands a verified writer gave through the backoffice, posted by the service as the App's own comment.

The service checks the writer's identity and permission, then posts one App-authored comment: a
human-readable status line and a marker naming the writer and the exact `/concurro` command,
signed with the service's delegation key. The controller treats such a comment as that writer's
own command comment (same ID, author and body), so ordering, replay protection, approval binding
and the writer re-check at processing time are unchanged.

A comment counts only if the App wrote it, its body is exactly the rendering of its marker's
fields, and the signature over the repository, issue and those fields is valid. App comments that
quote untrusted text therefore can't carry a command: a quoted marker never makes up the whole body.
"""

import hashlib
import hmac
import json
import re

import commands

MARKER = "<!-- concurro:delegated-command:v1 "
FIELDS = ("actor", "actor_id", "command", "nonce")
NONCE = re.compile(r"[0-9a-f]{16,64}")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
DESCRIPTIONS = {
    commands.CommandKind.SPONSOR: "sponsored {argument} for future contributed calls on this issue",
    commands.CommandKind.WITHDRAW: "withdrew their {argument} sponsorship",
    commands.CommandKind.START: "started this task",
    commands.CommandKind.APPROVE: "approved plan `{argument}`",
    commands.CommandKind.STOP: "stopped this task",
    commands.CommandKind.RESUME: "resumed this task",
    commands.CommandKind.RETRY: "requested a retry",
    commands.CommandKind.EXTEND: "granted {argument} more plan revisions and requested a retry",
    commands.CommandKind.INVALIDATE: "invalidated this task",
}


def command_text(kind, argument=None):
    """The exact `/concurro` command for a kind and its argument, or ValueError if the grammar refuses it."""
    if kind in ("sponsor", "withdraw") and argument is not None:
        text = f"/concurro {kind} {argument}"
    elif kind == "approve":
        text = f"/concurro approve {argument}"
    elif kind == "retry" and argument is not None:
        text = f"/concurro retry --plan-revisions {argument}"
    else:
        text = f"/concurro {kind}"
    if commands.parse(text) is None:
        raise ValueError("not a valid command")
    return text


def signature(key, repository, issue, fields):
    """HMAC-SHA256 over the repository, the issue and the marker's fields."""
    message = json.dumps([repository.lower(), issue, *(fields[name] for name in FIELDS)], separators=(",", ":"))
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def render(key, repository, issue, actor, actor_id, command, nonce):
    """The App comment body for one delegated command."""
    parsed = commands.parse(command)
    if parsed is None or not LOGIN.fullmatch(actor) or type(actor_id) is not int or not NONCE.fullmatch(nonce):
        raise ValueError("invalid delegated command")
    fields = {"actor": actor, "actor_id": actor_id, "command": command, "nonce": nonce}
    marker = json.dumps({**fields, "signature": signature(key, repository, issue, fields)}, sort_keys=True)
    line = DESCRIPTIONS[parsed.kind].format(argument=str(parsed.argument if parsed.argument is not None else "")[:12])
    if parsed.kind == commands.CommandKind.SPONSOR and parsed.argument is None:
        line = "requested sponsorship status; use /concurro sponsor openai or /concurro sponsor anthropic"
    notice = (
        " Current calls remain org-funded."
        if parsed.kind in (commands.CommandKind.SPONSOR, commands.CommandKind.WITHDRAW)
        else ""
    )
    return f"@{actor} {line} from the Concurro backoffice.{notice}\n\n{MARKER}{marker} -->"


def marked(body):
    """Whether a body has the shape of a delegated command; a cheap wake-up hint, not verification."""
    return f"\n\n{MARKER}" in body and body.endswith(" -->")


def sponsorship_hint(body):
    """Recognize provider-command wakeup candidates; signature verification remains in resolve()."""
    if not marked(body):
        return False
    try:
        data = json.loads(body.rsplit(MARKER, 1)[1].removesuffix(" -->"))
        command = commands.parse(data["command"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return False
    return command is not None and command.kind in (commands.CommandKind.SPONSOR, commands.CommandKind.WITHDRAW)


def resolve(comment, key, repository, issue, author):
    """The writer's equivalent command comment for a verified delegated comment, else None."""
    body = comment.get("body") or ""
    if not key or (comment.get("user") or {}).get("login") != author or MARKER not in body:
        return None
    try:
        data = json.loads(body.rsplit(MARKER, 1)[1].removesuffix(" -->"))
        fields = {name: data[name] for name in FIELDS}
        expected = render(key, repository, issue, *(fields[name] for name in FIELDS))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    # The rendering embeds the signature, so an exact match also verifies it.
    if body != expected:
        return None
    return {
        **comment,
        "user": {**comment["user"], "login": fields["actor"], "id": fields["actor_id"]},
        "body": fields["command"],
    }


def effective(comments, key, repository, issue, author):
    """Comments with each verified delegated command in its writer's place; everything else unchanged."""
    if not key:
        return comments
    return [resolve(comment, key, repository, issue, author) or comment for comment in comments]
