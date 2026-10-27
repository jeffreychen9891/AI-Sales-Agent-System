from __future__ import annotations


class WeComProxyError(RuntimeError):
    """Base class for every failure raised by the restricted WeComProxy client."""


class WeComProxyConfigError(WeComProxyError):
    """Credentials or allowlist configuration is missing, unreadable or unsafe."""


class ForbiddenOperationError(WeComProxyError):
    """A provider operation that v2 deliberately does not implement was called.

    These are the destructive connection-lifecycle operations: enumerating,
    logging out, stopping, or deleting the notify URL of a connection. v2 runs
    on its own purchased connection and cannot reach v1's (ADR-0027), so this is
    ordinary blast-radius hygiene rather than v1 isolation -- but M1a has no use
    for any of them, and not implementing them keeps that true.
    """


class GuidNotAllowedError(WeComProxyError):
    """A guid-bearing call named a guid that is not on v2's allowlist."""


class ConfirmationRequiredError(WeComProxyError):
    """An operation requiring per-call human confirmation was not confirmed."""
