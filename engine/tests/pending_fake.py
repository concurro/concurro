"""GitHub pending-review behavior for engine and service integration tests."""

from copy import deepcopy

import review_api


class PendingReviews:
    """Keep pending comments invisible until submission, preserving existing thread resolution."""

    def api(self, path, method="GET", data=None):
        """Answer the exact publication queries and mutations against in-memory GitHub state."""
        assert path == "graphql"
        if not hasattr(self, "pending_nodes"):
            self.pending_nodes = {}
        if data["query"] == review_api.NODE:
            return {"data": {"node": deepcopy(self.pending_nodes.get(data["variables"]["id"]))}}
        if data["query"] == review_api.LIST:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviews": {
                                "nodes": deepcopy(list(self.pending_nodes.values())),
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    }
                }
            }
        name, values = review_api.identify(data)
        if name == "addPullRequestReview":
            assert not any(n["state"] == "PENDING" for n in self.pending_nodes.values())
            self.counter += 1
            identifier = "R" + str(self.counter)
            node = {
                "id": identifier,
                "databaseId": self.counter,
                "body": values["body"],
                "state": "PENDING",
                "author": {"login": self.config["bot_login"].removesuffix("[bot]")},
                "commit": {"oid": values["commitOID"]},
                "pullRequest": {"number": 9, "state": "OPEN", "repository": {"nameWithOwner": self.repository}},
                "drafts": [],
            }
            self.pending_nodes[identifier] = node
            return {"data": {name: {"pullRequestReview": deepcopy(node)}}}
        node = self.pending_nodes[values["pullRequestReviewId"]]
        assert node["state"] == "PENDING"
        if name == "deletePullRequestReview":
            del self.pending_nodes[node["id"]]
            return {"data": {name: {"pullRequestReview": {"id": node["id"]}}}}
        if name == "addPullRequestReviewThread":
            if getattr(self, "null_thread", False) and values["subjectType"] == "LINE":
                return {"data": {name: {"thread": None}}}
            node["drafts"].append(("thread", deepcopy(values)))
            return {"data": {name: {"thread": {"id": "P" + str(len(node["drafts"]))}}}}
        if name == "addPullRequestReviewThreadReply":
            node["drafts"].append(("reply", deepcopy(values)))
            return {"data": {name: {"comment": {"id": "P" + str(len(node["drafts"]))}}}}
        assert name == "submitPullRequestReview"
        node.update(body=values["body"], state="COMMENTED")
        for kind, comment in node["drafts"]:
            if kind == "thread":
                fields = {
                    "path": comment["path"],
                    "subject_type": "file" if comment["subjectType"] == "FILE" else "line",
                    "line": comment.get("line"),
                }
                posted = self.review_comment(comment, **fields)
                self.thread(posted)
            else:
                thread = next(t for t in self.thread_list if t["id"] == comment["pullRequestReviewThreadId"])
                self.review_comment(comment, in_reply_to_id=thread["comments"]["nodes"][0]["databaseId"])
        self.app_reviews.append(
            {
                "id": node["databaseId"],
                "body": node["body"],
                "commit_id": node["commit"]["oid"],
                "event": "COMMENT",
                "user": {"login": self.config["bot_login"]},
            }
        )
        return {"data": {name: {"pullRequestReview": deepcopy(node)}}}
