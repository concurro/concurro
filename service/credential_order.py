"""Read-only ordering manifest for the vault's schema 4 migration.

Each consenting owner's current personal grants name the credential type their targets select
today. Where every selection agrees, the vault places that type first in the owner's new list.
"""

import sponsorship

FORMAT = 1


def selections(registry):
    """Yield `(owner, provider, source)` for every active personal grant on a live task or issue."""
    for repository in registry.sponsor_repositories():
        for issue in registry.sponsor_issues(repository, active=True):
            key = (repository, issue)
            document = registry.sponsorship(key)
            task = document.get("task") or {}
            slots = task.get("slots") or document.get("slots") or {}
            funding = task.get("provider_funding") or document.get("provider_funding") or {}
            # The projection's routes follow the task's effective configuration; frozen initialization may predate it.
            routes = document.get("routes") or []
            binding = registry.binding(key)
            for provider in sponsorship.PROVIDERS:
                slot = slots.get(provider) or {}
                # Org-funded routing says nothing about which personal account an owner prefers.
                if (
                    funding.get(provider) != "contributed"
                    or slot.get("status") != "active"
                    or not slot.get("sponsor_id")
                ):
                    continue
                kinds = sorted(
                    {
                        sponsorship.ROUTES[route["route"]]["kind"]
                        for route in routes
                        if route.get("provider") == provider and route.get("route") in sponsorship.ROUTES
                    }
                )
                yield (
                    str(slot["sponsor_id"]),
                    provider,
                    {
                        "repository": repository,
                        "issue": issue,
                        "config_sha": binding.config_sha if binding else None,
                        "kinds": kinds,
                    },
                )


def manifest(registry):
    """Each owner/provider's agreed first credential type, or why none could be established."""
    owners = {}
    for owner, provider, source in selections(registry):
        owners.setdefault(owner, {}).setdefault(provider, []).append(source)
    for lists in owners.values():
        for provider, sources in lists.items():
            kinds = {kind for source in sources for kind in source["kinds"]}
            complete = all(source["kinds"] for source in sources)
            evidence = "conflict" if len(kinds) > 1 else "agreement" if complete else "unavailable"
            lists[provider] = {
                "first": kinds.pop() if evidence == "agreement" else None,
                "evidence": evidence,
                "sources": sources,
            }
    return {"format": FORMAT, "owners": owners}
