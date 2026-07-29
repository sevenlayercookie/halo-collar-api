"""Exceptions raised by the Halo Collar client."""

from __future__ import annotations

from typing import Any


class HaloError(Exception):
    """Base exception for this package."""


class AuthenticationError(HaloError):
    """Halo authentication failed."""


class LoginRequiredError(AuthenticationError):
    """No usable session exists or interactive login is required."""


class InvalidCallbackError(AuthenticationError):
    """The pasted OAuth callback is invalid or belongs to another login attempt."""


class HaloAPIError(HaloError):
    """The Halo REST API returned an unsuccessful response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.method = method
        self.path = path


class HaloSignalRError(HaloError):
    """The Halo live-event connection failed."""


class SignalRNegotiationError(HaloSignalRError):
    """A SignalR negotiation response was unsuccessful or malformed."""


class SignalRConnectionError(HaloSignalRError):
    """The live-event connection could not be established or maintained."""


class SignalRProtocolError(HaloSignalRError):
    """The server sent a SignalR frame this client could not safely interpret."""


class SignalRBackpressureError(HaloSignalRError):
    """The consumer did not drain live events quickly enough."""


class UnsafeCorrectionError(HaloError):
    """A correction was stopped because a safety precondition was not met."""


class CommandCounterUnknownError(HaloError):
    """No local command counter exists yet for this pet."""


class CorrectionOutcomeUnknownError(HaloError):
    """The connection failed after dispatch, so delivery cannot be determined."""


class StaleCommandNumberError(HaloError):
    """Halo rejected a correction because another client advanced the counter."""

    def __init__(self, current_command_number: int | None, response: dict[str, Any]) -> None:
        suffix = (
            f" Halo reports the current command number is {current_command_number}."
            if current_command_number is not None
            else ""
        )
        super().__init__(
            "Halo rejected the command number as stale."
            f"{suffix} Confirm the correction again before sending a new command."
        )
        self.current_command_number = current_command_number
        self.response = response
