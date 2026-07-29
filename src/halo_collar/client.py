"""Synchronous client for the observed Halo Collar REST endpoints."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Sequence
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
        amplitude_session_id: str | None = None,
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
        # The apps send the session's start time in milliseconds. It only feeds
        # Halo's analytics, but sending it keeps requests shaped like the client
        # whose traffic this library was modelled on.
        self.amplitude_session_id = amplitude_session_id or str(
            int(datetime.now(timezone.utc).timestamp() * 1000)
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

    def pets(self) -> list[dict[str, Any]]:
        """List pets on the account, each with its embedded collar information."""

        value = self._request_json("GET", "/pet/my")
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise HaloAPIError("Halo returned an unexpected pet list.")
        return value

    def account_map(
        self,
        latitude: float | None = None,
        longitude: float | None = None,
        *,
        refresh_telemetry: bool = False,
        max_corrections_count: int = 20,
    ) -> dict[str, Any]:
        """Fetch the aggregate view the app renders on its map screen.

        One response returns ``pets`` (each with its collar embedded),
        ``geoFencesInfo``, and ``corrections``, so prefer this over several
        separate calls when polling. The apps always send a viewport centre, but
        Halo returns the whole account without one, so callers that only want
        fences or pets may omit the coordinates.
        """

        if (latitude is None) != (longitude is None):
            raise ValueError("Pass both latitude and longitude, or neither.")
        params = {
            "RefreshTelemetry": str(refresh_telemetry),
            "MaxCorrectionsCount": str(_positive(max_corrections_count, "max_corrections_count")),
        }
        if latitude is not None and longitude is not None:
            params["viewport.center.latitude"] = str(float(latitude))
            params["viewport.center.longitude"] = str(float(longitude))
        return self._get_object("/account/my/map", params=params)

    def geofences(self) -> list[dict[str, Any]]:
        """List the account's geofences, which Halo returns only on the map payload."""

        info = self.account_map().get("geoFencesInfo")
        if not isinstance(info, dict):
            raise HaloAPIError("Halo returned an unexpected geofence container.")
        value = info.get("geoFencesToDisplay")
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise HaloAPIError("Halo returned an unexpected geofence list.")
        return value

    def walks(self, *, page: int = 1, page_size: int = 30) -> dict[str, Any]:
        """Fetch one page of recorded walks."""

        return self._get_object(
            "/walk/my",
            params={
                "page": str(_positive(page, "page")),
                "pageSize": str(_positive(page_size, "page_size")),
            },
        )

    def notifications(self, *, page: int = 1, page_size: int = 30) -> dict[str, Any]:
        """Fetch one page of the account's notification history.

        Halo capitalizes this endpoint's paging parameters even though the
        otherwise identical ``/walk/my`` envelope uses lowercase ones.
        """

        return self._get_object(
            "/notification/my/query",
            params={
                "Page": str(_positive(page, "page")),
                "PageSize": str(_positive(page_size, "page_size")),
            },
        )

    def mapbox_requests(self) -> dict[str, Any] | list[Any]:
        value = self._request_json("GET", "/mapbox/request/my")
        if not isinstance(value, (dict, list)):
            raise HaloAPIError("Halo returned an unexpected mapbox request response.")
        return value

    def pet_colors(self) -> list[dict[str, Any]]:
        """List the collar colors that may be assigned to a pet."""

        value = self._request_json("GET", "/pet/colors")
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise HaloAPIError("Halo returned an unexpected pet color list.")
        return value

    def correction_rule_configuration(self) -> dict[str, Any]:
        """Fetch the sound/vibration intensity ladders corrections are built from."""

        return self._get_object("/correction-rule/configuration-v2")

    def pet_correction_rules(self, pet_id: str) -> dict[str, Any]:
        """Fetch the configured correction rules for one pet."""

        return self._get_object(f"/pet/{_identifier(pet_id)}/correction-rules")

    def training(self) -> dict[str, Any]:
        """Fetch training course progress for the account."""

        return self._get_object("/training/my-v2")

    def training_course_link(self, curriculum_id: str, course_name: str) -> str:
        """Get a one-time launch URL for a training course.

        Halo hosts the courses on SCORM Cloud rather than serving them itself,
        so this returns an external URL as a bare JSON string.
        """

        path = (
            f"/training/user/course-launch-link/"
            f"{_identifier(curriculum_id)}/{_identifier(course_name)}"
        )
        value = self._request_json("GET", path)
        if not isinstance(value, str) or not value:
            raise HaloAPIError("Halo returned an unexpected course launch link.")
        return value

    def set_notification_status(
        self,
        notification_ids: Sequence[str],
        *,
        status: str = "Read",
    ) -> None:
        """Mark notifications read or unread."""

        identifiers = [_identifier(item) for item in notification_ids]
        if not identifiers:
            raise ValueError("At least one notification id is required.")
        self._request(
            "PUT",
            "/notification/status",
            json_body={"Ids": identifiers, "Status": _required(status, "status")},
        )

    def generate_ecommerce_login_magic_code(self) -> dict[str, Any]:
        """Mint a single-use code that signs this account into the Halo store.

        The returned value is a credential: anyone holding it can act as this
        account in the store, so treat it like a password and do not log it.
        """

        return self._post_object("/account/generate-ecommerce-login-magic-code")

    def lookup_parcels(
        self,
        latitude: float,
        longitude: float,
        *,
        page: int = 1,
        results_per_page: int = 1,
    ) -> dict[str, Any]:
        """Look up public land-parcel records at a point, as the fence editor does.

        Halo proxies a third-party property database here. Responses contain
        real-world owner names and mailing addresses for whoever owns the land,
        so avoid storing or printing them casually. The envelope's ``body`` is a
        JSON-encoded *string* that must be parsed a second time.
        """

        return self._post_object(
            "/report-all/api/parcels",
            json_body={
                "spatial_intersect": (
                    f"POINT({_coordinate(longitude, 'longitude', 180.0)} "
                    f"{_coordinate(latitude, 'latitude', 90.0)})"
                ),
                "rpp": _positive(results_per_page, "results_per_page"),
                "page": _positive(page, "page"),
                "si_srid": 4326,
                "v": 8,
            },
        )

    def find_collar(self, collar_id: str) -> None:
        """Ask a collar to play its locate tone.

        This is a physical action on the collar, but an audible-only one; unlike
        a correction it is not aversive, so no command number is reserved. Halo
        answers ``204 No Content``.
        """

        self._request("PUT", f"/collar/{_identifier(collar_id)}/find")

    def pet_name_is_available(self, name: str, *, pet_id: str | None = None) -> bool:
        """Return whether a pet name is free.

        Pass ``pet_id`` when renaming so the pet does not collide with itself.
        """

        return self._name_is_available("/pet/check-name-uniqueness", name, pet_id)

    def add_pet(
        self,
        *,
        name: str,
        color_hex: str,
        breed: str,
        birthday: datetime | str,
        weight_kg: float,
    ) -> dict[str, Any]:
        """Create a pet.

        The new pet has no collar; Halo returns it with ``collarInfo`` null until
        one is bound. ``color_hex`` must be one of :meth:`pet_colors`.
        """

        return self._post_object(
            "/pet/add",
            json_body=_pet_body(name, color_hex, breed, birthday, weight_kg),
        )

    def update_pet(
        self,
        pet_id: str,
        *,
        name: str,
        color_hex: str,
        breed: str,
        birthday: datetime | str,
        weight_kg: float,
    ) -> dict[str, Any]:
        """Replace one pet's profile.

        Halo treats this as a full replacement rather than a patch, so every
        field is required; read :meth:`pet` first and pass back the values you
        are not changing. Saving marks the collar's configuration ``outdated``
        until it next syncs.
        """

        return self._put_object(
            f"/pet/{_identifier(pet_id)}",
            json_body=_pet_body(name, color_hex, breed, birthday, weight_kg),
        )

    def geo_fence_safe_zones(
        self,
        location_points: Sequence[tuple[float, float]],
        *,
        analytics: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Preview the safe zone Halo derives from a drawn boundary.

        The app calls this while the user drags fence points, before saving.
        """

        value = self._request_json(
            "POST",
            "/geo-fence/safe-zones",
            json_body={
                "LocationPoints": _location_points(location_points),
                "Analytics": analytics,
            },
        )
        if not isinstance(value, list):
            raise HaloAPIError("Halo returned an unexpected safe-zone response.")
        return value

    def geo_fence_name_is_available(
        self,
        name: str,
        *,
        geo_fence_id: str | None = None,
    ) -> bool:
        """Return whether a fence name is free.

        Pass ``geo_fence_id`` when renaming so the fence does not collide with
        its own current name.
        """

        return self._name_is_available(
            "/geo-fence/check-name-uniqueness",
            name,
            geo_fence_id,
        )

    def add_geo_fence(
        self,
        name: str,
        location_points: Sequence[tuple[float, float]],
        *,
        public_visibility_type: str = "Private",
        analytics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a containment fence.

        This changes where the collar will correct the dog. Verify the boundary
        before saving; the app calls :meth:`geo_fence_safe_zones` first.
        """

        return self._post_object(
            "/geo-fence/add",
            json_body={
                "Name": _required(name, "name"),
                "LocationPoints": _location_points(location_points),
                "PublicVisibilityType": public_visibility_type,
                "Analytics": analytics,
            },
        )

    def rename_geo_fence(self, geo_fence_id: str, name: str) -> None:
        """Rename a fence without touching its boundary."""

        self._request(
            "PUT",
            f"/geo-fence/{_identifier(geo_fence_id)}",
            json_body={"Name": _required(name, "name")},
        )

    def update_geo_fence_location(
        self,
        geo_fence_id: str,
        location_points: Sequence[tuple[float, float]],
        *,
        analytics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Move a fence's boundary.

        The new boundary replaces the old one outright and takes effect once the
        collar syncs, so a mistake here can leave a dog uncontained.
        """

        return self._put_object(
            f"/geo-fence/{_identifier(geo_fence_id)}/location",
            json_body={
                "LocationPoints": _location_points(location_points),
                "Analytics": analytics,
            },
        )

    def delete_geo_fence(self, geo_fence_id: str) -> dict[str, Any]:
        """Delete a containment fence.

        Removing the boundary that keeps a dog contained is destructive and is
        not reversible from this client; Halo does not return the deleted
        geometry, so re-drawing it is manual. Confirm with the owner first.
        """

        return self._request_json("DELETE", f"/geo-fence/{_identifier(geo_fence_id)}")

    def subscribe_push_notifications(
        self,
        device_handle: str,
        *,
        platform_type: str = "Android",
    ) -> None:
        """Register a push token so Halo delivers notifications to it."""

        self._request(
            "PUT",
            "/push-notification/subscribe",
            json_body={
                "PlatformType": platform_type,
                "DeviceHandle": _required(device_handle, "device_handle"),
            },
        )

    def unsubscribe_push_device(self, device_handle: str) -> None:
        """Stop Halo from delivering push notifications to one device token."""

        self._request(
            "PUT",
            "/push-notification/unsubscribe-device",
            json_body={"DeviceHandle": _required(device_handle, "device_handle")},
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

    def _name_is_available(self, path: str, name: str, identifier: str | None) -> bool:
        """Ask one of Halo's name-uniqueness endpoints, which answer 204 or 409."""

        response = self._request(
            "PUT",
            path,
            json_body={
                "Id": _identifier(identifier) if identifier is not None else None,
                "Name": _required(name, "name"),
            },
            raise_for_status=False,
        )
        if response.status_code == 409:
            return False
        if response.is_error:
            raise HaloAPIError(
                f"Halo API request failed with HTTP {response.status_code}.",
                status_code=response.status_code,
                method="PUT",
                path=path,
            )
        return True

    def _post_object(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self._mutate_object("POST", path, **kwargs)

    def _put_object(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return self._mutate_object("PUT", path, **kwargs)

    def _mutate_object(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        value = self._request_json(method, path, **kwargs)
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
            "Halo-Amplitude-SessionId": self.amplitude_session_id,
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


def _positive(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be an integer of at least 1.")
    return value


def _pet_body(
    name: str,
    color_hex: str,
    breed: str,
    birthday: datetime | str,
    weight_kg: float,
) -> dict[str, Any]:
    """Build the profile body shared by pet creation and replacement."""

    return {
        "Name": _required(name, "name"),
        "ColorHex": _required(color_hex, "color_hex"),
        "Breed": _required(breed, "breed"),
        # Seconds precision, matching the captured apps; Halo normalizes the
        # value to UTC in its response either way.
        "Birthday": (
            birthday.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if isinstance(birthday, datetime)
            else birthday
        ),
        "WeightKg": float(weight_kg),
    }


def _coordinate(value: float, name: str, limit: float) -> float:
    number = float(value)
    if number != number or abs(number) > limit:
        raise ValueError(f"{name} must be a real number within +/-{limit:g} degrees.")
    return number


def _location_points(points: Sequence[tuple[float, float]]) -> list[dict[str, float]]:
    """Convert (latitude, longitude) pairs into Halo's boundary point objects.

    A fence is an area, so Halo needs at least three corners; sending fewer
    would define no enclosure at all.
    """

    result = [
        {
            "Latitude": _coordinate(latitude, "latitude", 90.0),
            "Longitude": _coordinate(longitude, "longitude", 180.0),
        }
        for latitude, longitude in points
    ]
    if len(result) < 3:
        raise ValueError("A fence boundary needs at least three location points.")
    return result


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required.")
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
