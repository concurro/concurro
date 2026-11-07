"""Repository destinations and enrollment for an upstream/fork target."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """One upstream/fork pair and the repositories allowed to supply issues."""

    upstream: str
    fork: str
    issues: str

    @property
    def repositories(self):
        """All repositories needed for source, publication and enrollment."""
        return tuple(dict.fromkeys((self.upstream, self.fork)))

    @property
    def issue_repositories(self):
        """Repositories whose issues may enroll in this target."""
        return {"upstream": (self.upstream,), "fork": (self.fork,), "both": self.repositories}[self.issues]


def enrollment(targets):
    """Map each issue repository to exactly one target; reject ambiguous or duplicate settings."""
    result, seen, spellings = {}, set(), {}
    for target in targets:
        for repository in target.repositories:
            previous = spellings.setdefault(repository.lower(), repository)
            if previous != repository:
                raise ValueError("repository spelling differs between targets")
        for repository in target.issue_repositories:
            if repository.lower() in seen:
                raise ValueError("issue repository is assigned to more than one target")
            seen.add(repository.lower())
            result[repository] = target
    return result


def permissions(targets):
    """Union required token permissions when a repository serves several target roles."""
    result = {}
    for target in targets:
        for repository in target.repositories:
            result.setdefault(repository, {"read": {"contents": "read"}, "write": {}})
        upstream = result[target.upstream]
        upstream["read"].update(pull_requests="read", checks="read", statuses="read", actions="read")
        # Keep contents:write for the tested owned-thread resolution permission combination.
        upstream["write"].update(pull_requests="write", contents="write")
        # Referencing an upstream commit can introduce workflows into the fork unchanged.
        result[target.fork]["write"].update(contents="write", actions="write", workflows="write")
        for repository in target.issue_repositories:
            result[repository]["read"]["issues"] = "read"
            result[repository]["write"]["issues"] = "write"
    return result


def verify(targets, reader):
    """Require public repositories in one fork network before dispatching anonymous source fetches."""
    from policy import Blocked
    found = {}
    for target in targets:
        for repository in target.repositories:
            if repository not in found:
                data = reader(repository).request("GET", "repos/" + repository)
                if data.get("full_name", "").lower() != repository.lower() or data.get("private") is not False:
                    raise Blocked("the MVP target repositories must be public")
                found[repository] = data
        upstream, fork = found[target.upstream], found[target.fork]
        source = (upstream.get("source") or {}).get("full_name", upstream["full_name"])
        if fork.get("fork") is not True or (fork.get("source") or {}).get("full_name", "").lower() != source.lower():
            raise Blocked("target fork must belong to the upstream repository network")
