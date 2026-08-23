"""Application-level failures exposed by outbound ports."""


class QueueAccessError(RuntimeError):
    """The configured message queue could not perform an operation."""
