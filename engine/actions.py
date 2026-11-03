"""Comments that ask a human to act, and Input needed requests; all other progress lives in the status comment."""

import conversation
import markup
from markup import block, code, link, text
from checkpoints import DIAGNOSTICS
from policy import Blocked, digest

# Gate reasons only a human merge resolves: Concurro's own merge policy, and GitHub's requirements
# while GitHub reports the PR mergeable without conflicts.
POLICY_REASONS = {"autonomous merge is disabled", "autonomous merge is limited to configured low-risk paths"}
GITHUB_REASON = "GitHub mergeability is not clean"
COMMENT_URL = "https://github.com/{repository}/issues/{issue}#issuecomment-{comment}"
# Blocks that the checkpoint health notice already reports; they get no Blocked comment of their own.
PUBLICATION_REASONS = {DIAGNOSTICS["conversation_publish"], DIAGNOSTICS["conversation_conflict"]}


def transition(state, config, author, plan_url=None):
    """Post the comment a stage change asks of a human, once per occurrence of that stage."""
    journal = state.setdefault("conversation", {"sequence": 0, "pending": []})
    stage = state["stage"]
    signature = digest([stage, state.get("reason") if stage in ("blocked", "escalated", "gates") else None])
    if journal.get("transition") == signature:
        return
    journal["transition"] = signature
    reason = (state.get("reason") or "")[:4000]
    retry = f"Edit the issue if needed, then a writer comments {code('/concurro retry')}."
    if stage == "clarify":
        request(state, config, author)
    elif stage == "escalated":
        conversation.queue(state, f"{markup.ESCALATED} Escalated", explain(reason, retry))
    elif stage == "rejected":
        conversation.queue(state, f"{markup.BLOCKED} Rejected", explain(reason, retry))
    elif stage == "blocked" and reason not in PUBLICATION_REASONS:
        conversation.queue(state, f"{markup.BLOCKED} Blocked", explain(
            reason, f"Once the cause is fixed, a writer comments {code('/concurro retry')}; "
                    "a reason that asks for reconciliation needs an operator first."))
    elif stage == "planned" and state.get("mode") == "manual":
        plan = link("Implementation plan", plan_url) if plan_url else "The implementation plan"
        conversation.queue(state, "Plan ready for approval", (
            f"{plan} passed design review. A writer approves it by commenting:\n\n"
            f"{code('/concurro approve ' + state['plan_id'])}"))


def explain(reason, continuation):
    """A reason's first sentence, the rest collapsed, then how to continue."""
    first = markup.first_sentence(reason)
    if not reason:
        body = ""
    elif first and first != markup.one_line(reason):
        body = text(first) + "\n\n" + markup.details("Full reason", block(reason))
    else:
        body = block(reason)
    return (body + "\n\n" if body else "") + continuation


def ready(reasons, pr):
    """Whether only a human merge stands between the PR and its base."""
    return bool(reasons) and all(r in POLICY_REASONS or r == GITHUB_REASON and pr.get("mergeable") is True
                                 for r in reasons)


def handoff(state, config, reasons, pr):
    """Record whether the PR is handed off now, and post the hand-off once per head; whether state changed.

    The posted head only prevents a second notification; the status comment shows `handoff_ready`.
    """
    now = ready(reasons, pr)
    changed = state.get("handoff_ready") != now
    state["handoff_ready"] = now
    if not now or state.get("handoff") == state["head"]:
        return changed
    state["handoff"] = state["head"]
    number = state["pr"]
    url = f"https://github.com/{config['repository']}/pull/{number}"
    remaining = "\n".join("- " + text(r) for r in reasons)
    conversation.queue(state, "✅ Handed off for merge", (
        f"Concurro's work on {link(f'PR #{number}', url)} is done. A human reviews and merges it.\n\n"
        f"{remaining}\n\nGitHub's own requirements, such as required reviews and checks, are shown on the PR."))
    return True


