"""Service failures, including the control exceptions that stop a tick."""


class RegistryError(RuntimeError):
    """The registry cannot be opened safely: missing, mismatched, locked or corrupt."""


class GatewayStop(BaseException):
    """Stop the tick without further writes.

    Not an `Exception`, so the engine's broad wrappers and best-effort paths never
    catch, wrap or report it.
    """


class OwnershipLost(GatewayStop):
    """The generation no longer owns the task; a newer owner continues it."""


class Unresolved(GatewayStop):
    """The task has a write of unknown outcome that an operator must resolve."""


class RegistryFailure(GatewayStop):
    """Durable state failed; scheduling and mutation admission stop entirely."""
