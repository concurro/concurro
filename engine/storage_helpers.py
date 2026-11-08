"""Storage traversal and authorship primitives without engine dependencies."""


def owned(items, author, prefix, author_field):
    """Select matching authored items in collection order, without changing malformed-input handling."""
    return [
        item
        for item in items
        if item.get(author_field, {}).get("login") == author and item.get("body", "").startswith(prefix)
    ]


def graft_slot(value, path, error):
    """Locate an existing vacant slot before the caller fetches its referenced value."""
    parent = value
    for component in path[:-1]:
        parent = parent[component]
    key = path[-1]
    if parent[key] is not None:
        raise error
    return parent, key
