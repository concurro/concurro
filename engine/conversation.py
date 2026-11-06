"""Publish a bounded, recoverable outbox as a chronological issue conversation."""

from datetime import datetime, timezone
import json
import re
from typing import NamedTuple

from policy import Blocked, digest
from checkpoints import CheckpointError
from github import Transient
from identities import Identities
import markup
from markup import block, code, details as collapsed, link, table, text
import actions
import classification
import reviews

MARKER = "<!-- concurro:event:v1:"
# A sourced event without `format` is format 1 forever; format 2 posted reports as issue comments.
# Reports queued from now on are format 3: each is archived in its own write-once artifact.
FORMAT = 3
ARCHIVE_LIMIT = 60000
ARCHIVED = "… [Full text in its report archive.]"
# GitHub's artifact name rules: letters, numbers, spaces, periods, hyphens and underscores, starting and
# ending with a letter or number; 255 characters were measured as accepted.
NAME_UNSAFE = re.compile(r"[^A-Za-z0-9 ._-]")
ARTIFACT_NAME_LIMIT = 100
# The status comment's rounds table keeps this many archived reports.
REPORT_INDEX = 40
EVENT_LIMIT = 24000
# Room for an event's heading, marker and metadata line.
EVENT_RESERVE = 1000
# Expanded model text up to this size stays when an event is shortened.
KEPT_TEXT = 1000
OMITTED = "Full text omitted: it exceeds the comment limit. It is kept in the automation audit state."


class PublicationError(CheckpointError):
    """A fixed diagnostic for delivery failure, separate from checkpoint integrity."""


def render(state, kind, source, details):
    """Render full public text from one checkpoint-owned source without duplicating it."""
    value = state
    for key in source:
        value = value[key]
    if kind == 'plan_summary':
        publication = value['publication']
        return '\n'.join([
            f"[Implementation plan]({details['url']}) · artifact version {details['revision']}",
            f"Plan `{details['plan']}` · round {details['round']}", '', 'Changes:', '',
            *['- ' + text for text in publication['changes']], '', 'Review focus:', '',
            *['- ' + text for text in publication['review_focus']]])
    if kind == "plan":
        lines = [f"Plan `{details['plan']}` · round {details['round']}", "", value["summary"], "",
                 *["- " + step for step in value["steps"]], "", "Validation:", "",
                 *["- " + test for test in value["tests"]]]
        if value["responses"]:
            lines += ["", "Responses to review:", "",
                      *[f"- **{v['id']}**: {v['response']}" for v in value["responses"]]]
        if value["questions"]:
            lines += ["", "Questions:", "", *["- " + q for q in value["questions"]]]
        return "\n".join(lines)
    if kind == "qc":
        minimal = {"plan_id": details["plan"], "design_generation": details["round"] - 1,
                   "objections": [{"id": identifier, "reviewer": details["reviewer"], "resolved": False}
                                  for identifier in details["resolved"]]}
        disposition_ids = details.get('disposition_ids', [])
        concern_ids = [identifier for identifier, update in zip(disposition_ids, value.get('objection_updates', []))
                       if update['status'] == 'open']
        body = review_body(minimal, value, details["phase"], {"id": details["reviewer"]}, concern_ids)
        if value.get('objection_updates'):
            body += '\n\nObjection dispositions:\n' + ''.join(
                f"\n- **{disposition_ids[i] if disposition_ids else u['id'] or 'New finding'}**: {u['status']} — {u['evidence']}"
                + (f" Counterexample: {u['counterexample']} Closure: {u['closure']}" if u['status'] == 'open' else '')
                for i, u in enumerate(value['objection_updates']))
        return body
    if kind == "local":
        checks = ", ".join(f"{c['name']}: exit {c['exit_code']}" for c in value["checks"])
        body = f"Attempt {value['attempt'] + 1} · commit `{value.get('local_commit', 'pending')}`\n\n{checks}"
        if value.get("review"):
            report = value["review"]
            body += f"\n\n{details['reviewer']}: **{report['verdict']}** — {report['summary']}"
            body += "".join("\n\n- " + f["path"] + ": " + f["detail"] for f in report["findings"])
        return body
    if kind == "review":
        body = f"Commit `{details['head']}` · round {details['round']}\n\n{value['summary']}"
        if "finding_ids" not in value:
            # Reports from before the finding ledger keep their exact rendering, so queued and
            # already-posted events still match their recorded digests.
            body += "".join("\n\n- " + f["path"] + ": " + f["detail"] for f in value["findings"])
        elif "pr" in details:
            # The review itself, with its findings in threads, is published on the PR.
            new = ", ".join(value["finding_ids"]) or "none"
            answered = ", ".join(f"{d['id']} {d['status']}" for d in value["dispositions"]) or "none"
            body += f"\n\nPosted as a review on PR #{details['pr']}. New findings: {new}. Earlier findings: {answered}."
        else:
            lines = reviews.report_lines(value)
            body += "\n\n" + "\n".join(lines) if lines else ""
        if value["design_issue"]:
            body += "\n\n" + value["design_issue"]["explanation"]
        return body
    raise ValueError("unknown conversation renderer")


