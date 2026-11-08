"""Select canonical per-reviewer records without duplicating them in singleton slots."""


def source_value(state, source):
    """The checkpoint value an event renders from."""
    value = state
    for key in source:
        value = value[key]
    return value


def current_reports(state):
    """Current intake, PR-review and design-vote reports in checkpoint order."""
    reports = list(state.get("qc", {}).values()) + list(state.get("reviews", {}).values())
    reports += [vote["report"] for vote in state.get("design_votes", {}).values()]
    return reports


def slot(state):
    """The selected batch member, or the legacy serial task record."""
    batch = state.get("review_batch", {})
    return batch.get("members", {}).get(batch.get("active"), state)


def operation(state):
    """Current logical operation for attribution, replay and consumption."""
    return slot(state).get("operation", {})


def path(state, field):
    """Stable publication source for a selected member's rejected report."""
    batch = state.get("review_batch", {})
    return ["review_batch", "members", batch["active"], field] if batch.get("active") else [field]


def operations(state):
    """All bounded operations, including unapplied sibling results."""
    return [state.get("operation", {})] + [
        m.get("operation", {}) for m in state.get("review_batch", {}).get("members", {}).values()
    ]
