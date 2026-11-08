"""PR review findings: a checkpoint-owned ledger that gives each finding one stable identity, and its
publication on the pull request as a native review with one thread per finding."""

import re
import sys

from github import Transient
import markup
from markup import block, code, link, table, text
from policy import Blocked, digest

# A closed finding's text is excerpted once its review event is published.
EXCERPT = 300
ELIDED = "… [Full text in the published issue conversation.]"


def ledger(state):
    """The task's findings, oldest first."""
    return state.setdefault("findings", [])


def owned(state, provider):
    """The provider's findings it must answer: open ones, and ones withdrawn by a design reopen."""
    return [f for f in ledger(state) if f["provider"] == provider and f["status"] in ("open", "withdrawn")]


def context(state, provider):
    """What the reviewer sees of its own findings."""
    return [{k: f[k] for k in ("id", "status", "path", "start_line", "line", "detail")} for f in owned(state, provider)]


def apply(state, provider, report, head, issue_url=None):
    """Record a validated report's dispositions and new findings; return the review sequence and new IDs.

    Deterministic for one report, so a retained model result applied again after a crash
    assigns the same identities.
    """
    sequence = state.get("review_sequence", 0) + 1
    state["review_sequence"] = sequence
    findings = {f["id"]: f for f in ledger(state)}
    for disposition in report["dispositions"]:
        finding = findings[disposition["id"]]
        finding.update(status=disposition["status"], answered=sequence)
    identifiers = []
    for item in report["findings"]:
        number = state.get("finding_sequence", 0) + 1
        state["finding_sequence"] = number
        identifier = f"F{number}"
        finding = {"id": identifier, "provider": provider, **item, "text_digest": digest(item["detail"]),
                   "head": head, "review": sequence, "status": "open", "format": FORMAT}
        if issue_url:
            finding['report_url'] = issue_url
        finding["opening_digest"] = digest(finding_body(finding))
        ledger(state).append(finding)
        identifiers.append(identifier)
    return sequence, identifiers


def withdraw(state):
    """A design reopen withdraws every open finding; its provider retires or re-adopts it under the new plan.

    Returns the withdrawn IDs.
    """
    withdrawn = []
    for finding in ledger(state):
        if finding["status"] == "open":
            finding["status"] = "withdrawn"
            withdrawn.append(finding["id"])
    return withdrawn


def open_findings(state):
    """Open findings for the implementer's repair context."""
    return [{k: f[k] for k in ("id", "provider", "path", "start_line", "line", "detail")}
            for f in ledger(state) if f["status"] == "open"]


def compact(state):
    """Excerpt the text of closed findings; called only once every queued event is published.

    The full text stays in the published review event that raised the finding, and its
    `text_digest` still identifies it.
    """
    for finding in state.get("findings", []):
        if (finding["status"] in ("fixed", "obsolete") and len(finding["detail"]) > EXCERPT
                and not finding["detail"].endswith(ELIDED)):
            finding["detail"] = finding["detail"][:EXCERPT] + ELIDED


def label(finding_id, item):
    """One finding as published: its ID, location and text; reports predating the ledger have neither IDs nor lines."""
    location = item.get("path") or "general"
    start, line = item.get("start_line"), item.get("line")
    if line is not None:
        location += f":{start}-{line}" if start not in (None, line) else f":{line}"
    prefix = f"**{finding_id}** " if finding_id else ""
    return f"{prefix}`{location}`: {item['detail']}"


def report_lines(report):
    """A report's new findings and dispositions as Markdown list items."""
    identifiers = report.get("finding_ids") or [None] * len(report["findings"])
    lines = ["- " + label(i, f) for i, f in zip(identifiers, report["findings"])]
    lines += [f"- **{d['id']}** {d['status']}: {d['evidence']}" for d in report.get("dispositions", [])]
    return lines


# Publication on the pull request: every write is a projection of checkpoint state, recovered by
# its marker and author before anything is sent again.

# Findings and outbox items created from now on publish in this format; one without `format` is format 1 forever.
FORMAT = 3
REVIEW_MARKER = "<!-- concurro:review:v1:"
FINDING_MARKER = "<!-- concurro:finding:v1:"
DISPOSITION_MARKER = "<!-- concurro:disposition:v1:"
WITHDRAWN_MARKER = "<!-- concurro:withdrawn:v1:"
# Failed attempts of one publication item before the task is blocked; a 422 may be spam prevention.
MAX_ATTEMPTS = 3
# GitHub rejects a review body over 65,536 characters.
REVIEW_BODY_LIMIT = 60000
KEPT_TEXT = 1000
OMITTED = "Full text omitted: it exceeds GitHub's review limit. It is kept in the automation audit state."
HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def shown_lines(patch):
    """The right-side line numbers each hunk of a unified `patch` actually shows, one set per hunk."""
    hunks, line = [], None
    for text in patch.split("\n"):
        header = HUNK.match(text)
        if header:
            hunks.append(set())
            line = int(header[1])
        elif hunks and text[:1] in (" ", "+"):
            hunks[-1].add(line)
            line += 1
        elif hunks and text == "":
            # An empty context line whose leading space was stripped.
            hunks[-1].add(line)
            line += 1
    return hunks


