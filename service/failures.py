"""Map a tick's failure to what the scheduler does next; shared by every caller of the engine."""

from controller import Outcome
from github import Transient

from .errors import GatewayStop

BACKOFF_SECONDS = 60
BACKOFF_LIMIT = 3600


def backoff(attempt):
    """Local exponential backoff by attempt; only this locally chosen delay is capped."""
    return min(BACKOFF_LIMIT, BACKOFF_SECONDS * 2 ** min(attempt, 16))


def classify(error, stop=None, attempt=0):
    """A stop (recorded or raised), a `wait-until` outcome for a transient failure, or None.

    The gateway's recorded `stop` wins over `error`, so a later exception cannot turn a
    stopped tick into an ordinary failure. None leaves the failure to the remaining
    rows of the classifier (step 3).
    """
    if stop is not None:
        return stop
    if isinstance(error, GatewayStop):
        return error
    if isinstance(error, Transient):
        delay = error.retry_after if error.retry_after is not None else backoff(attempt)
        return Outcome("wait-until", retry_after=delay)
    return None
