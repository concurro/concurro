"""Actionable comments and Input needed requests; advisory intake notices use the conversation outbox."""

import re

import conversation
import markup
import protocol
import reason_guidance
from checkpoints import DIAGNOSTICS
from github import NotFound
from markup import code, link, text
from model_config import assigned
from policy import Blocked, digest
from protocol import COMMENT_URL as COMMENT_URL
from vocab import TERMINAL_STAGES

# Gate reasons only a human merge resolves: Concurro's own merge policy, and GitHub's requirements
# while GitHub reports the PR mergeable without conflicts.
POLICY_REASONS = {"autonomous merge is disabled", "autonomous merge is limited to configured low-risk paths"}
GITHUB_REASON = "GitHub mergeability is not clean"
BUNDLE_REASON = (
    "Trusted charter, role, skill or schema bundle changed. "
    "A writer must use /concurro invalidate to reassess under the current bundle; "
    "/concurro retry does not change the task's bundle."
)
# Blocks already reported by the checkpoint health notice.
PUBLICATION_REASONS = {DIAGNOSTICS["conversation_publish"], DIAGNOSTICS["conversation_conflict"]}
RESUMED_STAGES = {
    "intake",
    "plan",
    "plan_qc",
    "planned",
    "clarify",
    "implement",
    "publish",
    "review",
    "gates",
    "merging",
}
BLOCKED_HEADER = re.compile(protocol.EVENT_MARKER + protocol.DIGEST_PATTERN + r" -->\n### (?:🛑 )?Blocked\n")
BLOCKED_RETRY = (
    f"Once the cause is fixed, a writer comments {code('/concurro retry')}; "
    "a reason that asks for reconciliation needs an operator first."
)
BLOCKED_INVALIDATE = (
    f"A writer accepts a new assessment with {code('/concurro invalidate')}. "
    "Otherwise restore the previous bundle before retrying."
)


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
    if state.get("revision_wait"):
        retry = (
            "A writer can grant 1–3 additional plan revisions, for example "
            + code("/concurro retry --plan-revisions 1")
            + ". Findings and spent budgets are preserved; plain retry cannot increase the allowance."
        )
    if stage == "clarify":
        request(state, config, author)
    elif stage == "escalated":
        conversation.queue(state, f"{markup.ESCALATED} Escalated", explain(reason, retry, state, config))
    elif stage == "rejected":
        conversation.queue(state, f"{markup.BLOCKED} Rejected", explain(reason, retry, state, config))
    elif stage == "blocked" and reason not in PUBLICATION_REASONS:
        continuation = BLOCKED_INVALIDATE if reason == BUNDLE_REASON else BLOCKED_RETRY
        conversation.queue(state, f"{markup.BLOCKED} Blocked", explain(reason, continuation, state, config))
    elif stage == "planned" and state.get("mode") == "manual":
        plan = link("Implementation plan", plan_url) if plan_url else "The implementation plan"
        conversation.queue(
            state,
            "Plan ready for approval",
            (
                f"{plan} passed design review. A writer approves it by commenting:\n\n"
                f"{code('/concurro approve ' + state['plan_id'])}"
            ),
        )


def blocked_posted(state, comment, body):
    """Remember a notice ID and original/resolved digests, never its full text."""
    if not BLOCKED_HEADER.match(body):
        return False
    records = state.setdefault("blocked_notices", [])
    state["blocked_notice_cursor"] = max(state.get("blocked_notice_cursor", 0), comment)
    if any(record["comment"] == comment for record in records):
        return False
    records.append({"comment": comment, "digest": digest(body), "resolved_digest": digest(blocked_resolution(body))})
    return True


def discover_blocked(state, comments, author):
    """Discover older App notices once; skipped edits must never be adopted again."""
    if state.get("stage") in TERMINAL_STAGES:
        return
    cursor = state.get("blocked_notice_cursor", 0)
    for comment in comments:
        if comment["id"] > cursor and comment.get("user", {}).get("login") == author:
            blocked_posted(state, comment["id"], comment.get("body", ""))


def blocked_resolution(body):
    """Keep the struck-through heading and reason; remove engine continuations and event footer."""
    marker, _, original = body.split("\n", 2)
    reason = re.sub(r"\n\n<sub>Event [0-9]+</sub>$", "", original).strip("\n")
    for continuation in (BLOCKED_RETRY, BLOCKED_INVALIDATE):
        if reason == continuation:
            reason = ""
            break
        if reason.endswith("\n\n" + continuation):
            reason = reason.removesuffix("\n\n" + continuation)
            break
    reason = reason.removesuffix("\n\n**What you can do:**").rstrip()
    history = "<del>\n\n### Blocked" + ("\n\n" + reason if reason else "") + "\n\n</del>"
    quoted = "\n".join("> " + line if line else ">" for line in history.split("\n"))
    return marker + "\n" + quoted + "\n\n**This blocker has been resolved.**"