def questions(state, config):
    """The open questions that sent the task to clarification, each tagged with its provider."""
    found = []
    intake = state.get("qc", {}).get("intake")
    if intake and intake["decision"] == "clarify":
        found += [(config["qc"], q) for q in intake["questions"]]
    if state.get("plan"):
        found += [(config["planner"], q) for q in state["plan"]["questions"]]
    # A round records each vote before acting on it, so the clarifying vote is among these.
    for vote in state.get("design_votes", {}).values():
        found += [(vote["identity"]["provider"], q) for q in vote["report"]["questions"]]
    seen, result = set(), []
    for provider, question in found:
        if question not in seen:
            seen.add(question)
            result.append({"provider": provider, "question": question})
    return result


def request(state, config, author):
    """Ask once for these questions; new questions supersede an open request."""
    asked = questions(state, config)
    records = state.setdefault("requests", [])
    current = [r for r in records if r["state"] == "open"]
    if current and current[-1]["questions"] == asked:
        return
    number = len(records) + 1
    for record in current:
        record.update(state="superseded")
        edit(record, f"↪️ **Superseded** by request {number}, below.")
    mention = f"@{author}, " if author and not author.endswith("[bot]") else ""
    lines = [f"{mention}Concurro needs answers before it can continue.", ""]
    for index, item in enumerate(asked, 1):
        tag = f" ({text(item['provider'])})" if item["provider"] else ""
        lines += [f"**Q{index}.**{tag} {block(item['question'])}", ""]
    how = "Reply in a comment or edit the issue description; the author or any writer may answer."
    if state.get("mode") == "manual":
        how += f" Then a writer comments {code('/concurro start')}."
    lines.append(how)
    records.append({"number": number, "questions": asked, "state": "open", "comment": None,
                    "issue_digest": state["issue_digest"]})
    conversation.queue(state, "❓ Input needed", "\n".join(lines), request=number)


def posted(state, number, comment, body):
    """Record a request's comment once it is published."""
    record = next(r for r in state["requests"] if r["number"] == number)
    record.update(comment=comment, body=body)


def edit(record, line):
    """Intend an edit that puts `line` under the request's marker; applied by `publish`."""
    if not record.get("comment"):
        return
    marker, rest = record["body"].split("\n", 1)
    record["edit"] = {"body": f"{marker}\n{line}\n\n{rest}", "state": "intended"}


def answers(state, config, comments, issue_digest, answerer):
    """Mark open requests answered by a reply from the author or a writer, or an edit to the issue.

    `answerer(login)` says whether a login may answer. Leaving clarification for any other
    reason, such as a base change, records no answer.
    """
    changed = False
    for record in state.get("requests", []):
        if record["state"] != "open" or not record.get("comment"):
            continue
        reply = next((c for c in sorted(comments, key=lambda c: c["id"]) if c["id"] > record["comment"]
                      and not c["body"].startswith(("<!-- concurro:", "/concurro "))
                      and answerer(c["user"]["login"])), None)
        if reply:
            url = COMMENT_URL.format(repository=config["repository"], issue=state["issue"], comment=reply["id"])
            record.update(state="answered", answer={"comment": reply["id"]})
            edit(record, f"✅ **Answered** by {text(reply['user']['login'])}: {link('reply', url)}")
        elif issue_digest != record["issue_digest"]:
            record.update(state="answered", answer={"issue_digest": issue_digest})
            edit(record, "✅ **Answered**: the issue description was edited.")
        else:
            continue
        changed = True
    return changed


def publish(state, github, checkpoint):
    """Apply intended request edits; a lost response completes by the edited body on the next tick."""
    for record in state.get("requests", []):
        intended = record.get("edit")
        if not intended or intended["state"] != "intended":
            continue
        current = github.repo(f"issues/comments/{record['comment']}")["body"]
        if current not in (intended["body"], record["body"]):
            raise Blocked("Concurro request comment differs from its recorded content; reconcile manually")
        if current != intended["body"]:
            github.repo(f"issues/comments/{record['comment']}", "PATCH", {"body": intended["body"]})
        intended["state"] = "done"
        checkpoint()
