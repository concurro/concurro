"""Attempt-bound completion metadata, independent of model verdicts and storage layout."""

from copy import deepcopy

import review_state
import review_timing
from policy import digest
from report_store import ReportDataError, annotated, projection


def binding(state, operation, repository):
    """Identify the already charged operation, without copying instructions or credentials."""
    return {
        **({"admission": digest(operation["admission"])} if "admission" in operation else {}),
        "repository": repository,
        "issue": state["issue"],
        "operation": {k: operation.get(k) for k in ("id", "attempt", "provider", "stage", "request")},
        "contract": digest(operation.get("contract", {})),
        "path": review_state.path(state, "operation"),
        "call": operation.get("call", state["calls"]),
    }


def capture(state, operation, response, session, repository):
    """Attach reconstruction metadata to a core that is actually uploaded for this attempt."""
    core, annotations, draft = projection(response)
    completion = {
        "binding": binding(state, operation, repository),
        "response": digest(response),
        "annotations": annotations,
        "draft": draft,
        "timing": review_timing.normalize(operation.get("timing")),
        "session": None,
    }
    if session:
        completion["session"] = {
            "role": session["role"],
            "before": {k: deepcopy(v) for k, v in session.items() if k != "role"},
            "after": deepcopy(state["sessions"][session["role"]]),
        }
    return digest(core), completion


def restore(state, operation, core, receipt, repository):
    """Validate everything before restoring one response and an idempotent session advance."""
    try:
        if set(receipt) != {"binding", "response", "annotations", "draft", "timing", "session"}:
            raise ValueError()
        if receipt["binding"] != binding(state, operation, repository):
            raise ValueError()
        response = annotated(deepcopy(core), receipt["annotations"], receipt["draft"])
        if digest(response) != receipt["response"]:
            raise ValueError()
        session = receipt["session"]
        if session is not None:
            if set(session) != {"role", "before", "after"}:
                raise ValueError()
            before, after = session["before"], session["after"]
            if (
                after["turn"] != before["turn"] + 1
                or {k: v for k, v in after.items() if k not in ("turn", "completed")}
                != {k: v for k, v in before.items() if k not in ("turn", "completed")}
                or set(after["completed"]) != {"call", "request", "response"}
                or after["completed"]["call"] != receipt["binding"]["call"]
                or after["completed"]["request"] != operation["request"]
            ):
                raise ValueError()
            current = state.get("sessions", {}).get(session["role"])
            if current not in (before, after):
                return False  # A rotated or advanced session cannot be rolled back by an old upload.
        if session is not None:
            state["sessions"][session["role"]] = deepcopy(session["after"])
        operation.update(status="completed", response=response)
        if receipt["timing"] is not None:
            operation["timing"] = deepcopy(receipt["timing"])
        if review_state.slot(state) is not state:
            review_state.slot(state)["status"] = "complete"
        return True
    except (KeyError, IndexError, TypeError, ValueError):
        raise ReportDataError("Malformed attempt completion record.") from None
