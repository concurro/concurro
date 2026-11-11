"""Run byte-pinned historical publication scenarios using their original queue format."""

from functools import wraps
from unittest.mock import patch


def legacy_events(test):
    """Queue format-five events; their production recovery/writer paths remain unchanged."""

    @wraps(test)
    def run(*args, **kwargs):
        """Restore the current queue format after the historical scenario."""
        with patch("conversation.event_format", return_value=5):
            return test(*args, **kwargs)

    return run