def publish_blocked(state, github, checkpoint):
    """Resolve notices best-effort on resumption; edited or deleted history never blocks work."""
    records = state.get("blocked_notices", [])
    if not records or state.get("stage") not in RESUMED_STAGES:
        return
    # The stage, IDs, original/resolved digests and discovery cursor must survive a lost PATCH response.
    checkpoint()
    for record in list(records):
        path = f"issues/comments/{record['comment']}"
        try:
            current = github.repo(path)["body"]
            current_digest = digest(current)
            if current_digest != record["resolved_digest"] and current_digest == record["digest"]:
                github.repo(path, "PATCH", {"body": blocked_resolution(current)})
        except NotFound:
            pass
        records.remove(record)
        checkpoint()


def explain(reason, continuation, state=None, config=None):
    """Describe the blocker, link its evidence, and give the applicable human action."""
    state, config = state or {}, config or {}
    first = markup.first_sentence(reason)
    operator = reason_guidance.operator_summary(reason, state.get("stage", "blocked"))
    if operator:
        summary = operator
        remainder = reason
        continuation = reason_guidance.OPERATOR_ACTION
    else:
        summary = first or markup.one_line(reason)[:1000]
        normalized = markup.one_line(reason)
        remainder = normalized[len(first) :].lstrip() if first else normalized[len(summary) :].lstrip()
        if not first and remainder:
            summary += "…"
    lines = ["**What happened:** " + text(summary)] if summary else []
    evidence = []
    if config.get("repository"):
        for finding in state.get("findings", []):
            if finding["status"] in ("open", "withdrawn") and finding.get("comment") and state.get("pr"):
                repository = config.get("upstream", config["repository"])
                url = f"https://github.com/{repository}/pull/{state['pr']}#discussion_r{finding['comment']}"
                evidence.append(link(finding["id"], url))
        objections = [o["id"] for o in state.get("objections", []) if not o["resolved"]]
        if objections:
            reports = [r for r in state.get("reports", []) if r["step"] == "Design review" and r.get("artifact")]
            evidence += [
                link("Design review", protocol.artifact_url(config["repository"], state["issue"], r["artifact"]))
                for r in reports[-4:]
            ]
            evidence.append("Open objections: " + ", ".join(text(i) for i in objections))
    if evidence:
        lines += ["**Why:** " + " · ".join(evidence)]
    if remainder:
        lines += [
            markup.details(
                "Operator note" if operator else "Further detail",
                markup.excerpt(remainder, 12000, "Full reason in the audit state."),
            )
        ]
    if continuation:
        lines += ["**What you can do:**\n\n" + continuation]
    return "\n\n".join(lines)


def ready(reasons, pr):
    """Whether only a human merge stands between the PR and its base."""
    return bool(reasons) and all(
        r in POLICY_REASONS or r == GITHUB_REASON and pr.get("mergeable") is True for r in reasons
    )


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
    conversation.queue(
        state,
        "✅ Handed off for merge",
        (
            f"Concurro's work on {link(f'PR #{number}', url)} is done. A human reviews and merges it.\n\n"
            f"{remaining}\n\nGitHub's own requirements, such as required reviews and checks, are shown on the PR."
        ),
    )
    return True


def questions(state, config):
    """The open questions that sent the task to clarification, each tagged with its provider."""
    found = []
    intake = state.get("qc", {}).get("intake")
    if intake and intake["decision"] == "clarify":
        found += [(assigned(config, "triage"), q) for q in intake["questions"]]
    if state.get("plan"):
        found += [(assigned(config, "planner"), q) for q in state["plan"]["questions"]]
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
        question = markup.excerpt(
            item["question"],
            max(1000, 16000 // len(asked)),
            "Full question in the report archive linked from the dashboard.",
        )
        lines += [f"**Q{index}.**{tag} {question}", ""]
    how = "Reply in a comment or edit the issue description; the author or any writer may answer."
    if state.get("mode") == "manual":
        how += f" Then a writer comments {code('/concurro start')}."
    lines.append(how)
    records.append(
        {"number": number, "questions": asked, "state": "open", "comment": None, "issue_digest": state["issue_digest"]}
    )
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
        reply = next(
            (
                c
                for c in sorted(comments, key=lambda c: c["id"])
                if c["id"] > record["comment"]
                and not c["body"].startswith(("<!-- concurro:", "/concurro "))
                and answerer(c["user"]["login"])
            ),
            None,
        )
        if reply:
            url = protocol.comment_url(config["repository"], state["issue"], reply["id"])
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
    """Apply durable comment edits; recover lost responses from the exact intended body."""
    for record in state.get("requests", []):
        intended = record.get("edit")
        if not intended or intended["state"] != "intended":
            continue
        current = github.repo(f"issues/comments/{record['comment']}")["body"]
        if current not in (intended["body"], record["body"]):
            raise Blocked(reason_guidance.REQUEST_CONFLICT)
        if current != intended["body"]:
            github.repo(f"issues/comments/{record['comment']}", "PATCH", {"body": intended["body"]})
        intended["state"] = "done"
        checkpoint()
    publish_blocked(state, github, checkpoint)
