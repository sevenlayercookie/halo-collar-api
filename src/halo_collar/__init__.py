"""Unofficial, reverse-engineered Halo Collar REST client."""

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
    InvalidCallbackError,
    LoginRequiredError,
    StaleCommandNumberError,
    UnsafeCorrectionError,
)
from .models import AuthorizationFlow, CorrectionType, TokenSet
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
    "HaloOAuth",
    "IOS_CLIENT_SECRET",
    "IOS_PROFILE",
    "InvalidCallbackError",
    "LoginRequiredError",
    "OAuthClientProfile",
    "StaleCommandNumberError",
    "StateStore",
    "TokenSet",
    "UnsafeCorrectionError",
]