def placement(finding, patches, inline=True):
    """`inline`, `file` or `body` for one finding against the PR's files at the reviewed head."""
    path = finding["path"]
    if path is None or path not in patches:
        return "body"
    line = finding["line"]
    if inline and line is not None and patches[path]:
        wanted = set(range(finding["start_line"] or line, line + 1))
        if any(wanted <= hunk for hunk in shown_lines(patches[path])):
            return "inline"
    return "file"


def task_id(state, config):
    """The task's identity in markers."""
    return f"{config['repository']}#{state['issue']}"


def finding_prefix(finding):
    """A format 1 opening comment's text before the finding's detail."""
    return f"{FINDING_MARKER}{finding['id']} -->\n**{finding['id']}** · {finding['provider']}: "


def finding_body(finding):
    """The comment that opens a finding's thread, in the finding's format."""
    if finding.get("format", 1) == 1:
        return finding_prefix(finding) + finding["detail"]
    severity = markup.SEVERITY.get(finding.get("severity"))
    heading = " · ".join(["Concurro"] + ([severity[1]] if severity else []) + [finding["id"], markup.finding_title(finding)])
    prefix = f"{FINDING_MARKER}{finding['id']} -->\n{severity[0] + ' ' if severity else ''}**{heading}**\n\n"
    suffix = "\n\n" + markup.sub("Concurro review · " + text(finding["provider"]))
    detail = block(finding['detail'])
    if finding.get('format', 1) >= 3:
        note = "Full finding in the report archive linked from the issue dashboard."
        if finding.get('report_url'):
            note = link(note, finding['report_url'])
        detail = markup.excerpt(finding['detail'], REVIEW_BODY_LIMIT - len((prefix + suffix).encode()), note)
    body = prefix + detail + suffix
    return body


def opening_matches(finding, comment):
    """Whether a comment is the finding's recorded opening.

    By digest, since a closed finding's text is excerpted: of the detail in format 1, of the
    whole comment, recorded when the finding was, in format 2.
    """
    body = comment.get("body", "")
    if comment.get("path") != finding["path"] or comment.get("in_reply_to_id") is not None:
        return False
    if finding.get("format", 1) != 1:
        return digest(body) == finding["opening_digest"]
    prefix = finding_prefix(finding)
    return body.startswith(prefix) and digest(body[len(prefix):]) == finding["text_digest"]


def thread_url(state, config, finding):
    """A finding's opening comment on the PR."""
    return f"https://github.com/{config.get('upstream', config['repository'])}/pull/{state['pr']}#discussion_r{finding['comment']}"


def where(finding):
    """A finding's location as a table cell."""
    if finding["path"] is None:
        return "general"
    start, line = finding["start_line"], finding["line"]
    lines = "" if line is None else f":{start}-{line}" if start not in (None, line) else f":{line}"
    return code(finding["path"] + lines, cell=True)


class Droppable(str):
    """Review body text that goes, for one note, when the body would exceed GitHub's limit."""


def review_body(state, config, item, report, findings, placed):
    """The review's own body, in the item's format."""
    if item.get("format", 1) != 1:
        return review_body_v2(state, config, item, report, findings, placed)
    return review_body_v1(state, config, item, report, findings, placed)