def source_value(state, source):
    """The checkpoint value an event renders from."""
    value = state
    for key in source:
        value = value[key]
    return value


def objections(value, details):
    """A QC report's objection IDs: those its approval resolves, and its concerns and questions."""
    prefix = f"D{details['round']}-{details['reviewer'][:8]}-{details['phase']}"
    resolved = list(details["resolved"]) if value["decision"] == "approve" else []
    updates = zip(details.get("disposition_ids", []), value.get("objection_updates", []))
    concern_ids = [identifier for identifier, update in updates if update["status"] == "open"]
    concerns = [(concern_ids[i] if concern_ids else f"{prefix}-c{i + 1}", t) for i, t in enumerate(value["concerns"])]
    questions = [(f"{prefix}-q{i + 1}", t) for i, t in enumerate(value["questions"])]
    return resolved, concerns, questions


def items(entries):
    """Model text entries as plain-text list items."""
    return "\n".join("- " + text(entry, 1000) for entry in entries)


class Hidden(NamedTuple):
    """A collapsed section of an event, dropped first when the event would exceed its limit."""
    caption: str
    body: str


class Text(NamedTuple):
    """Model text shown expanded, dropped with the collapsed sections when long and the event would not fit."""
    body: str


def droppable(part):
    """Whether a part goes when its event would exceed the limit."""
    return isinstance(part, Hidden) or isinstance(part, Text) and len(part.body.encode()) > KEPT_TEXT


def join(parts, compact):
    """An event body from its parts; `compact` replaces droppable parts with one note."""
    if compact:
        parts = [p for p in parts if not droppable(p)] + [OMITTED]
    return "\n\n".join(collapsed(p.caption, p.body) if isinstance(p, Hidden) else p.body if isinstance(p, Text) else p
                       for p in parts if p)


def render_v2(state, kind, source, details):
    """`(title, body, meta)` of one sourced event in format 2; long model text goes when it would not fit."""
    title, parts, meta = parts_v2(state, kind, source, details)
    body = join(parts, False)
    if len(body.encode()) > EVENT_LIMIT - EVENT_RESERVE and any(droppable(p) for p in parts):
        body = join(parts, True)
    return title, body, meta


def lead(report, field):
    """`(lead, collapsed)`: a report's headline and its main text to collapse under it, or the text itself."""
    title = markup.headline(report, field)
    if title and not markup.redundant(report, field):
        return title, block(report[field])
    return Text(block(report[field])), None


