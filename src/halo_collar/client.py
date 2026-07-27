"""Synchronous client for the observed Halo Collar REST endpoints."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from .auth import (
    IOS_PROFILE,
    HaloOAuth,
    build_http_client,
    client_profile,
    resolve_client_secret,
)
from .errors import (
    CorrectionOutcomeUnknownError,
    HaloAPIError,
    LoginRequiredError,
    StaleCommandNumberError,
    UnsafeCorrectionError,
)
from .models import CorrectionType, TokenSet
from .storage import StateStore

API_BASE_URL = "https://api.halocollar.com"
DEFAULT_MOBILE_ID = 2


class HaloClient:
    """A conservative client for endpoints confirmed in captured iOS traffic.

    Responses remain dictionaries because the reverse-engineered server schema may
    change. The client intentionally performs no automatic network retries.
    """

    def __init__(
        self,
        *,
        client_secret: str | None = None,
        tokens: TokenSet | None = None,
        store: StateStore | None = None,
        timezone_name: str | None = None,
        app_instance_id: str | None = None,
        client_id: str | None = None,
        app_version: str | None = None,
        http: httpx.Client | None = None,
        api_base_url: str = API_BASE_URL,
        auth_base_url: str | None = None,
    ) -> None:
        self.store = store or StateStore()
        settings = self.store.settings()
        stored_auth = self.store.auth_profile()
        stored_client_id = stored_auth.get("client_id")
        self.profile = client_profile(client_id or stored_client_id or IOS_PROFILE.client_id)
        self.client_id = self.profile.client_id
        self.client_secret = resolve_client_secret(self.profile, explicit=client_secret)
        self.tokens = tokens
        # Halo binds refresh tokens to the client that issued them, so a stored
        # session belonging to another profile must not be paired with this one.
        stored_session_matches = stored_client_id in (None, self.client_id)
        self._conflicting_client_id = None if stored_session_matches else stored_client_id
        if self.tokens is None and stored_session_matches:
            try:
                self.tokens = self.store.load_tokens()
            except LoginRequiredError:
                self.tokens = None
        self.timezone_name = (
            timezone_name or settings.get("timezone") or os.environ.get("TZ", "UTC")
        )
        self.app_instance_id = (
            app_instance_id or settings.get("app_instance_id") or str(uuid.uuid4())
        )
        stored_app_version = (
            stored_auth.get("app_version") if stored_client_id == self.client_id else None
        )
        self.app_version = app_version or stored_app_version or self.profile.app_version
        self.api_base_url = api_base_url.rstrip("/")
        self.auth_base_url = auth_base_url
        self._parallel_call_version = "0"
        self._owns_http = http is None
        self.http = http or build_http_client()
        if (
            settings.get("app_instance_id") != self.app_instance_id
            or settings.get("timezone") != self.timezone_name
        ):
            self.store.update_settings(
                app_instance_id=self.app_instance_id,
                timezone=self.timezone_name,
            )

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> HaloClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def halo_client_header(self) -> str:
        return urlencode(
            {
                "clientId": self.client_id,
                "version": self.app_version,
                "appInstanceId": self.app_instance_id,
                "timezone": self.timezone_name,
            }
        )

    def refresh_login(self, *, force: bool = False) -> TokenSet:
        if self.tokens is None:
            if self._conflicting_client_id is not None:
                raise LoginRequiredError(
                    f"The stored login belongs to {self._conflicting_client_id}, not "
                    f"{self.client_id}. Halo binds refresh tokens to one client; log in "
                    "again with this profile."
                )
            raise LoginRequiredError("No Halo login is stored. Run `halo login`.")
        if not force and not self.tokens.is_expired:
            return self.tokens
        if not self.client_secret:
            raise LoginRequiredError(
                f"No {self.profile.name} client secret is available. Run `halo login`."
            )
        kwargs: dict[str, Any] = {}
        if self.auth_base_url is not None:
            kwargs["auth_base_url"] = self.auth_base_url
        oauth = HaloOAuth(self.client_secret, profile=self.profile, http=self.http, **kwargs)
        try:
            self.tokens = oauth.refresh(self.tokens.refresh_token)
        except LoginRequiredError:
            self.store.clear_tokens()
            self.tokens = None
            raise
        self.store.save_tokens(self.tokens)
        return self.tokens

    def configuration(self) -> dict[str, Any]:
        """Fetch Halo's public application configuration."""

        return self._get_object("/configuration/", authenticated=False)

    def collars(self) -> list[dict[str, Any]]:
        """List collars owned by the authenticated account."""

        value = self._request_json("GET", "/collar/my/")
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise HaloAPIError("Halo returned an unexpected collar list.")
        return value

    def pet(self, pet_id: str, *, refresh_telemetry: bool = False) -> dict[str, Any]:
        """Fetch a pet, optionally asking the collar for fresher telemetry."""

        return self._get_object(
            f"/pet/{_identifier(pet_id)}/",
            params={"RefreshTelemetry": str(refresh_telemetry)},
        )

    def user_profile(self) -> dict[str, Any]:
        return self._get_object("/user-profile/")

    def beacons(self) -> dict[str, Any] | list[Any]:
        value = self._request_json("GET", "/beacon/my/")
        if not isinstance(value, (dict, list)):
            raise HaloAPIError("Halo returned an unexpected beacon response.")
        return value

    def subscription(self) -> dict[str, Any] | list[Any]:
        value = self._request_json("GET", "/subscription/my/")
        if not isinstance(value, (dict, list)):
            raise HaloAPIError("Halo returned an unexpected subscription response.")
        return value

    def portal_notifications(self) -> dict[str, Any] | list[Any]:
        value = self._request_json("GET", "/portal-notification/my/in-app/")
        if not isinstance(value, (dict, list)):
            raise HaloAPIError("Halo returned an unexpected notification response.")
        return value

    def server_time(self) -> datetime:
        """Return Halo's UTC server clock used for command expiration."""

        response = self._request("GET", "/system/server-date-time")
        parsed = _parse_server_time_response(response)
        if parsed is None:
            raise HaloAPIError(
                "Halo's server-time response was not recognized; correction dispatch was stopped.",
                status_code=response.status_code,
                method="GET",
                path="/system/server-date-time",
            )
        return parsed.astimezone(timezone.utc)

    def collar_for_pet(self, pet_id: str) -> dict[str, Any]:
        for collar in self.collars():
            pet_info = collar.get("petInfo")
            if isinstance(pet_info, dict) and str(pet_info.get("id")) == pet_id:
                return collar
        raise UnsafeCorrectionError("No collar assigned to this pet was returned by Halo.")

    @staticmethod
    def collar_is_online(collar: dict[str, Any]) -> bool:
        telemetry = collar.get("telemetry")
        if not isinstance(telemetry, dict):
            return False
        for adapter in ("wiFi", "cellular"):
            state = telemetry.get(adapter)
            if (
                isinstance(state, dict)
                and str(state.get("status", "")).casefold() == "socketconnected"
            ):
                return True
        return False

    def send_instant_correction(
        self,
        pet_id: str,
        correction_type: CorrectionType | str,
        *,
        command_number: int | None = None,
        require_online: bool = True,
    ) -> dict[str, Any]:
        """Send one physical correction with no automatic network retry.

        The command number is reserved on disk *before* the request. If the
        connection fails, this method raises ``CorrectionOutcomeUnknownError`` and
        deliberately does not retry because the collar may already have acted. A
        definite 401 is refreshed and retried once because the API rejected the
        original request before accepting the command.
        """

        kind = CorrectionType.parse(correction_type)
        if require_online:
            collar = self.collar_for_pet(pet_id)
            if not self.collar_is_online(collar):
                raise UnsafeCorrectionError(
                    "Halo does not report this collar as socket-connected over Wi-Fi or cellular."
                )
        server_now = self.server_time()
        reserved_number = self.store.reserve_command_number(pet_id, command_number)
        expiration = server_now + timedelta(seconds=kind.expiration_seconds)
        body = {
            "MobileId": DEFAULT_MOBILE_ID,
            "CommandNumber": reserved_number,
            "ExpirationDate": _format_utc(expiration),
            "CorrectionType": kind.value,
        }
        try:
            response = self._request(
                "POST",
                f"/pet/{_identifier(pet_id)}/run-instant-correction/",
                json_body=body,
                wrap_transport_errors=False,
                raise_for_status=False,
            )
        except httpx.HTTPError as exc:
            raise CorrectionOutcomeUnknownError(
                "The correction request encountered a network error after dispatch. "
                "It was not retried; its delivery is unknown."
            ) from exc

        value = self._decode_json(response)
        if not isinstance(value, dict):
            raise HaloAPIError(
                "Halo returned an unexpected correction response.",
                status_code=response.status_code,
                method="POST",
                path="/pet/{pet_id}/run-instant-correction/",
            )
        if str(value.get("result", "")).casefold() == "oldcommandnumber":
            current = value.get("currentCommandNumber")
            current_number = current if isinstance(current, int) else None
            if current_number is not None:
                self.store.reconcile_command_number(pet_id, current_number)
            raise StaleCommandNumberError(current_number, value)
        if response.is_error:
            raise HaloAPIError(
                f"Halo API correction request failed with HTTP {response.status_code}.",
                status_code=response.status_code,
                method="POST",
                path="/pet/{pet_id}/run-instant-correction/",
            )
        if str(value.get("result", "")).casefold() != "success":
            raise HaloAPIError(
                f"Halo did not accept the correction (result={value.get('result', 'unknown')!r}).",
                status_code=response.status_code,
                method="POST",
                path="/pet/{pet_id}/run-instant-correction/",
            )
        return value

    def _get_object(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        value = self._request_json("GET", path, params=params, authenticated=authenticated)
        if not isinstance(value, dict):
            raise HaloAPIError(f"Halo returned an unexpected response for {path}.")
        return value

    def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._decode_json(self._request(method, path, **kwargs))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        authenticated: bool = True,
        retry_after_unauthorized: bool = True,
        wrap_transport_errors: bool = True,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        method = method.upper()
        if authenticated:
            self.refresh_login()
        headers = {
            "Halo-Client": self.halo_client_header,
            "Halo-ParallelCall-Version": self._parallel_call_version,
        }
        if authenticated:
            if self.tokens is None:
                raise LoginRequiredError("No Halo login is stored. Run `halo login`.")
            headers["Authorization"] = f"{self.tokens.token_type} {self.tokens.access_token}"
        try:
            response = self.http.request(
                method,
                f"{self.api_base_url}{path}",
                params=params,
                json=json_body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            if not wrap_transport_errors:
                raise
            raise HaloAPIError(
                "Could not reach the Halo API. The request was not retried.",
                method=method,
                path=path,
            ) from exc
        parallel_version = response.headers.get("Halo-ParallelCall-Version")
        if parallel_version:
            self._parallel_call_version = parallel_version

        if response.status_code == 401 and authenticated and retry_after_unauthorized:
            self.refresh_login(force=True)
            return self._request(
                method,
                path,
                params=params,
                json_body=json_body,
                authenticated=authenticated,
                retry_after_unauthorized=False,
                wrap_transport_errors=wrap_transport_errors,
                raise_for_status=raise_for_status,
            )
        if raise_for_status and response.is_error:
            raise HaloAPIError(
                f"Halo API request failed with HTTP {response.status_code}.",
                status_code=response.status_code,
                method=method,
                path=path,
            )
        return response

    @staticmethod
    def _decode_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HaloAPIError(
                "Halo returned a response that was not valid JSON.",
                status_code=response.status_code,
            ) from exc


def _identifier(value: str) -> str:
    """Allow UUIDs/serial-like IDs while blocking accidental path injection."""

    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
    if not value or any(character not in allowed for character in value):
        raise ValueError("Halo identifiers may contain only ASCII letters, digits, and hyphens.")
    return value


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_server_time_response(response: httpx.Response) -> datetime | None:
    try:
        value = response.json()
    except ValueError:
        value = response.text.strip()
    candidates: list[Any] = [value]
    if isinstance(value, dict):
        candidates = [
            value.get(key)
            for key in (
                "serverDateTime",
                "serverTime",
                "dateTime",
                "utcDateTime",
                "date",
                "value",
            )
        ]
    for candidate in candidates:
        parsed = _parse_datetime(candidate)
        if parsed is not None:
            return parsed
    date_header = response.headers.get("Date")
    if date_header:
        try:
            return parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            pass
    return None