def review_body_v2(state, config, item, report, findings, placed):
    """Verdict and headline, new findings by severity, answers to earlier ones; the rest collapsed."""
    emoji, outcome = markup.VERDICT[report["verdict"]]
    lines = [f"{REVIEW_MARKER}{task_id(state, config)}:{item['sequence']} -->",
             f"### {emoji} Concurro review · {text(item['provider'])} · {outcome}"]
    # The summary itself leads when it is one short sentence or has no headline to lead with.
    # Long model text is `Droppable`: it goes, for one note, when the body would exceed GitHub's limit.
    title = markup.headline(report, "summary")
    collapse = bool(title) and not markup.redundant(report, "summary")
    summary = block(report["summary"])
    lines += ["", title if collapse else Droppable(summary) if len(summary) > KEPT_TEXT else summary]
    order = list(markup.SEVERITY)
    ordered = sorted(findings, key=lambda f: (order.index(f["severity"]) if f.get("severity") in order else len(order),
                                              int(f["id"][1:])))
    if ordered:
        rows = [[markup.severity(f) or "—", f"**{text(f['id'])}** {markup.finding_title(f)}", where(f)] for f in ordered]
        lines += ["", table(["Severity", "Finding", "Where"], rows)]
    if report["dispositions"]:
        answers = []
        for disposition in report["dispositions"]:
            finding = find(state, disposition["id"])
            name = link(disposition["id"], thread_url(state, config, finding)) if finding.get("comment") else text(disposition["id"])
            answers.append(f"{name} {markup.label(markup.STATUS[disposition['status']])}")
        lines += ["", "**Earlier findings:** " + " · ".join(answers)]
    if ordered or any(d["status"] == "open" for d in report["dispositions"]):
        lines += ["", markup.BLOCKING_NOTE]
    if report["design_issue"]:
        issue = report["design_issue"]
        explanation = block(issue['explanation'])
        evidence = "\n".join("- " + text(value, 1000) for value in issue['evidence'])
        if item.get('format', 1) >= 3:
            explanation, evidence = Droppable(explanation), Droppable(evidence)
        lines += ["", f"**Design issue** · {text(issue['kind'])}", "", explanation, "", evidence]
    if collapse:
        lines += ["", Droppable(markup.details("Full review", summary))]
    unthreaded = [f for f in ordered if placed[f["id"]] == "body"]
    if unthreaded:
        lines += ["", Droppable(markup.details("Findings without a file", "\n\n".join(
            f"**{text(f['id'])} · {markup.finding_title(f)}**\n\n{block(f['detail'])}" for f in unthreaded)))]
    effort = (" / effort " + report["agent"]["effort"]) if report.get("agent") else ""
    lines += ["", markup.sub(text(f"{item['provider']} / {report['model']}" + effort), "head " + code(item["head"][:7]),
                             f"review {item['sequence']}")]
    body = "\n".join(str(line) for line in lines)
    if len(body) > REVIEW_BODY_LIMIT:
        body = "\n".join(OMITTED if isinstance(line, Droppable) else line for line in lines)
    return body


def review_body_v1(state, config, item, report, findings, placed):
    """The review's own body: verdict, summary, answers to earlier findings, and unthreaded findings."""
    lines = [f"{REVIEW_MARKER}{task_id(state, config)}:{item['sequence']} -->",
             f"### {item['provider']} / {report['model']} · {report['verdict'].replace('_', ' ')}", "", report["summary"]]
    if report["design_issue"]:
        issue = report["design_issue"]
        lines += ["", f"Design reopened ({issue['kind']}): {issue['explanation']}", ""]
        lines += ["- " + evidence for evidence in issue["evidence"]]
    threaded = [f["id"] for f in findings if placed[f["id"]] != "body"]
    if threaded:
        lines += ["", "New findings, in their threads: " + ", ".join(threaded)]
    unthreaded = [f for f in findings if placed[f["id"]] == "body"]
    if unthreaded:
        lines += ["", "New findings:", ""] + ["- " + label(f["id"], f) for f in unthreaded]
    if report["dispositions"]:
        lines += ["", "Earlier findings:", ""] + [f"- **{d['id']}** {d['status']}: {d['evidence']}"
                                                  for d in report["dispositions"]]
    return "\n".join(lines)


def disposition_body(state, item, disposition):
    """A reply answering one finding in its thread, in the item's format."""
    marker = f"{DISPOSITION_MARKER}{disposition['id']}:{item['sequence']} -->"
    if item.get("format", 1) != 1:
        emoji, status = markup.STATUS[disposition["status"]]
        location = {"fixed": "in " + code(item["head"][:7]), "open": "on " + code(item["head"][:7]),
                 "obsolete": "under plan " + code(state["plan_id"][:12])}[disposition["status"]]
        evidence = block(disposition['evidence'])
        if item.get('format', 1) >= 3:
            note = 'Full evidence in the report archive linked from the issue dashboard.'
            finding = find(state, disposition['id']) or {}
            if finding.get('report_url'):
                note = link(note, finding['report_url'])
            evidence = markup.excerpt(disposition['evidence'], REVIEW_BODY_LIMIT - 1000, note)
        return f"{marker}\n{emoji} **{status}** {location}\n\n{evidence}"
    answer = {"fixed": f"Fixed in `{item['head']}`", "open": f"Still open on `{item['head']}`",
              "obsolete": f"Obsolete under plan `{state['plan_id']}`"}[disposition["status"]]
    return f"{marker}\n{answer}: {disposition['evidence']}"


