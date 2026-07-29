"""Unofficial, reverse-engineered Halo Collar API client."""

from .auth import (
    ANDROID_CLIENT_SECRET,
    ANDROID_PROFILE,
    IOS_CLIENT_SECRET,
    IOS_PROFILE,
    HaloOAuth,
    OAuthClientProfile,
)
from .client import HaloClient
from .errors import (
    AuthenticationError,
    CommandCounterUnknownError,
    CorrectionOutcomeUnknownError,
    HaloAPIError,
    HaloError,
    HaloSignalRError,
    InvalidCallbackError,
    LoginRequiredError,
    SignalRBackpressureError,
    SignalRConnectionError,
    SignalRNegotiationError,
    SignalRProtocolError,
    StaleCommandNumberError,
    UnsafeCorrectionError,
)
from .models import AuthorizationFlow, CorrectionType, TokenSet
from .signalr import HaloSignalRClient, SignalREvent, SignalRHub
from .storage import StateStore

__all__ = [
    "AuthenticationError",
    "ANDROID_CLIENT_SECRET",
    "ANDROID_PROFILE",
    "AuthorizationFlow",
    "CommandCounterUnknownError",
    "CorrectionOutcomeUnknownError",
    "CorrectionType",
    "HaloAPIError",
    "HaloClient",
    "HaloError",
    "HaloSignalRClient",
    "HaloSignalRError",
    "HaloOAuth",
    "IOS_CLIENT_SECRET",
    "IOS_PROFILE",
    "InvalidCallbackError",
    "LoginRequiredError",
    "OAuthClientProfile",
    "SignalRBackpressureError",
    "SignalRConnectionError",
    "SignalREvent",
    "SignalRHub",
    "SignalRNegotiationError",
    "SignalRProtocolError",
    "StaleCommandNumberError",
    "StateStore",
    "TokenSet",
    "UnsafeCorrectionError",
]
