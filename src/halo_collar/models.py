"""Small stable models for authentication and API request contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _parse_string_enum(cls: type[Enum], value: Any, label: str) -> Any:
    if isinstance(value, cls):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string.")
    normalized = value.replace("-", "").replace("_", "").casefold()
    for item in cls:
        if str(item.value).casefold() == normalized:
            return item
    choices = ", ".join(str(item.value) for item in cls)
    raise ValueError(f"Unknown {label} {value!r}. Choose one of: {choices}")


class CorrectionType(str, Enum):
    """Correction names supported by the Halo Collar API."""

    WARNING = "Warning"
    FIRST_TIME = "FirstTime"
    ESCALATION = "Escalation"
    RETURN_WHISTLE = "ReturnWhistle"
    GOOD_BEHAVIOR = "GoodBehavior"
    HEADING_HOME = "HeadingHome"

    @property
    def expiration_seconds(self) -> int:
        if self in {
            CorrectionType.RETURN_WHISTLE,
            CorrectionType.GOOD_BEHAVIOR,
            CorrectionType.HEADING_HOME,
        }:
            return 7
        return 4

    @classmethod
    def parse(cls, value: str | CorrectionType) -> CorrectionType:
        return _parse_string_enum(cls, value, "correction type")


class CorrectionRuleKindType(str, Enum):
    """Feedback modalities accepted by persistent rules and collar tests."""

    VIBRATION = "Vibration"
    SOUND = "Sound"
    SHOCK = "Shock"

    @classmethod
    def parse(
        cls,
        value: str | CorrectionRuleKindType,
    ) -> CorrectionRuleKindType:
        return _parse_string_enum(cls, value, "correction rule kind type")


@dataclass(frozen=True, slots=True)
class CorrectionRuleUpdate:
    """One identified item in Halo's correction-rule batch update."""

    correction_rule_id: str
    kind_type: CorrectionRuleKindType | str
    level: int | None = None
    sound_id: str | None = None
    vibration_id: str | None = None


class FirmwareUpdateStatus(str, Enum):
    """Known asynchronous firmware-update states returned by collar reads."""

    UNKNOWN = "unknown"
    DOWNLOAD_DELAYED_INCOMPATIBLE_NETWORK = "downloadDelayedIncompatibleNetwork"
    DOWNLOAD_DELAYED_LOW_BATTERY = "downloadDelayedLowBattery"
    DOWNLOADING = "downloading"
    DOWNLOAD_FAILED = "downloadFailed"
    VERIFYING = "verifying"
    VERIFY_FAILED = "verifyFailed"
    APPLY_DELAYED_NOT_CHARGING = "applyDelayedNotCharging"
    APPLYING = "applying"
    APPLY_FAILED = "applyFailed"
    DOWNLOAD_DELAYED_NOT_ON_CHARGER = "downloadDelayedNotOnCharger"
    APPLIED = "applied"
    DOWNLOAD_NOT_STARTED = "downloadNotStarted"

    @classmethod
    def parse(cls, value: str | FirmwareUpdateStatus) -> FirmwareUpdateStatus:
        return _parse_string_enum(cls, value, "firmware update status")


class BeaconModelType(str, Enum):
    """Physical beacon models accepted by Halo's beacon routes."""

    UNKNOWN = "Unknown"
    USB = "Usb"
    OUTDOOR = "Outdoor"
    UFO = "Ufo"
    REMOTE_CONTROL = "RemoteControl"
    REMOTE_CONTROL_5_BUTTON = "RemoteControl5Button"

    @classmethod
    def parse(cls, value: str | BeaconModelType) -> BeaconModelType:
        return _parse_string_enum(cls, value, "beacon model type")


class BeaconActionType(str, Enum):
    """Actions a Halo beacon can apply when a pet approaches it."""

    KEEP_AWAY = "KeepAway"
    IGNORE_FENCES = "IgnoreFences"
    PORTABLE_FENCE = "PortableFence"
    NOTIFY_ONLY = "NotifyOnly"
    REMOTE_FEEDBACK = "RemoteFeedback"
    START_WALK = "StartWalk"
    LEAVE_FENCE = "LeaveFence"

    @classmethod
    def parse(cls, value: str | BeaconActionType) -> BeaconActionType:
        return _parse_string_enum(cls, value, "beacon action type")


class BeaconCorrectionEscalationType(str, Enum):
    """Correction escalation choices carried by beacon configuration."""

    UNKNOWN = "Unknown"
    WARNING = "Warning"
    FIRST_TIME = "FirstTime"
    ESCALATION = "Escalation"
    RETURN_WHISTLE = "ReturnWhistle"
    GOOD_BEHAVIOR = "GoodBehavior"
    HEADING_HOME = "HeadingHome"

    @classmethod
    def parse(
        cls,
        value: str | BeaconCorrectionEscalationType,
    ) -> BeaconCorrectionEscalationType:
        return _parse_string_enum(cls, value, "beacon correction escalation type")


class WalkStopOption(str, Enum):
    """Ways Halo can leave leash mode when stopping one collar's walk."""

    DEFAULT = "Default"
    FORCE_KEEP_FENCES_MODE = "ForceKeepFencesMode"
    FORCE_SET_FENCES_ON = "ForceSetFencesOn"

    @classmethod
    def parse(cls, value: str | WalkStopOption) -> WalkStopOption:
        return _parse_string_enum(cls, value, "walk stop option")


@dataclass(slots=True)
class TokenSet:
    """OAuth tokens plus the local expiry calculated from ``expires_in``."""

    access_token: str
    refresh_token: str
    expires_at: float
    token_type: str = "Bearer"
    scope: str = ""

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= datetime.now(timezone.utc).timestamp()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TokenSet:
        return cls(
            access_token=str(value["access_token"]),
            refresh_token=str(value["refresh_token"]),
            expires_at=float(value["expires_at"]),
            token_type=str(value.get("token_type", "Bearer")),
            scope=str(value.get("scope", "")),
        )


@dataclass(frozen=True, slots=True)
class AuthorizationFlow:
    """Ephemeral values required to validate and complete a PKCE login."""

    url: str
    code_verifier: str
    state: str
    nonce: str
