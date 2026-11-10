"""Operator-triggered reconciliation of one journaled pending-review mutation."""

import review_api
from controller import Outcome


def reconcile(gateway, intent, operator):
    """Confirm publication or compensate a pending edit without clearing uncertainty first."""
    registry = gateway.registry
    record, context = registry.mutation(intent), registry.review_intent(intent)
    if not record or record.status != "unknown" or not context or not isinstance(operator, str) or not operator.strip():
        raise ValueError("not an unresolved review mutation")
    if context["kind"] not in (
        "addPullRequestReviewThread",
        "addPullRequestReviewThreadReply",
        "submitPullRequestReview",
    ):
        raise ValueError("this review mutation requires manual resolution")
    key = record.key
    with gateway.section(key):
        owner = registry.current(key)
        if owner and owner.live and owner.lease_until > registry.clock():
            raise ValueError("task still has a live owner")
        if registry.in_flight(key) or any(r.intent != intent for r in registry.unresolved(key)):
            raise ValueError("another write needs resolution first")
        generation = registry.grant(key, "review-recovery:" + operator, 900, consume=False)
    try:
        child = registry.mutation(context["recovery"]) if context.get("recovery") else None
        if child:
            if child.status != "done":
                raise ValueError("compensating write requires manual resolution")
            registry.resolve(intent, "discarded", "confirmed review deletion: " + child.intent, operator)
            return {"intent": intent, "disposition": "discarded"}
        node = gateway._graphql_node(context["repository"], review_api.NODE, context["review"])
        pr = node.get("pullRequest") or {}
        author = node.get("author", {}).get("login")
        if (
            node.get("id") != context["review"]
            or author not in (gateway.bot_login, (gateway.bot_login or "").removesuffix("[bot]"))
            or pr.get("number") != context["pull"]
            or pr.get("repository", {}).get("nameWithOwner", "").lower() != context["repository"].lower()
            or node.get("commit", {}).get("oid") != context["head"]
            or not gateway.owned_pull(key, context["pull"])
        ):
            raise ValueError("review identity cannot be confirmed")
        if context["kind"] == "submitPullRequestReview":
            expected = context["request"]["variables"]["input"]["body"]
            if node.get("state") != "COMMENTED" or node.get("body") != expected:
                raise ValueError("submission remains unconfirmed; same-ID retry awaits live validation")
            registry.resolve(intent, "applied", "verified submitted review: " + node["id"], operator)
            return {"intent": intent, "disposition": "applied"}
        if node.get("state") != "PENDING" or node.get("body") != context["pending_body"]:
            raise ValueError("pending review content or state changed")
        body = review_api.request("deletePullRequestReview", {"pullRequestReviewId": node["id"]})
        gateway.mutate(key, generation, "POST", "graphql", body, recovery_of=intent)
        child = registry.review_intent(intent)["recovery"]
        registry.resolve(intent, "discarded", "confirmed review deletion: " + child, operator)
        return {"intent": intent, "disposition": "discarded"}
    finally:
        gateway.release(key, generation, Outcome("wait-until", False, 30))