def queue_review(state, provider, report):
    """Queue a recorded report's publication on the PR.

    A design reopen is not kept among the head's reports, so its item carries the report.
    """
    item = {"kind": "review", "provider": provider, "sequence": report["sequence"],
            "head": report["head"], "attempts": 0, "inline": True, "format": FORMAT}
    if report["verdict"] == "reopen":
        item["report"] = report
    state.setdefault("pr_outbox", []).append(item)


def queue_withdrawal(state, identifiers):
    """Queue one notice in each withdrawn finding's thread."""
    threaded = [i for i in identifiers if (find(state, i) or {}).get("comment")]
    if threaded:
        state.setdefault("pr_outbox", []).append({"kind": "withdrawn", "findings": threaded,
                                                  "generation": state["design_generation"], "attempts": 0,
                                                  "format": FORMAT})


def find(state, identifier):
    """One ledger finding by ID, or None."""
    return next((f for f in state.get("findings", []) if f["id"] == identifier), None)


def report_for(state, item):
    """The recorded report a review item publishes, current or archived."""
    if "report" in item:
        return item["report"]
    for reports in [state.get("reviews", {})] + list(reversed(state.get("review_history", []))):
        report = reports.get(item["provider"])
        if report and report.get("sequence") == item["sequence"]:
            return report
    return None


