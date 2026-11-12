"""Evidence that a delivered comment change has reached GitHub's readable state."""

from datetime import datetime

from policy import digest


def expected(comment, action="created", previous=None):
    """Retain identity and digests, including nonce-bearing bodies after an uncertain create."""
    return {
        "id": comment.get("id"),
        "body": digest(comment.get("body") or ""),
        "action": action,
        "updated_at": comment.get("updated_at"),
        "previous": digest(previous) if previous is not None else None,
    }


def newer(observed, wanted):
    """Require a strictly newer GitHub timestamp before declaring an edit superseded."""
    try:
        return datetime.fromisoformat(observed) > datetime.fromisoformat(wanted)
    except (TypeError, ValueError):
        return False


def satisfied(expectations, comments):
    """Confirm each change or a visible later change, never treating a missing create as success."""
    done = set()
    for index, item in enumerate(expectations):
        matches = [comment for comment in comments if item["id"] is not None and comment["id"] == item["id"]]
        if item["action"] == "deleted":
            if item["id"] is not None and not matches:
                done.add(index)
        elif (
            any(
                digest(comment.get("body") or "") == item["body"]
                for comment in (matches if item["id"] is not None else comments)
            )
            or matches
            and newer(matches[0].get("updated_at"), item["updated_at"])
        ):
            done.add(index)
    # GitHub timestamps have second precision. Prior-body evidence also orders edits within one second.
    while True:
        before = len(done)
        for index, item in enumerate(expectations):
            for later in (expectations[i] for i in list(done)):
                same = item["id"] is not None and item["id"] == later["id"]
                follows = later["previous"] == item["body"]
                # An ID-less create (lost POST reply) is the identified comment with its exact,
                # nonce-bearing body, so that comment's resolved create, edit or deletion settles it.
                identified = item["id"] is None and later["id"] is not None and later["body"] == item["body"]
                if item["action"] != "deleted" and (
                    same
                    and (later["action"] == "deleted" or item["action"] == "created" and later["action"] == "edited")
                    or (same or item["id"] is None)
                    and follows
                    or item["action"] == "created"
                    and identified
                ):
                    done.add(index)
        if len(done) == before:
            return len(done) == len(expectations)
