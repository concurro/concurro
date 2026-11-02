"""Map a tick's failure to what the scheduler does next; shared by every caller of the engine."""

from checkpoints import CheckpointError
from controller import Outcome
from conversation import PublicationError
from github import Transient

from .errors import GatewayStop

BACKOFF_SECONDS = 60
BACKOFF_LIMIT = 3600


def backoff(attempt):
    """Local exponential backoff by attempt; only this locally chosen delay is capped."""
    return min(BACKOFF_LIMIT, BACKOFF_SECONDS * 2 ** min(attempt, 16))


def checkpoint(error):
    """A checkpoint failure that leaves the stage in place; a publication conflict suspends instead."""
    return isinstance(error, CheckpointError) and not isinstance(error, PublicationError)


def preserved(error, verified):
    """Whether the failure keeps the stage: no verified state, a transient error, an uncertain call or a checkpoint."""
    return not verified or getattr(error, "uncertain", False) or checkpoint(error)


def classify(error, stop=None, attempt=0, verified=True):
    """The stop, or the outcome, for a tick that raised `error`.

    The gateway's recorded `stop` wins over `error`, so a later exception cannot turn a
    stopped tick into an ordinary failure. `verified` says whether the task's state was
    loaded and owned; without it nothing is suspended and the tick is retried later.
    `attempt` counts the task's consecutive failures before this one.
    """
    if stop is not None:
        return stop
    if isinstance(error, GatewayStop):
        return error
    if isinstance(error, Transient):
        delay = error.retry_after if error.retry_after is not None else backoff(attempt)
        return Outcome("wait-until", retry_after=delay)
    if preserved(error, verified):
        return Outcome("wait-until", retry_after=backoff(attempt))
    return Outcome("blocked")


def effect(error, verified):
    """The diagnostic write a failure warrants: None, `health` (a notice) or `suspend` (to `blocked`)."""
    if isinstance(error, (GatewayStop, Transient)) or not verified or getattr(error, "uncertain", False):
        return None
    return "health" if checkpoint(error) else "suspend"