def parts_v2(state, kind, source, details):
    """`(title, parts, meta)` of one sourced event: outcome first, the rest in `Hidden` sections."""
    value = source_value(state, source)
    plan = ("plan " + code(details["plan"][:12])) if details.get("plan") not in (None, "intake") else ""
    if kind == "intake_rejection":
        return "⚠️ Intake output rejected", [block(value["reason"])], (details["round"],)
    if kind == "implementation":
        return "Implementation report", [block(value["summary"]),
                "Agent-reported validation: " + block(value["validation"])], (f"round {details['round']}",)
    if kind == "plan_summary":
        publication = value["publication"]
        # The plan itself is linked, so its summary's headline is enough here.
        first, _ = lead(value, "summary")
        lines = [first, link("Implementation plan", details["url"]) + f" · artifact version {details['revision']}"]
        lines.append(Hidden("Changes and review focus", "**Changes**\n\n" + items(publication["changes"])
                            + "\n\n**Review focus**\n\n" + items(publication["review_focus"])))
        return f"Plan updated · {text(details['actor'])}", lines, (plan, f"round {details['round']}")
    if kind == "qc":
        emoji, outcome = markup.DECISION[value["decision"]]
        stage = "Intake QC" if details["phase"] == "intake" else "Plan QC"
        resolved, concerns, questions = objections(value, details)
        ids = details.get("disposition_ids", [])
        updates = [(ids[i] if ids else u["id"] or "New finding", u) for i, u in enumerate(value.get("objection_updates", []))]
        updated = {i for i, _ in updates}
        rows = [[code(i, cell=True), "Objection", markup.label(markup.STATUS[u["status"]])] for i, u in updates]
        rows += [[code(i, cell=True), "Concern", markup.label(markup.STATUS["open"])] for i, _ in concerns if i not in updated]
        rows += [[code(i, cell=True), "Question", markup.label(markup.STATUS["open"])] for i, _ in questions]
        rows += [[code(i, cell=True), "Objection", markup.label(markup.STATUS["resolved"])] for i in resolved if i not in updated]
        first, full = lead(value, "rationale")
        parts = [first]
        if rows:
            parts.append(table(["Objection", "Kind", "Status"], rows))
        hidden = [full] if full else []
        hidden += [f"**{text(i)}** · Concern\n\n{block(t)}" for i, t in concerns]
        hidden += [f"**{text(i)}** · Question\n\n{block(t)}" for i, t in questions]
        for identifier, update in updates:
            entry = f"**{text(identifier)}** · {markup.label(markup.STATUS[update['status']])}\n\n{block(update['evidence'])}"
            if update["status"] == "open":
                entry += f"\n\nCounterexample: {text(update['counterexample'], 2000)}\n\nClosure: {text(update['closure'], 2000)}"
            hidden.append(entry)
        if hidden:
            parts.append(Hidden("Rationale and objections", "\n\n".join(hidden)))
        title = f"{emoji} {stage} · {text(details['provider'])} · {outcome}"
        phase = "" if details["phase"] == "intake" else details["phase"]
        return title, parts, (text(details["model"]), plan, f"round {details['round']}", phase)
    if kind == "local":
        rows = [[code(c["name"], cell=True), markup.label(markup.PASS if c["exit_code"] == 0 else markup.FAIL),
                 str(c["exit_code"])] for c in value["checks"]]
        parts = [table(["Check", "Result", "Exit"], rows)] if rows else []
        report = value.get("review")
        if report:
            emoji, outcome = markup.VERDICT[report["verdict"]]
            parts.append(f"**{emoji} Local review · {text(details['reviewer'])} · {outcome}**")
            first, full = lead(report, "summary")
            hidden = ([full] if full else []) + [f"**{code(f['path'])}**\n\n{block(f['detail'])}" for f in report["findings"]]
            parts.append(first)
            if hidden:
                parts.append(Hidden("Local review", "\n\n".join(hidden)))
        commit = value.get("local_commit")
        return (f"Local validation · attempt {value['attempt'] + 1}", parts,
                ("commit " + code(commit[:7]) if commit else "",))
    if kind == "review":
        emoji, outcome = markup.VERDICT[value["verdict"]]
        first, full = lead(value, "summary")
        parts = [first]
        new = [reviews.find(state, i) or {"id": i, **f} for i, f in zip(value["finding_ids"], value["findings"])]
        severities = [f.get("severity") for f in new]
        summary = f"{len(new)} new finding{'s' if len(new) != 1 else ''}"
        breakdown = ", ".join(f"{severities.count(key)} {markup.label(entry)}"
                              for key, entry in markup.SEVERITY.items() if key in severities)
        line = [link(f"Review on PR #{details['pr']}", details["pr_url"]), summary + (f" ({breakdown})" if breakdown else "")]
        if value["dispositions"]:
            line.append("earlier: " + ", ".join(f"{text(d['id'])} {markup.label(markup.STATUS[d['status']])}"
                                                for d in value["dispositions"]))
        parts.append(" · ".join(line))
        if value["design_issue"]:
            parts += ["**Design issue:**", Text(block(value["design_issue"]["explanation"]))]
        if full:
            parts.append(Hidden("Full review", full))
        title = f"{emoji} Code review · {text(details['provider'])} · {outcome}"
        return title, parts, (text(details["model"]), "head " + code(details["head"][:7]), f"round {details['round']}")
    raise ValueError("unknown conversation renderer")


