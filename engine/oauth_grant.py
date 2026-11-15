"""GitHub's user-token response, validated without guessing lifetimes; shared by every OAuth client."""


class GrantError(ValueError):
    """A token response was refused or incomplete; `uncertain` when the grant may still have been issued."""

    def __init__(self, code, uncertain=False):
        """`code` is `refused` or `incomplete`; no response text is retained."""
        super().__init__(code)
        self.code, self.uncertain = code, uncertain


def parse(status, payload):
    """`(token, expires_in, refresh_token, refresh_expires_in)` from one token response.

    Absent expiry and refresh fields stay absent: a non-expiring token has neither.
    """
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(token, str) or not token:
        raise GrantError("refused", uncertain=status >= 500)
    lifetime, refresh, refresh_lifetime = (
        payload.get("expires_in"),
        payload.get("refresh_token"),
        payload.get("refresh_token_expires_in"),
    )
    if lifetime is not None and (type(lifetime) is not int or lifetime <= 0):
        raise GrantError("incomplete", uncertain=True)
    if refresh is not None and (
        not isinstance(refresh, str)
        or not refresh
        or type(refresh_lifetime) is not int
        or refresh_lifetime <= 0
        or lifetime is None
    ):
        raise GrantError("incomplete", uncertain=True)
    return token, lifetime, refresh, refresh_lifetime