class Publisher:
    """Publish the PR outbox through one GitHub client, checkpointing after each durable step."""

    def __init__(self, state, github, config, checkpoint):
        self.state, self.gh, self.config, self.checkpoint = state, github, config, checkpoint
        self.bot = config["bot_login"]
        self.pr = state["pr"]

    def ours(self, items, marker):
        """App-authored items whose body starts with `marker`; more than one is a conflict."""
        found = [i for i in items if i.get("user", {}).get("login") == self.bot and i.get("body", "").startswith(marker)]
        if len(found) > 1:
            raise Blocked("duplicate PR review publication; reconcile manually")
        return found[0] if found else None

    def comment(self, comments, marker, path, body, data):
        """Find an App comment by marker, verifying its content, or create it."""
        existing = self.ours(comments, marker)
        if existing:
            if existing["body"] != body:
                raise Blocked("PR review publication differs from its recorded content; reconcile manually")
            return existing
        created = self.gh.repo(path, "POST", data)
        comments.append({"id": created["id"], "user": {"login": self.bot}, "body": body, "path": data.get("path")})
        return created

    def opening(self, comments, finding):
        """A finding's verified opening comment; a missing one is retried, a changed one blocks."""
        comment = next((c for c in comments if c["id"] == finding["comment"]), None)
        if comment is None:
            raise RuntimeError("finding comment not visible")
        if not opening_matches(finding, comment):
            raise Blocked("PR review publication differs from its recorded content; reconcile manually")
        return comment

    def review(self, item):
        """Publish one recorded review: the review, file-level threads, replies and resolutions."""
        state = self.state
        report = report_for(state, item)
        if report is None:
            return
        patches = {}
        if report["verdict"] != "reopen":
            # Placement holds only for the head it was computed on; a newer head has its own review.
            # A design reopen has no findings to place and is published whatever the head.
            if self.gh.repo(f"pulls/{self.pr}")["head"]["sha"] != item["head"]:
                return
            patches = {f["filename"]: f.get("patch") for f in self.gh.pages(f"pulls/{self.pr}/files")}
            if self.gh.repo(f"pulls/{self.pr}")["head"]["sha"] != item["head"]:
                return
        findings = [find(state, i) for i in report["finding_ids"]]
        placed = {f["id"]: placement(f, patches, item["inline"]) for f in findings}
        comments = self.gh.pages(f"pulls/{self.pr}/comments")
        marker = f"{REVIEW_MARKER}{task_id(state, self.config)}:{item['sequence']} -->"
        body = review_body(state, self.config, item, report, findings, placed)
        existing = self.ours(self.gh.pages(f"pulls/{self.pr}/reviews"), marker)
        if existing and existing["body"] != body:
            raise Blocked("PR review publication differs from its recorded content; reconcile manually")
        if not existing:
            inline = []
            for f in findings:
                if placed[f["id"]] == "inline":
                    comment = {"path": f["path"], "line": f["line"], "side": "RIGHT", "body": finding_body(f)}
                    if f["start_line"] not in (None, f["line"]):
                        comment.update(start_line=f["start_line"], start_side="RIGHT")
                    inline.append(comment)
            self.gh.repo(f"pulls/{self.pr}/reviews", "POST",
                         {"commit_id": item["head"], "event": "COMMENT", "body": body, "comments": inline})
            comments = self.gh.pages(f"pulls/{self.pr}/comments")
        for f in findings:
            if placed[f["id"]] == "file":
                comment_body = finding_body(f)
                self.comment(comments, f"{FINDING_MARKER}{f['id']} -->", f"pulls/{self.pr}/comments", comment_body,
                             {"commit_id": item["head"], "path": f["path"], "subject_type": "file", "body": comment_body})
        self.map_threads(findings, comments, placed)
        self.checkpoint()
        for disposition in report["dispositions"]:
            finding = find(state, disposition["id"])
            if finding.get("comment"):
                self.opening(comments, finding)
                comment_body = disposition_body(state, item, disposition)
                self.comment(comments, comment_body.split("\n", 1)[0], f"pulls/{self.pr}/comments/{finding['comment']}/replies",
                             comment_body, {"body": comment_body})
        self.resolve([find(state, d["id"]) for d in report["dispositions"] if d["status"] in ("fixed", "obsolete")])

    def map_threads(self, findings, comments, placed):
        """Record each new finding's verified opening comment and thread; the item stays pending until all are visible."""
        threads = {t["comment"]: t["id"] for t in self.gh.review_threads(self.pr)}
        for finding in findings:
            finding.setdefault("resolution", "none")
            if placed[finding["id"]] == "body":
                finding.update(comment=None, thread="body")
                continue
            opening = self.ours(comments, f"{FINDING_MARKER}{finding['id']} -->")
            if opening is None or opening["id"] not in threads:
                raise RuntimeError("finding thread not visible")
            if not opening_matches(finding, opening):
                raise Blocked("PR review publication differs from its recorded content; reconcile manually")
            finding.update(comment=opening["id"], thread=threads[opening["id"]])

    def resolve(self, closed):
        """Resolve each closed finding's thread at most once; an unresolved thread after an attempt is left to a human."""
        threads = {t["id"]: t["isResolved"] for t in self.gh.review_threads(self.pr)}
        for finding in closed:
            thread = finding.get("thread")
            if not thread or thread == "body" or finding["status"] not in ("fixed", "obsolete"):
                continue
            resolution = finding.setdefault("resolution", "none")
            if resolution == "intended":
                finding["resolution"] = "done" if threads.get(thread) else "manual"
            elif resolution == "none":
                finding["resolution"] = "intended"
                self.checkpoint()
                finding["resolution"] = "done" if self.gh.resolve_thread(thread) else "manual"
            self.checkpoint()

    def withdrawn(self, item):
        """Tell each withdrawn finding's thread that the design changed; the thread stays unresolved."""
        comments = self.gh.pages(f"pulls/{self.pr}/comments")
        for identifier in item["findings"]:
            finding = find(self.state, identifier)
            self.opening(comments, finding)
            marker = f"{WITHDRAWN_MARKER}{identifier}:{item['generation']} -->"
            if item.get("format", 1) == 1:
                notice = (f"{marker}\nThe design was reopened. {finding['provider']} will re-assess this finding "
                          "under the new plan.")
            else:
                emoji, status = markup.STATUS["withdrawn"]
                notice = (f"{marker}\n{emoji} **{status}**: the design was reopened. {text(finding['provider'])} "
                          "will re-assess this finding under the new plan.")
            self.comment(comments, marker, f"pulls/{self.pr}/comments/{finding['comment']}/replies", notice,
                         {"body": notice})

    def run(self):
        """Publish pending items in order; return whether the outbox is now empty.

        A failed item stays pending and is retried on the next tick, its inline findings moved
        to file-level comments; after `MAX_ATTEMPTS` failures the task is blocked.
        """
        outbox = self.state.get("pr_outbox", [])
        while outbox:
            item = outbox[0]
            try:
                self.review(item) if item["kind"] == "review" else self.withdrawn(item)
            except (Blocked, Transient):
                raise
            except Exception:
                item["attempts"] += 1
                item["inline"] = False
                self.checkpoint()
                if item["attempts"] >= MAX_ATTEMPTS:
                    raise Blocked("PR review publication failed repeatedly; inspect the PR and its review threads")
                print("Concurro PR review publication failed; retained for retry.", file=sys.stderr)
                return False
            outbox.pop(0)
            self.checkpoint()
        return True


def publish(state, github, config, checkpoint):
    """Publish the PR outbox, if any; see `Publisher.run`."""
    if not state.get("pr_outbox") or not state.get("pr"):
        return True
    return Publisher(state, github, config, checkpoint).run()
