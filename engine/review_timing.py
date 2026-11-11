"""Optional exchange observations, kept outside model reports and authorization inputs."""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

import report_store
import review_state

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from records import Document, Operation, ReportIndex, TaskState, Timing


STATUSES = {"queued", "running", "done", "lost", "cancelled"}


def normalize(value: object) -> Timing | None:
    """Keep only a bounded, non-secret receipt; malformed telemetry is unavailable."""
    if not isinstance(value, dict):
        return None
    turn = value.get("turn")
    if (
        not isinstance(turn, str)
        or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", turn)
        or not isinstance(value.get("status"), str)
        or value["status"] not in STATUSES
    ):
        return None
    result: Timing = {"turn": turn, "status": value["status"]}
    for key in ("created", "started", "finished"):
        stamp: Any = value.get(key)
        result[key] = stamp if type(stamp) in (int, float) and 0 <= stamp <= 1e12 and math.isfinite(stamp) else None
    return result


def durations(value: object) -> dict[str, float | None]:
    """Exchange-clock durations only; absent or inconsistent intervals remain unknown."""
    value = normalize(value) or {}
    created, started, finished = (value.get(k) for k in ("created", "started", "finished"))
    admitted = started - created if created is not None and started is not None and started >= created else None
    execution = (
        finished - started
        if value.get("status") == "done" and started is not None and finished is not None and finished >= started
        else None
    )
    return {"admission_to_claim_seconds": admitted, "execution_seconds": execution}


def index_receipt(state: TaskState, kind: str, details: Document) -> Timing | None:
    """Associate telemetry only with the current operation's own report, never model text."""
    stage = {
        "review": "review",
        "review_rejection": "plan_qc",
        "intake_rejection": "intake",
        "implementation": "develop",
    }.get(kind)
    if kind == "qc":
        stage = "intake" if details.get("phase") == "intake" else "plan_qc"
    operation = review_state.operation(state)
    value = normalize(operation.get("timing"))
    if (
        not value
        or not stage
        or operation.get("stage") != stage
        or operation.get("provider") != details.get("provider")
        or operation.get("status") not in ("completed", "consumed")
    ):
        return None
    return {"operation": operation["id"], "attempt": operation["attempt"], **value}


def make_room(state: TaskState, limit: int, size: Callable[[TaskState], int]) -> None:
    """Drop inline observations before they could make a checkpoint fail to save."""
    if size(state) <= limit:
        return
    inline = report_store.inline_fields(state, {"timing"})
    holders: list[ReportIndex | Operation] = list(state.get("reports", []))
    holders += [e.get("index", {}) for e in state.get("conversation", {}).get("pending", [])]
    holders.extend(review_state.operations(state))
    for holder in holders:
        if (id(holder), "timing") in inline:
            if size(state) <= limit:
                return
            holder.pop("timing", None)