def archive_name(event):
    """An archive artifact's name, from engine values only, within GitHub's name rules."""
    index = event["index"]
    name = f"Concurro report {event['sequence']} - {index['step']} - {index['provider']} - round {index['round']}"
    return NAME_UNSAFE.sub("-", name)[:ARTIFACT_NAME_LIMIT].strip(" ._-")


def content(event, body):
    """Build the exact immutable text used for publication and reconciliation (format 1)."""
    return (f"{MARKER}{event['id']} -->\n### {event['title']}\n\n"
            f"Event {event['sequence']} · {event['at']}\n\n{body}")


def content_v2(event, title, body, meta):
    """Format 2: heading, outcome-first body, then one metadata line."""
    return f"{MARKER}{event['id']} -->\n### {title}\n\n{body}\n\n" + markup.sub(f"Event {event['sequence']}", *meta)


def archive(event, state):
    """Format 3: a report's archive, its heading and headline, then the report verbatim as JSON."""
    title, _, meta = render_v2(state, event["kind"], event["source"], event["details"])
    if event["details"].get("agent"):
        agent = event["details"]["agent"]
        meta = (*meta, text(f"{agent['profile']} / {agent['provider']} / {agent['model']} / effort {agent['effort']}"))
    value = source_value(state, event["source"])
    report = None if event["kind"] == "intake_rejection" else (
        value.get("review") if event["kind"] == "local" else value)
    headline = markup.headline(report, "rationale" if event["kind"] == "qc" else "summary") if report else None
    record = (value["output"] if event["kind"] == "intake_rejection"
              else json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", record)), default=0) + 1)
    if event["kind"] == "intake_rejection":
        fence = min((char * max(3, max((len(run) for run in re.findall(char + r"+", record)), default=0) + 1)
                     for char in ("`", "~")), key=len)
    lines = [f"{MARKER}{event['id']} -->", f"### {title}", ""]
    if event["kind"] == "intake_rejection":
        lines += [block(value["reason"]), "", "Rejected output — not an accepted intake decision.", ""]
        if value["truncated"]:
            lines += ["Output excerpted to fit publication limits.", ""]
    if headline:
        lines += [headline, ""]
    lines += [markup.sub(f"Report {event['sequence']}", *meta), "", fence + "json", record, fence]
    return "\n".join(lines)


def index_entry(state, kind, source, details):
    """The status comment's record of one archived report: step, round, provider and outcome."""
    value = source_value(state, source)
    if kind == "intake_rejection":
        return {"step": "Intake", "provider": details["provider"], "round": details["round"], "outcome": "invalid"}
    if kind == "implementation":
        return {"step": "Implement", "provider": details["provider"], "round": str(details["round"]),
                "outcome": "candidate published"}
    if kind == "qc":
        intake = details["phase"] == "intake"
        return {"step": "Intake" if intake else "Design review", "provider": details["provider"],
                "round": str(details["round"]) + ("" if intake else " " + details["phase"]),
                "outcome": value["decision"]}
    if kind == "local":
        return {"step": "Implement", "provider": details["reviewer"], "round": f"attempt {value['attempt'] + 1}",
                "outcome": value["review"]["verdict"]}
    return {"step": "Code review", "provider": details["provider"], "round": str(details["round"]),
            "outcome": value["verdict"]}


def queue(state, title, body=None, *, source=None, kind=None, details=None, meta=(), request=None):
    """Persist a bounded event descriptor; large reports stay in their existing state fields.

    A sourced event is a report, archived in its own artifact; `title` and `body` are for
    an issue comment.
    """
    journal = state.setdefault("conversation", {"sequence": 0, "pending": []})
    if len(journal["pending"]) >= 8:
        raise Blocked("conversation event exceeds publication bounds")
    journal["sequence"] += 1
    sequence = journal["sequence"]
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if source is not None:
        title, _, _ = render_v2(state, kind, source, details)
        body = json.dumps(source_value(state, source), sort_keys=True)
    identifier = digest([state["issue"], sequence, title, body, timestamp])
    event = {"id": identifier, "title": title, "sequence": sequence, "at": timestamp}
    if source is None:
        rendered, limit = content_v2(event, title, body, meta), EVENT_LIMIT
        event = {"id": identifier, "body": rendered}
        if request is not None:
            event["request"] = request
    else:
        event.update(source=list(source), kind=kind, details=details, format=FORMAT,
                     index=index_entry(state, kind, source, details))
        assessment = source_value(state, source).get("classification", {}).get("source", {}).get("report")
        if assessment:
            event["index"]["assessment"] = assessment
        rendered, limit = archive(event, state), ARCHIVE_LIMIT
        event["content_digest"] = digest(rendered)
    if len(rendered.encode()) > limit:
        journal["sequence"] -= 1
        raise Blocked("conversation event exceeds publication bounds")
    journal["pending"].append(event)


def queue_intake_concerns(state, concerns):
    """Notify humans of advisory intake concerns through the recoverable comment outbox."""
    lines = ["Intake approved moving forward to planning with these advisory concerns:", ""]
    used = len("\n".join(lines).encode())
    shortened = False
    for concern in concerns:
        rendered = text(concern, 1000)
        line = "- " + rendered
        if used + len(line.encode()) + 1 > EVENT_LIMIT - EVENT_RESERVE - 500:
            shortened = True
            break
        shortened |= len(markup.one_line(concern)) > 1000
        lines.append(line)
        used += len(line.encode()) + 1
    lines += ["", "These are planning and review considerations, not a request for human action.", "",
              ("Some text is shortened. " if shortened else "")
              + "The full concerns are in the intake report linked from the dashboard."]
    queue(state, "Intake approved with advisory concerns", "\n".join(lines))


def event_body(state, event):
    """Reject changed sources before acknowledging or posting a queued event."""
    if "body" in event:
        return event["body"]
    try:
        if event.get("format", 1) == 1:
            body = content(event, render(state, event["kind"], event["source"], event["details"]))
        elif event["format"] == 2:
            body = content_v2(event, *render_v2(state, event["kind"], event["source"], event["details"]))
        else:
            body = archive(event, state)
        if digest(body) == event["content_digest"]:
            return body
    except (KeyError, TypeError, ValueError):
        pass
    raise PublicationError("conversation_conflict")


def publish(state, github, config):
    """Reconcile lost POST responses by exact App-authored marker and content."""
    journal = state.get("conversation", {})
    pending = journal.get("pending", [])
    if not pending:
        return False
    try:
        comments = github.pages(f"issues/{state['issue']}/comments")
        identities = Identities(config)
        artifacts = None
        for event in pending:
            body = event_body(state, event)
            marker = MARKER + event["id"] + " -->"
            if event.get("format", 1) >= 3:
                # Archives are artifacts, authored like the audit state.
                if artifacts is None:
                    artifacts = github.pages(f"issues/{state['issue']}/artifacts")
                author = identities.author("state")
                items = artifacts
                create = lambda: github.repo(f"issues/{state['issue']}/artifacts", "POST", {
                    "type": "generic", "name": archive_name(event), "body": body})
                login = lambda item: item.get("creator", {}).get("login")
            else:
                # Recovery trusts only the one identity expected to author this event.
                author = identities.author("conversation", event.get("role"))
                items = comments
                create = lambda: github.repo(f"issues/{state['issue']}/comments", "POST", {"body": body})
                login = lambda item: item.get("user", {}).get("login")
            matches = [c for c in items if login(c) == author and c.get("body", "").startswith(marker)]
            if len(matches) > 1 or matches and matches[0]["body"] != body:
                raise PublicationError("conversation_conflict")
            published = matches[0] if matches else create()
            if "source" not in event:
                journal["latest"] = published["id"]
                actions.blocked_posted(state, published["id"], body)
                if event.get("request") is not None:
                    actions.posted(state, event["request"], published["id"], body)
                continue
            source = source_value(state, event["source"])
            receipt = {"content_digest": event["content_digest"], "source_digest": digest(source)}
            if isinstance(source, dict) and "classification" in source:
                receipt["core_digest"] = digest(classification.core(source))
            if event.get("format", 1) >= 3:
                receipt["artifact"] = published["number"]
                index = state.setdefault("reports", [])
                index.append({**event["index"], "artifact": published["number"]})
                del index[:-REPORT_INDEX]
            else:
                receipt["comment"] = published["id"]
                journal["latest"] = published["id"]
            journal.setdefault("receipts", {})["/".join(event["source"])] = receipt
    except (PublicationError, Transient):
        raise
    except Exception:
        raise PublicationError("conversation_publish") from None

    journal["pending"] = []
    return True


def receipt_matches(report, receipts):
    """Recognize published core text after advisory edits, retaining legacy full-digest matching."""
    full, core = digest(report), digest(classification.core(report))
    return any(full == receipt.get("source_digest") or core == receipt.get("core_digest")
               for receipt in receipts)


def compact(state):
    """Retain operative evidence and compact only published narrative text.

    Waits for the PR outbox too, since a pending PR review renders its report's full text.
    """
    state.pop("cooperative_votes", None)
    if "objections" in state:
        state["objections"] = [v for v in state["objections"] if not v["resolved"]]
    journal = state.get("conversation", {})
    if journal.get("pending") or state.get("pr_outbox") or not journal.get("receipts"):
        return
    reviews.compact(state)
    state.pop("review_event", None)
    published = list(journal["receipts"].values())
    archived = [receipt for receipt in published if "artifact" in receipt]
    reports = list(state.get("qc", {}).values()) + list(state.get("reviews", {}).values())
    reports += [v["report"] for v in state.get("design_votes", {}).values()]
    notes = {id(r): ARCHIVED if receipt_matches(r, archived) else None for r in reports}
    reports = [r for r in reports if receipt_matches(r, published)]
    for key in ("local_status", "local_evidence"):
        if (s := state.get(key, {})).get("review") and receipt_matches(s, published):
            reports.append(s["review"])
            notes[id(s["review"])] = ARCHIVED if receipt_matches(s, archived) else None
    for report in reports:
        for key in ("rationale", "summary"):
            text = report.get(key, "")
            size = len(json.dumps(text).encode())
            if size > 1000:
                excerpt = text[:max(1, len(text) * 800 // size)]
                while len(json.dumps(excerpt).encode()) > 900:
                    excerpt = excerpt[:len(excerpt) // 2]
                report[key] = excerpt + (notes.get(id(report)) or "… [Full text in the published issue conversation.]")
                if state.get("reason") == text:
                    state["reason"] = report[key]


def review_body(state, report, phase, member, concern_ids=()):
    """Render attributed judgments and stable objection identifiers for one plan."""
    plan = state.get("plan_id", "intake")
    round_number = state.get("design_generation", 0) + 1
    prefix = f"D{round_number}-{member['id'][:8]}-{phase}"
    lines = [f"Plan `{plan}` · round {round_number} · {phase}", "", report["rationale"]]
    if report["decision"] == "approve":
        resolved = [v["id"] for v in state.get("objections", [])
                    if v["reviewer"] == member["id"] and not v["resolved"]]
        if resolved:
            lines += ["", "This approval closes earlier objections: " + ", ".join(resolved) + "."]
    for key in ("concerns", "questions"):
        for index, text in enumerate(report[key], 1):
            identifier = concern_ids[index - 1] if key == 'concerns' and concern_ids else f"{prefix}-{key[0]}{index}"
            lines += ["", f"- **{identifier}**: {text}"]
    return "\n".join(lines)
