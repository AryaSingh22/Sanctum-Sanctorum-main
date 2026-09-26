"""Business-rule errors raised by the services.

Services stay free of HTTP concerns; ``app.main`` maps each error type to a status code.
The error message becomes the response's ``detail``.
"""


class DomainError(Exception):
    """A request broke a business rule. Raise one of the subclasses, not this class."""


class NotFoundError(DomainError):
    """The requested record does not exist."""


class ForbiddenError(DomainError):
    """The member is not allowed to do this."""


class ConflictError(DomainError):
    """The request clashes with the current state (stock, status, an existing record)."""
