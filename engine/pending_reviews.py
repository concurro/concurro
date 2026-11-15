"""Recoverable publication of one report and its comments as a single pending review."""

import markup
import reason_guidance
import review_api
import reviews
from github import Rejected
from identities import Actor
from policy import Blocked

# The rejection reason of a pending review GitHub refused to submit.
SUBMISSION = "submission"


class Publication:
    """Publish an outbox item without exposing standalone comments or resubmitting uncertain writes."""

    def __init__(self, publisher, item):
        """Bind to the existing publisher's checkpoint and thread-verification contracts."""
        self.publisher, self.item = publisher, item
        self.state, self.gh, self.config = publisher.state, publisher.gh, publisher.config
        self.progress = item.setdefault("publication_state", {"phase": "building"})

    @property
    def actor(self):
        """The account recorded when this publication began; an older record is bound by login alone."""
        recorded = self.progress.get("actor")
        if recorded:
            return Actor.parse(recorded)
        return Actor(self.publisher.actor.login) if "head" in self.progress else self.publisher.actor

    def verify(self, node, body):
        """Require exact review ownership, task, commit, marker and frozen body."""
        pr = node.get("pullRequest") or {}
        return (
            self.actor.graphql(node.get("author"))
            and pr.get("number") == self.publisher.pr
            and pr.get("repository", {}).get("nameWithOwner", "").lower()
            == self.config.get("upstream", self.config["repository"]).lower()
            and node.get("commit", {}).get("oid") == self.progress["head"]
            and node.get("body") == body
        )

    def place(self, values):
        """One finding's thread, or None when GitHub definitely refused that placement."""
        try:
            return review_api.mutate(self.gh, "addPullRequestReviewThread", **values)
        except Rejected as error:
            # Recorded with the item's outcome if the whole review is later refused too.
            self.progress.setdefault("refused", []).append({"placement": values["subjectType"], "status": error.status})
            return None

    @staticmethod
    def submit(send):
        """Submit the pending review, naming a definite refusal so its replacement is rebuilt."""
        try:
            return send()
        except Rejected as error:
            error.reason = SUBMISSION
            raise

    def run(self):
        """Build, freeze, submit, then complete mapping and resolutions before acknowledging."""
        p, item, progress = self.publisher, self.item, self.progress
        withdrawn = item["kind"] != "review"
        report = None if withdrawn else reviews.report_for(self.state, item)
        if not withdrawn and report is None:
            return
        pr = self.gh.repo(f"pulls/{p.pr}")
        if pr.get("state", "open") != "open":
            raise Blocked(reason_guidance.CLOSED_BEFORE_REVIEW)
        sequence = f"withdrawn:{item['generation']}" if withdrawn else item["sequence"]
        marker = reviews.review_marker(self.state, self.config, sequence)
        if "head" not in progress:
            progress.update(head=pr["head"]["sha"] if withdrawn else item["head"], marker=marker)
            if p.actor.id is not None or p.actor.node is not None:
                progress["actor"] = p.actor.record()
            p.checkpoint()
        findings = [] if withdrawn else [reviews.find(self.state, i) for i in report["finding_ids"]]
        comments = self.gh.pages(f"pulls/{p.pr}/comments")
        if progress["phase"] == "submitting" and item.get("outcome") == {"status": "rejected", "reason": SUBMISSION}:
            # GitHub definitely refused the frozen review: drop it and build the replacement
            # rendering, whose placement the publisher has already stepped down.
            for key in ("review", "review_number", "body", "placed", "threads", "replies"):
                progress.pop(key, None)
            progress["phase"] = "building"
            p.checkpoint()
        if progress["phase"] == "building":
            existing_reviews = list(review_api.reviews(self.gh, p.pr))
            if sum((node.get("body") or "").startswith(marker) for node in existing_reviews) > 1:
                raise Blocked(reason_guidance.DUPLICATE_REVIEW)
            for node in existing_reviews:
                if (
                    self.actor.graphql(node.get("author"))
                    and node.get("state") == "PENDING"
                    and not (node.get("body") or "").startswith(marker)
                ):
                    raise Blocked(reason_guidance.UNIDENTIFIED_REVIEW)
            for existing in existing_reviews:
                same = (existing.get("body") or "").startswith(marker)
                if same:
                    expected = marker if existing["state"] == "PENDING" else progress.get("body")
                    if not expected or not self.verify(existing, expected):
                        raise Blocked(reviews.CONFLICT)
                    if existing["state"] == "COMMENTED":
                        progress.update(
                            phase="submitted", review=existing["id"], review_number=existing.get("databaseId")
                        )
                        p.checkpoint()
                        break
                    if existing["state"] != "PENDING":
                        raise Blocked(reviews.CONFLICT)
                    review_api.mutate(self.gh, "deletePullRequestReview", pullRequestReviewId=existing["id"])
            if progress["phase"] == "building":
                moved = not p.frozen and not withdrawn and report["verdict"] != "reopen"
                if moved and pr["head"]["sha"] != progress["head"]:
                    return
                patches = {} if withdrawn else p.patches(item, progress["head"])
                current = self.gh.repo(f"pulls/{p.pr}")
                if current.get("state", "open") != "open":
                    raise Blocked(reason_guidance.CLOSED_BEFORE_REVIEW_BUILD)
                if moved and current["head"]["sha"] != progress["head"]:
                    return
                node = review_api.mutate(
                    self.gh,
                    "addPullRequestReview",
                    pullRequestId=pr["node_id"],
                    commitOID=progress["head"],
                    body=marker,
                )
                if not self.verify(node, marker):
                    raise Blocked(reviews.CONFLICT)
                progress.update(
                    review=node["id"], review_number=node.get("databaseId"), placed={}, threads={}, replies=[]
                )
                p.checkpoint()
                for finding in findings:
                    placement = reviews.placement(finding, patches, item.get("inline", True), item.get("threads", True))
                    if placement != "body":
                        values = {
                            "pullRequestReviewId": node["id"],
                            "path": finding["path"],
                            "body": reviews.finding_body(finding),
                            "subjectType": "LINE" if placement == "inline" else "FILE",
                        }
                        if placement == "inline":
                            values.update(line=finding["line"], side="RIGHT")
                            if finding["start_line"] not in (None, finding["line"]):
                                values.update(startLine=finding["start_line"], startSide="RIGHT")
                        thread = self.place(values)
                        if thread is None and placement == "inline":
                            values = {
                                k: v for k, v in values.items() if k not in ("line", "side", "startLine", "startSide")
                            }
                            values["subjectType"] = "FILE"
                            placement = "file"
                            thread = self.place(values)
                        if thread is None:
                            placement = "body"
                        else:
                            progress["threads"][finding["id"]] = thread["id"]
                    progress["placed"][finding["id"]] = placement
                    p.checkpoint()
                replies = []
                if withdrawn:
                    for identifier in item["findings"]:
                        finding = reviews.find(self.state, identifier)
                        body = f"{reviews.WITHDRAWN_MARKER}{identifier}:{item['generation']} -->\n↩️ **Withdrawn**: the design was reopened. {markup.text(finding['provider'])} will re-assess this finding under the new plan."
                        replies.append((finding, body))
                else:
                    replies = [
                        (reviews.find(self.state, d["id"]), reviews.disposition_body(self.state, item, d))
                        for d in report["dispositions"]
                    ]
                for finding, body in replies:
                    if finding.get("comment"):
                        p.opening(comments, finding)
                        review_api.mutate(
                            self.gh,
                            "addPullRequestReviewThreadReply",
                            pullRequestReviewId=node["id"],
                            pullRequestReviewThreadId=finding["thread"],
                            body=body,
                        )
                        progress["replies"].append({"finding": finding["id"], "body": body})
                        p.checkpoint()
                body = (
                    marker + "\n### Design reopened\n\nEarlier findings will be reassessed under the new plan."
                    if withdrawn
                    else reviews.review_body(self.state, self.config, item, report, findings, progress["placed"])
                )
                progress.update(body=body, phase="submitting")
                p.checkpoint()
                # The first submission is sent only in this invocation, after freezing its intent.
                submitted = self.submit(
                    lambda: review_api.mutate(
                        self.gh, "submitPullRequestReview", pullRequestReviewId=node["id"], event="COMMENT", body=body
                    )
                )
                if not self.verify(submitted, body):
                    raise Blocked(reviews.CONFLICT)
                progress["phase"] = "submitted"
                p.checkpoint()
        if progress["phase"] == "submitting":
            node = review_api.node(self.gh, progress["review"])
            if node and node.get("state") == "PENDING" and self.verify(node, progress["marker"]):
                resume = getattr(self.gh, "resume_review_submission", None)
                if resume is not None:
                    node = self.submit(lambda: resume(progress["review"], progress["body"]))
            if not node or node.get("state") != "COMMENTED" or not self.verify(node, progress["body"]):
                raise Blocked(reason_guidance.UNCONFIRMED_REVIEW)
            progress["phase"] = "submitted"
            p.checkpoint()
        node = review_api.node(self.gh, progress["review"])
        if not node or node.get("state") != "COMMENTED" or not self.verify(node, progress["body"]):
            raise Blocked(reviews.CONFLICT)
        comments = self.gh.pages(f"pulls/{p.pr}/comments")
        for reply in progress["replies"]:
            finding = reviews.find(self.state, reply["finding"])
            p.opening(comments, finding)
            posted = p.ours(comments, reply["body"].split("\n", 1)[0])
            if posted is None:
                raise reviews.NotVisible("review reply not visible")
            if posted["body"] != reply["body"] or posted.get("in_reply_to_id") != finding["comment"]:
                raise Blocked(reviews.CONFLICT)
        if not withdrawn:
            p.map_threads(findings, comments, progress["placed"])
            p.checkpoint()
            p.resolve(
                [
                    reviews.find(self.state, d["id"])
                    for d in report["dispositions"]
                    if d["status"] in ("fixed", "obsolete")
                ]
            )
