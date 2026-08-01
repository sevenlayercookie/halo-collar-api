"""Synchronous client for Halo Collar REST endpoints."""

from __future__ import annotations

import json
import os
import platform
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
from .models import (
    BeaconActionType,
    BeaconCorrectionEscalationType,
    BeaconModelType,
    CorrectionRuleKindType,
    CorrectionRuleUpdate,
    CorrectionType,
    FirmwareUpdateStatus,
    TokenSet,
    WalkStopOption,
)
from .storage import StateStore

API_BASE_URL = "https://api.halocollar.com"
DEFAULT_MOBILE_ID = 2
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_UNKNOWN_PARALLEL_CALL_VERSION = "0"
_UNSET = object()


class HaloClient:
    """A conservative client for supported Halo Collar endpoints.

    Responses remain dictionaries because the server schema may change. The
    client intentionally performs no automatic network retries.
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
        # Halo's analytics, but sending it keeps requests compatible with the
        # mobile API.
        self.amplitude_session_id = amplitude_session_id or str(
            int(datetime.now(timezone.utc).timestamp() * 1000)
        )
        stored_app_version = (
            stored_auth.get("app_version") if stored_client_id == self.client_id else None
        )
        self.app_version = app_version or stored_app_version or self.profile.app_version
        # Halo assigns this when a device registers; corrections carry it. The
        # value differs per installation, so a stored one always beats the
        # constant this client falls back to.
        stored_mobile_id = settings.get("mobile_id")
        try:
            self.mobile_id = int(stored_mobile_id) if stored_mobile_id else DEFAULT_MOBILE_ID
        except (TypeError, ValueError):
            self.mobile_id = DEFAULT_MOBILE_ID
        self.api_base_url = api_base_url.rstrip("/")
        self.auth_base_url = auth_base_url
        self._parallel_call_version = _UNKNOWN_PARALLEL_CALL_VERSION
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

    def videos(self) -> list[dict[str, Any]]:
        """List the streaming videos the apps play, gathered from the configuration.

        Halo scatters these through the payload — onboarding, training, and
        subscription screens — as objects holding an HLS stream and a thumbnail.
        Neither the configuration nor the URLs are authenticated, so unlike pet
        reports and fence thumbnails these carry no signature and play without a
        login.
        """

        return _video_assets(self.configuration())

    def collars(self) -> list[dict[str, Any]]:
        """List collars owned by the authenticated account."""

        value = self._request_json("GET", "/collar/my")
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise HaloAPIError("Halo returned an unexpected collar list.")
        return value

    def collar(self, collar_id: str) -> dict[str, Any]:
        """Fetch one account collar, including its current ``petInfo`` relationship."""

        return self._get_object(f"/collar/{_identifier(collar_id)}")

    def firmware_statuses(self) -> list[dict[str, Any]]:
        """Read installed and pending firmware state for every account collar.

        Firmware rollout is server-managed. This method is a focused projection
        of :meth:`collars`; it does not initiate, cancel, or select an update.
        """

        return [_firmware_status(collar) for collar in self.collars()]

    def firmware_status(self, collar_id: str) -> dict[str, Any]:
        """Read installed and pending firmware state for one account collar."""

        return _firmware_status(self.collar(collar_id))

    @staticmethod
    def firmware_update_state(
        status: dict[str, Any],
    ) -> FirmwareUpdateStatus | str | None:
        """Return a known firmware state enum while preserving future wire values."""

        value = status.get("updateStatus")
        if value is None:
            update = status.get("firmwareUpdate")
            if isinstance(update, dict):
                update_details = update.get("update")
                if isinstance(update_details, dict):
                    value = update_details.get("status")
        if not isinstance(value, str):
            return None
        try:
            return FirmwareUpdateStatus.parse(value)
        except ValueError:
            return value

    def check_collar_binding(self, serial_number: str) -> dict[str, Any]:
        """Check whether a collar can be bound to the authenticated account.

        ``serial_number`` is the value printed on the collar. The response
        explains why binding is unavailable when its ``result`` field is false.
        """

        return self._put_object(
            "/collar/check-can-be-bound-to-user",
            json_body={"SerialNumber": _required(serial_number, "serial_number")},
        )

    def bind_collar(
        self,
        serial_number: str,
        encrypted_serial_number: str,
    ) -> dict[str, Any]:
        """Bind a physical collar to the authenticated account.

        ``encrypted_serial_number`` must come from the collar over Bluetooth;
        the printed serial number or the ``uuId`` in an account response is not
        known to be a substitute.

        This route and request shape have not yet been independently verified
        against a successful live response.
        """

        return self._put_object(
            "/collar/bind-to-user",
            json_body={
                "SerialNumber": _required(serial_number, "serial_number"),
                "EncryptedSerialNumber": _required(
                    encrypted_serial_number,
                    "encrypted_serial_number",
                ),
            },
        )

    def unbind_collar_from_user(self, collar_id: str) -> None:
        """Remove a collar from the authenticated account.

        This is not the same operation as detaching the collar from a pet. Halo's
        client calls account removal directly even for an assigned collar, but
        whether the server clears that pet relationship as a cascade has not been
        independently observed. Call :meth:`unbind_collar_from_pet` first when a
        deliberate two-stage removal is preferable.
        """

        self._request(
            "POST",
            f"/collar/{_identifier(collar_id)}/unbind-from-user",
        )

    def pet(self, pet_id: str, *, refresh_telemetry: bool = False) -> dict[str, Any]:
        """Fetch a pet, optionally asking the collar for fresher telemetry."""

        return self._get_object(
            f"/pet/{_identifier(pet_id)}",
            params={"RefreshTelemetry": str(refresh_telemetry)},
        )

    def pets(self) -> list[dict[str, Any]]:
        """List pets on the account, each with its embedded collar information."""

        value = self._request_json("GET", "/pet/my")
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise HaloAPIError("Halo returned an unexpected pet list.")
        return value

    def bind_collar_to_pet(self, pet_id: str, collar_id: str) -> None:
        """Attach an account-bound collar to a pet.

        ``collar_id`` is Halo's server-issued collar UUID, not its printed serial
        number or Bluetooth-derived encrypted serial number. A successful HTTP
        response acknowledges the request; use a refreshed :meth:`pet` read and
        :meth:`pet_collar_binding_is_synchronized` to confirm that it was applied.
        """

        self._request(
            "PUT",
            f"/pet/{_identifier(pet_id)}/bind-collar",
            json_body={"CollarId": _identifier(collar_id)},
        )

    def unbind_collar_from_pet(self, pet_id: str) -> None:
        """Detach a collar from a pet while keeping it on the account."""

        self._request("PUT", f"/pet/{_identifier(pet_id)}/unbind-collar")

    @staticmethod
    def pet_collar_binding_is_synchronized(
        pet: dict[str, Any],
        collar_id: str,
    ) -> bool:
        """Test a pet snapshot for a fully synchronized collar attachment."""

        collar_info = pet.get("collarInfo")
        return (
            isinstance(collar_info, dict)
            and str(collar_info.get("id")) == _identifier(collar_id)
            and pet.get("isCollarBindingToPetSynchronized") is True
        )

    @staticmethod
    def collar_is_assigned_to_pet(collar: dict[str, Any], pet_id: str) -> bool:
        """Test a collar snapshot for the reciprocal current pet relationship."""

        pet_info = collar.get("petInfo")
        return isinstance(pet_info, dict) and str(pet_info.get("id")) == _identifier(pet_id)

    def set_pet_fences_enabled(
        self,
        pet_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        """Set the requested containment-fence mode for one pet.

        Halo reports the accepted target under ``desiredMode`` and the collar's
        last reported state under ``telemetry.mode``. Those values may differ
        while the request is waiting to reach or be confirmed by the collar.
        Disabling fences is safety-relevant, so verify the reported state before
        relying on it. Extra response fields are preserved without interpretation.
        """

        return self._put_object(
            f"/pet/{_identifier(pet_id)}/instant-mode",
            json_body={
                "ModePatch": {
                    "FencesOn": _required_boolean(enabled, "enabled"),
                    "BeaconsOn": None,
                }
            },
        )

    def set_pet_beacons_assigned(
        self,
        pet_id: str,
        assigned: bool,
    ) -> dict[str, Any] | None:
        """Enable or disable beacon assignment for one pet.

        Beacon assignment uses a separate route from containment mode. The
        response shape has not been established, so an object is returned
        unchanged when present and an empty successful response returns
        ``None``.
        """

        path = f"/beacon/set-is-assigned/{_identifier(pet_id)}"
        response = self._request(
            "PUT",
            path,
            json_body={"IsAssigned": _required_boolean(assigned, "assigned")},
        )
        if not response.content:
            return None
        value = self._decode_json(response)
        if not isinstance(value, dict):
            raise HaloAPIError(f"Halo returned an unexpected response for {path}.")
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
        fences or pets may omit the coordinates. A pet's ``currentGeoFenceId``
        is collar-reported state, not a selection or assignment field.
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

    def register_mobile_device(
        self,
        *,
        model: str | None = None,
        manufacturer: str | None = None,
        version_string: str | None = None,
        platform_name: str | None = None,
        idiom: str = "Phone",
    ) -> int:
        """Register this installation as a device and store the ``mobileId`` Halo assigns.

        The apps call this once after login and then send the returned id as
        ``MobileId`` on every instant correction. ``InternalMobileId`` is the
        same per-installation UUID this client already sends as ``appInstanceId``
        in the Halo-Client header, so registering twice re-reads one id rather
        than accumulating devices. Corrections fall back to
        :data:`DEFAULT_MOBILE_ID` until this has been called.

        ``Platform`` follows the OAuth profile because Halo pairs it with the
        client id, but the hardware fields describe the machine actually running
        this client rather than inventing a handset. Override them if you would
        rather Halo's device list name something else.
        """

        value = self._post_object(
            "/account/mobile-data",
            json_body={
                "InternalMobileId": self.app_instance_id,
                "Model": model or platform.machine() or "unknown",
                "Manufacturer": manufacturer or platform.system() or "unknown",
                "VersionString": version_string or platform.release() or "unknown",
                "Platform": platform_name or self.profile.name,
                "Idiom": idiom,
            },
        )
        mobile_id = value.get("mobileId")
        if not isinstance(mobile_id, int) or isinstance(mobile_id, bool):
            raise HaloAPIError("Halo did not return a usable mobileId.")
        self.mobile_id = mobile_id
        self.store.update_settings(mobile_id=str(mobile_id))
        return mobile_id

    def geofences(self) -> list[dict[str, Any]]:
        """List the account's geofences, which Halo returns only on the map payload.

        Fences are account-scoped and automatically distributed to the
        account's pets. Each fence retains its ``petsSync`` entries describing
        the result for every pet independently of the telemetry-derived
        ``currentGeoFenceId``.
        """

        info = self.account_map().get("geoFencesInfo")
        if not isinstance(info, dict):
            raise HaloAPIError("Halo returned an unexpected geofence container.")
        value = info.get("geoFencesToDisplay")
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise HaloAPIError("Halo returned an unexpected geofence list.")
        return value

    def geo_fence_pet_sync(self, geo_fence_id: str) -> list[dict[str, Any]]:
        """Return automatic per-pet distribution state for one account fence.

        ``completed`` means the fence reached a collared pet; ``pending`` means
        synchronization is still in progress. Collarless pets normally report
        ``skipped``, which does not mean the account fence was unassigned.
        """

        identifier = _identifier(geo_fence_id)
        for fence in self.geofences():
            if str(fence.get("id")) != identifier:
                continue
            value = fence.get("petsSync")
            if not isinstance(value, list) or not all(
                isinstance(item, dict) for item in value
            ):
                raise HaloAPIError("Halo returned an unexpected fence pet-sync list.")
            return value
        raise HaloAPIError(f"Halo did not return fence {identifier}.")

    def walks(self, *, page: int = 1, page_size: int = 30) -> dict[str, Any]:
        """Fetch one page of completed walks."""

        return self._get_object(
            "/walk/my",
            params={
                "page": str(_positive(page, "page")),
                "pageSize": str(_positive(page_size, "page_size")),
            },
        )

    def walk_summary(self, walk_id: str) -> dict[str, Any]:
        """Fetch one completed walk, including trail-image URLs when available."""

        return self._get_object(f"/walk/{_identifier(walk_id)}/summary")

    def set_walk_paused(
        self,
        walk_id: str,
        collar_id: str,
        paused: bool,
    ) -> dict[str, Any]:
        """Ask one collar to pause or resume an existing walk.

        A ``result`` of ``success`` acknowledges the command; fresh collar
        telemetry for the same walk id is the applied-state confirmation.
        """

        return self._post_object(
            f"/walk/{_identifier(walk_id)}/set-is-paused",
            json_body={
                "CollarId": _identifier(collar_id),
                "SetWalkIsPaused": _required_boolean(paused, "paused"),
            },
        )

    def stop_walk(
        self,
        walk_id: str,
        collar_id: str,
        *,
        stop_option: WalkStopOption | str = WalkStopOption.DEFAULT,
    ) -> dict[str, Any]:
        """Ask one collar to stop participating in a walk.

        Stopping one collar does not finalize a multi-pet walk. A successful
        command is confirmed when fresh telemetry reports ``walk`` as null.
        """

        return self._post_object(
            f"/walk/{_identifier(walk_id)}/stop",
            json_body={
                "CollarId": _identifier(collar_id),
                "StopOption": WalkStopOption.parse(stop_option).value,
            },
        )

    def mark_walk_ended(
        self,
        walk_id: str,
        *,
        started_at: datetime | str,
        ended_at: datetime | str,
        pets: Sequence[dict[str, Any]],
        user: dict[str, Any],
        location_name: str | None,
    ) -> None:
        """Submit the aggregate summary for a completed walk.

        ``pets`` and ``user`` use Halo's PascalCase summary DTO fields. This
        request does not carry raw trail points; upload the rendered images
        separately. Halo declares no response object, so any successful empty
        2xx response is accepted.
        """

        pet_summaries = list(pets)
        if not pet_summaries or not all(isinstance(item, dict) for item in pet_summaries):
            raise ValueError("pets must contain at least one summary object.")
        if not isinstance(user, dict):
            raise ValueError("user must be a summary object.")
        if location_name is not None:
            _required(location_name, "location_name")
        self._request(
            "POST",
            f"/walk/{_identifier(walk_id)}/mark-ended",
            json_body={
                "StartedAt": _walk_timestamp(started_at, "started_at"),
                "EndedAt": _walk_timestamp(ended_at, "ended_at"),
                "Pets": [item.copy() for item in pet_summaries],
                "User": user.copy(),
                "LocationName": location_name,
            },
        )

    def upload_walk_trail_thumbnail(
        self,
        walk_id: str,
        image: bytes,
        *,
        filename: str = "trail-thumbnail.png",
        content_type: str = "image/png",
    ) -> None:
        """Upload the rendered overall trail thumbnail for a completed walk."""

        self._upload_walk_image(
            f"/walk/{_identifier(walk_id)}/trail-thumbnail",
            field_name="trail-thumbnail",
            image=image,
            filename=filename,
            content_type=content_type,
        )

    def upload_walk_pet_trail_image(
        self,
        walk_id: str,
        pet_id: str,
        image: bytes,
        *,
        filename: str = "trail-image.png",
        content_type: str = "image/png",
    ) -> None:
        """Upload one pet's rendered trail image for a completed walk."""

        self._upload_walk_image(
            f"/walk/{_identifier(walk_id)}/pet/{_identifier(pet_id)}/trail-image",
            field_name="trail-image",
            image=image,
            filename=filename,
            content_type=content_type,
        )

    def _upload_walk_image(
        self,
        path: str,
        *,
        field_name: str,
        image: bytes,
        filename: str,
        content_type: str,
    ) -> None:
        self._request(
            "PUT",
            path,
            files={
                field_name: (
                    _required(filename, "filename"),
                    _required_bytes(image, "image"),
                    _required(content_type, "content_type"),
                )
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

    def update_correction_rules(
        self,
        items: Sequence[CorrectionRuleUpdate],
    ) -> dict[str, Any]:
        """Update only the identified persistent correction rules.

        Each rule ID already identifies its pet and escalation slot. Omitted
        rules are left alone; this is an item-level batch update despite the
        route using ``PUT``. Read the valid levels and asset IDs from
        :meth:`correction_rule_configuration` rather than hard-coding them.
        """

        if not items:
            raise ValueError("Pass at least one correction rule to update.")
        body_items = [_correction_rule_update_body(item) for item in items]
        rule_ids = [item["CorrectionRuleId"] for item in body_items]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("Each correction rule may appear only once in an update.")
        return self._put_object(
            "/correction-rule",
            json_body={"Items": body_items},
        )

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
        until it next syncs. ``currentGeoFenceId`` may appear in Halo's response
        but is reported state and is deliberately not sent in this request.
        """

        return self._put_object(
            f"/pet/{_identifier(pet_id)}",
            json_body=_pet_body(name, color_hex, breed, birthday, weight_kg),
        )

    def delete_pet(self, pet_id: str) -> None:
        """Delete a pet and everything Halo keeps under it.

        Halo answers 200 with an empty body rather than returning the pet, so
        its history is not recoverable from here. Confirm with the owner first.
        """

        self._request("DELETE", f"/pet/{_identifier(pet_id)}")

    def geo_fence_safe_zones(
        self,
        location_points: Sequence[tuple[float, float]],
        *,
        analytics: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Preview the safe zone Halo derives from a drawn boundary.

        The submitted points describe the warning boundary; Halo returns the
        generated safe-zone geometry. This is a preview and changes no fence.
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
        before saving by calling :meth:`geo_fence_safe_zones` first. The
        response's ``geoFence.petsSync`` entries report which pets Halo assigned
        automatically and whether each collar has synchronized.
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
        """Rename a fence without touching its boundary.

        Halo answers HTTP 200 with an empty body.
        """

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
        collar syncs, so a mistake here can leave a dog uncontained. Halo
        normally answers ``{"status": "success"}`` without returning geometry.
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
        """Fetch the authenticated account profile and completion flags."""

        return self._get_object("/user-profile")

    def update_profile_name(self, first_name: str, last_name: str) -> dict[str, Any] | None:
        """Replace the only editable profile fields: first and last name."""

        return self._put_optional_object(
            "/user-profile",
            json_body={
                "FirstName": _required(first_name, "first_name"),
                "LastName": _required(last_name, "last_name"),
            },
        )

    def upload_profile_avatar(
        self,
        image: bytes,
        *,
        filename: str = "avatar.png",
        content_type: str = "image/png",
    ) -> None:
        """Upload an account avatar through the ``icon`` multipart field."""

        self._request(
            "PUT",
            "/user-profile/me/icon",
            files={
                "icon": (
                    _required(filename, "filename"),
                    _required_bytes(image, "image"),
                    _required(content_type, "content_type"),
                )
            },
        )

    def delete_profile_avatar(self) -> None:
        """Remove the current account avatar."""

        self._request("DELETE", "/user-profile/me/icon")

    def onboarding_progress(self) -> dict[str, Any]:
        """Fetch Halo's versioned onboarding progress object."""

        return self._get_object("/user-profile/onboarding/progress")

    def update_onboarding_progress(
        self,
        *,
        version: int,
        steps: Sequence[str | dict[str, Any]],
        progress_state: str,
    ) -> dict[str, Any]:
        """Save versioned onboarding progress and return its normalized response.

        Halo rejects stale versions rather than merging them. Reload
        :meth:`onboarding_progress` before retrying a conflict.
        """

        return self._put_object(
            "/user-profile/onboarding/progress",
            json_body={
                "Version": _nonnegative_integer(version, "version"),
                "Steps": _onboarding_steps(steps),
                "ProgressState": _required(progress_state, "progress_state"),
            },
        )

    def questionnaire(self) -> dict[str, Any]:
        """Fetch the saved account questionnaire.

        Halo represents an absent questionnaire as an API error, not an empty
        object, so a successful read is the completion check.
        """

        return self._get_object("/user-profile/questionnaire")

    def save_questionnaire(self, questionnaire: dict[str, Any]) -> dict[str, Any] | None:
        """Save a complete PascalCase ``UserQuestionnaireDto`` object."""

        if not isinstance(questionnaire, dict) or not questionnaire:
            raise ValueError("questionnaire must be a non-empty object.")
        return self._put_optional_object(
            "/user-profile/questionnaire",
            json_body=questionnaire,
        )

    def check_user_can_change_email(self, email: str) -> None:
        """Check whether an address is eligible for an email-change request."""

        self._request(
            "POST",
            "/account/check-user-can-change-email",
            json_body={"Email": _required(email, "email")},
        )

    def request_email_change(self, email: str) -> None:
        """Start an email change and send its confirmation code to ``email``."""

        self._request(
            "POST",
            "/account/email-change-request",
            json_body={"Email": _required(email, "email")},
        )

    def confirm_email_change(self, code: str) -> None:
        """Confirm a pending email change with the emailed code."""

        self._request(
            "POST",
            "/account/email-change-request/confirm",
            json_body={"Code": _required(code, "code")},
        )

    def resend_email_change_confirmation(self) -> None:
        """Resend a pending email-change confirmation message."""

        self._request("POST", "/account/email-change-request/resend-email")

    def cancel_email_change(self) -> str:
        """Restore or cancel the pending email change and return Halo's message."""

        value = self._request_json("PUT", "/account/email-change-request")
        if not isinstance(value, str):
            raise HaloAPIError("Halo returned an unexpected email-change cancellation response.")
        return value

    def delete_account(self) -> None:
        """Permanently delete the authenticated Halo account."""

        self._request("DELETE", "/account")

    def beacons(self) -> dict[str, Any] | list[Any]:
        """Return account beacons and the server's available range configuration."""

        value = self._request_json("GET", "/beacon/my")
        if not isinstance(value, (dict, list)):
            raise HaloAPIError("Halo returned an unexpected beacon response.")
        return value

    def beacon_name_is_available(
        self,
        name: str,
        *,
        beacon_id: str | None = None,
    ) -> bool:
        """Check a new or existing beacon name for a server-side conflict."""

        return self._name_is_available(
            "/beacon/check-name-uniqueness",
            name,
            beacon_id,
        )

    def check_beacon_binding(self, serial_number: str) -> dict[str, Any]:
        """Check whether a physical beacon serial can be bound to this account."""

        return self._put_object(
            "/beacon/check-can-be-bound-to-user",
            json_body={"SerialNumber": _required(serial_number, "serial_number")},
        )

    def add_beacon(
        self,
        *,
        name: str,
        serial_number: str,
        model_type: BeaconModelType | str,
        action_type: BeaconActionType | str,
        should_notify: bool,
        beacon_range: dict[str, Any] | None = None,
        is_enabled: bool | None = None,
        transmission_rate_milliseconds: int | None = None,
        correction_escalation_type: BeaconCorrectionEscalationType | str | None = None,
        pet_id: str | None = None,
    ) -> dict[str, Any]:
        """Add or bind a physical beacon and return its complete server object."""

        return self._post_object(
            "/beacon",
            json_body={
                "Name": _required(name, "name"),
                "SerialNumber": _required(serial_number, "serial_number"),
                "ModelType": BeaconModelType.parse(model_type).value,
                "Range": _beacon_range(beacon_range),
                "IsEnabled": _nullable_boolean(is_enabled, "is_enabled"),
                "ActionType": BeaconActionType.parse(action_type).value,
                "ShouldNotify": _required_boolean(should_notify, "should_notify"),
                "TransmissionRateMilliseconds": _nullable_positive_integer(
                    transmission_rate_milliseconds,
                    "transmission_rate_milliseconds",
                ),
                "CorrectionEscalationType": (
                    BeaconCorrectionEscalationType.parse(
                        correction_escalation_type
                    ).value
                    if correction_escalation_type is not None
                    else None
                ),
                "PetId": _nullable_identifier(pet_id, "pet_id"),
            },
        )

    def update_beacon(
        self,
        beacon_id: str,
        *,
        name: Any = _UNSET,
        is_enabled: Any = _UNSET,
        action_type: Any = _UNSET,
        should_notify: Any = _UNSET,
        beacon_range: Any = _UNSET,
        model_type: Any = _UNSET,
        transmission_rate_milliseconds: Any = _UNSET,
        correction_escalation_type: Any = _UNSET,
        pet_id: Any = _UNSET,
    ) -> dict[str, Any]:
        """Update only the supplied beacon settings.

        Every server field is nullable. Omitting a keyword leaves it out of the
        request, while explicitly passing ``None`` sends JSON null.
        """

        body: dict[str, Any] = {}
        if name is not _UNSET:
            body["Name"] = _nullable_required(name, "name")
        if is_enabled is not _UNSET:
            body["IsEnabled"] = _nullable_boolean(is_enabled, "is_enabled")
        if action_type is not _UNSET:
            body["ActionType"] = (
                BeaconActionType.parse(action_type).value
                if action_type is not None
                else None
            )
        if should_notify is not _UNSET:
            body["ShouldNotify"] = _nullable_boolean(should_notify, "should_notify")
        if beacon_range is not _UNSET:
            body["Range"] = _beacon_range(beacon_range)
        if model_type is not _UNSET:
            body["ModelType"] = (
                BeaconModelType.parse(model_type).value if model_type is not None else None
            )
        if transmission_rate_milliseconds is not _UNSET:
            body["TransmissionRateMilliseconds"] = _nullable_positive_integer(
                transmission_rate_milliseconds,
                "transmission_rate_milliseconds",
            )
        if correction_escalation_type is not _UNSET:
            body["CorrectionEscalationType"] = (
                BeaconCorrectionEscalationType.parse(
                    correction_escalation_type
                ).value
                if correction_escalation_type is not None
                else None
            )
        if pet_id is not _UNSET:
            body["PetId"] = _nullable_identifier(pet_id, "pet_id")
        if not body:
            raise ValueError("Pass at least one beacon field to update.")
        return self._put_object(
            f"/beacon/{_identifier(beacon_id)}",
            json_body=body,
        )

    def delete_beacon(self, beacon_id: str) -> None:
        """Delete or unbind one beacon server record."""

        self._request("DELETE", f"/beacon/{_identifier(beacon_id)}")

    def upload_beacon_telemetry(
        self,
        readings: Sequence[dict[str, Any]],
    ) -> None:
        """Upload battery observations discovered locally by the phone."""

        normalized = []
        for reading in readings:
            if not isinstance(reading, dict):
                raise ValueError("Beacon telemetry readings must be objects.")
            normalized.append(
                {
                    "SerialNumber": _required(
                        reading.get("SerialNumber"),
                        "SerialNumber",
                    ),
                    "BatteryChargePercent": _percentage(
                        reading.get("BatteryChargePercent"),
                        "BatteryChargePercent",
                    ),
                }
            )
        if not normalized:
            raise ValueError("At least one beacon telemetry reading is required.")
        self._request(
            "PUT",
            "/beacon/telemetry",
            json_body={"BeaconsTelemetry": normalized},
        )

    def beacon_pet_sync(self, beacon_id: str) -> list[dict[str, Any]]:
        """Return per-pet distribution state for one account beacon."""

        identifier = _identifier(beacon_id)
        payload = self.beacons()
        if not isinstance(payload, dict):
            raise HaloAPIError("Halo returned an unexpected beacon collection.")
        beacons = payload.get("beacons")
        if not isinstance(beacons, list):
            raise HaloAPIError("Halo returned an unexpected beacon list.")
        for beacon in beacons:
            if not isinstance(beacon, dict) or str(beacon.get("id")) != identifier:
                continue
            value = beacon.get("petsSync")
            if not isinstance(value, list) or not all(
                isinstance(item, dict) for item in value
            ):
                raise HaloAPIError("Halo returned an unexpected beacon pet-sync list.")
            return value
        raise HaloAPIError(f"Halo did not return beacon {identifier}.")

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

    def test_correction_on_collar(
        self,
        pet_id: str,
        kind_type: CorrectionRuleKindType | str,
        *,
        sound_id: str | None = None,
        vibration_id: str | None = None,
        sound_intensity_level: int | None = None,
        shock_intensity_level: int | None = None,
        command_number: int | None = None,
        expiration_seconds: int = 30,
        require_online: bool = True,
    ) -> dict[str, Any]:
        """Test proposed feedback directly on a pet's collar exactly once.

        This does not save a persistent correction rule. The command counter is
        reserved before dispatch and transport failures are never retried because
        the collar may already have acted. The 30-second expiry is a conservative
        client default and callers may override it explicitly.
        """

        pet_identifier = _identifier(pet_id)
        kind = CorrectionRuleKindType.parse(kind_type)
        expires_in = _positive(expiration_seconds, "expiration_seconds")
        sound = _nullable_identifier(sound_id, "sound_id")
        vibration = _nullable_identifier(vibration_id, "vibration_id")
        sound_level = _nullable_positive_integer(
            sound_intensity_level,
            "sound_intensity_level",
        )
        shock_level = _nullable_positive_integer(
            shock_intensity_level,
            "shock_intensity_level",
        )
        if kind is CorrectionRuleKindType.SOUND:
            _validate_correction_modality(
                kind,
                level=sound_level,
                sound_id=sound,
                vibration_id=vibration,
            )
            if shock_level is not None:
                raise ValueError("Sound collar tests cannot include shock_intensity_level.")
        elif kind is CorrectionRuleKindType.VIBRATION:
            _validate_correction_modality(
                kind,
                level=sound_level,
                sound_id=sound,
                vibration_id=vibration,
            )
            if shock_level is not None:
                raise ValueError("Vibration collar tests cannot include shock_intensity_level.")
        else:
            _validate_correction_modality(
                kind,
                level=shock_level,
                sound_id=sound,
                vibration_id=vibration,
            )
            if sound_level is not None:
                raise ValueError("Shock collar tests cannot include sound_intensity_level.")

        if require_online:
            collar = self.collar_for_pet(pet_identifier)
            if not self.collar_is_online(collar):
                raise UnsafeCorrectionError(
                    "Halo does not report this collar as socket-connected over Wi-Fi or cellular."
                )
        server_now = self.server_time()
        reserved_number = self.store.reserve_command_number(
            pet_identifier,
            command_number,
        )
        expiration = server_now + timedelta(seconds=expires_in)
        body = {
            "MobileId": self.mobile_id,
            "CommandNumber": reserved_number,
            "PetId": pet_identifier,
            "KindType": kind.value,
            "SoundId": sound,
            "VibrationId": vibration,
            "SoundIntensityLevel": sound_level,
            "ShockIntensityLevel": shock_level,
            "ExpirationDate": _format_utc(expiration),
        }
        try:
            response = self._request(
                "PUT",
                "/correction-rule/test-on-collar",
                json_body=body,
                wrap_transport_errors=False,
                raise_for_status=False,
            )
        except httpx.HTTPError as exc:
            raise CorrectionOutcomeUnknownError(
                "The collar-test request encountered a network error after dispatch. "
                "It was not retried; its delivery is unknown."
            ) from exc

        value = self._decode_json(response)
        if not isinstance(value, dict):
            raise HaloAPIError(
                "Halo returned an unexpected collar-test response.",
                status_code=response.status_code,
                method="PUT",
                path="/correction-rule/test-on-collar",
            )
        if str(value.get("result", "")).casefold() == "oldcommandnumber":
            current = value.get("currentCommandNumber")
            current_number = current if isinstance(current, int) else None
            if current_number is not None:
                self.store.reconcile_command_number(pet_identifier, current_number)
            raise StaleCommandNumberError(current_number, value)
        if response.is_error:
            raise HaloAPIError(
                f"Halo API collar-test request failed with HTTP {response.status_code}.",
                status_code=response.status_code,
                method="PUT",
                path="/correction-rule/test-on-collar",
            )
        if str(value.get("result", "")).casefold() != "success":
            raise HaloAPIError(
                f"Halo did not accept the collar test (result={value.get('result', 'unknown')!r}).",
                status_code=response.status_code,
                method="PUT",
                path="/correction-rule/test-on-collar",
            )
        return value

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
            "MobileId": self.mobile_id,
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

    def _put_optional_object(self, path: str, **kwargs: Any) -> dict[str, Any] | None:
        response = self._request("PUT", path, **kwargs)
        if not response.content:
            return None
        value = self._decode_json(response)
        if not isinstance(value, dict):
            raise HaloAPIError(f"Halo returned an unexpected response for {path}.")
        return value

    def _mutate_object(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        value = self._request_json(method, path, **kwargs)
        if not isinstance(value, dict):
            raise HaloAPIError(f"Halo returned an unexpected response for {path}.")
        return value

    def _ensure_parallel_call_version(self) -> None:
        """Learn Halo's current parallel-call version before mutating anything.

        Halo increments this as account state changes and rejects a mutation
        carrying a stale one with HTTP 400 ``errorCode 3001``, so a client whose
        first request is a write would always fail. Reading the clock is the
        cheapest way to be told the current value. The rejection happens before
        Halo acts, but this preflight avoids relying on that: no write is sent
        twice.
        """

        if self._parallel_call_version != _UNKNOWN_PARALLEL_CALL_VERSION:
            return
        self._request("GET", "/system/server-date-time", raise_for_status=False)

    def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._decode_json(self._request(method, path, **kwargs))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        authenticated: bool = True,
        retry_after_unauthorized: bool = True,
        wrap_transport_errors: bool = True,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        method = method.upper()
        if authenticated:
            self.refresh_login()
            if method in _MUTATING_METHODS:
                self._ensure_parallel_call_version()
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
                files=files,
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
                files=files,
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


def _video_assets(configuration: Any) -> list[dict[str, Any]]:
    """Find every ``videoStreamUrl`` in the configuration, wherever Halo moves it."""

    found: list[dict[str, Any]] = []

    def walk(node: Any, trail: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            stream = node.get("videoStreamUrl")
            if isinstance(stream, str) and stream:
                found.append(
                    {
                        "name": trail[-1] if trail else "",
                        "section": ".".join(trail[:-1]),
                        "videoStreamUrl": stream,
                        "thumbnailUrl": node.get("thumbnailUrl"),
                    }
                )
            for key, value in node.items():
                walk(value, (*trail, key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, (*trail, str(index)))

    walk(configuration, ())
    return found


def _firmware_status(collar: dict[str, Any]) -> dict[str, Any]:
    update = collar.get("firmwareUpdate")
    update_details = update.get("update") if isinstance(update, dict) else None
    update_status = (
        update_details.get("status") if isinstance(update_details, dict) else None
    )
    return {
        "collarId": collar.get("id"),
        "serialNumber": collar.get("serialNumber"),
        "collarType": collar.get("type"),
        "firmware": collar.get("firmware"),
        "hasFirmwareUpdatesAvailable": collar.get("hasFirmwareUpdatesAvailable"),
        "firmwareUpdate": update,
        "updateStatus": update_status,
    }


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


def _nonnegative_integer(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return value


def _onboarding_steps(steps: Sequence[str | dict[str, Any]]) -> list[dict[str, str]]:
    if isinstance(steps, (str, bytes)) or not isinstance(steps, Sequence):
        raise ValueError("steps must be a sequence of step IDs or objects.")
    result = []
    for index, step in enumerate(steps):
        if isinstance(step, str):
            step_id = step
        elif isinstance(step, dict):
            step_id = step.get("Id", step.get("id"))
        else:
            raise ValueError(f"steps[{index}] must be a step ID or object.")
        result.append({"Id": _required(step_id, f"steps[{index}].id")})
    return result


def _required_boolean(value: bool, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean.")
    return value


def _nullable_required(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _required(value, name)


def _nullable_boolean(value: Any, name: str) -> bool | None:
    if value is None:
        return None
    return _required_boolean(value, name)


def _nullable_positive_integer(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _positive(value, name)


def _nullable_identifier(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a Halo identifier or None.")
    return _identifier(value)


def _correction_rule_update_body(item: CorrectionRuleUpdate) -> dict[str, Any]:
    if not isinstance(item, CorrectionRuleUpdate):
        raise ValueError("items must contain CorrectionRuleUpdate values.")
    kind = CorrectionRuleKindType.parse(item.kind_type)
    level = _nullable_positive_integer(item.level, "level")
    sound_id = _nullable_identifier(item.sound_id, "sound_id")
    vibration_id = _nullable_identifier(item.vibration_id, "vibration_id")
    _validate_correction_modality(
        kind,
        level=level,
        sound_id=sound_id,
        vibration_id=vibration_id,
    )
    return {
        "CorrectionRuleId": _identifier(item.correction_rule_id),
        "KindType": kind.value,
        "Level": level,
        "SoundId": sound_id,
        "VibrationId": vibration_id,
    }


def _validate_correction_modality(
    kind: CorrectionRuleKindType,
    *,
    level: int | None,
    sound_id: str | None,
    vibration_id: str | None,
) -> None:
    if kind is CorrectionRuleKindType.SOUND:
        if sound_id is None:
            raise ValueError("Sound correction settings require sound_id.")
        if vibration_id is not None:
            raise ValueError("Sound correction settings cannot include vibration_id.")
    elif kind is CorrectionRuleKindType.VIBRATION:
        if vibration_id is None:
            raise ValueError("Vibration correction settings require vibration_id.")
        if sound_id is not None or level is not None:
            raise ValueError("Vibration correction settings cannot include sound_id or level.")
    else:
        if level is None:
            raise ValueError("Shock correction settings require level.")
        if sound_id is not None or vibration_id is not None:
            raise ValueError("Shock correction settings cannot include sound_id or vibration_id.")


def _integer(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer.")
    return value


def _percentage(value: Any, name: str) -> int:
    result = _integer(value, name)
    if not 0 <= result <= 100:
        raise ValueError(f"{name} must be between 0 and 100.")
    return result


def _beacon_range(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("beacon_range must be an object or None.")
    if "Level" not in value or "RadiusInDecibel" not in value:
        raise ValueError("beacon_range requires Level and RadiusInDecibel.")
    return {
        "Level": _positive(value["Level"], "beacon_range.Level"),
        "RadiusInDecibel": _integer(
            value["RadiusInDecibel"],
            "beacon_range.RadiusInDecibel",
        ),
    }


def _required_bytes(value: bytes, name: str) -> bytes:
    if not isinstance(value, bytes) or not value:
        raise ValueError(f"{name} must be non-empty bytes.")
    return value


def _walk_timestamp(value: datetime | str, name: str) -> str:
    if isinstance(value, datetime):
        return _format_utc(value)
    return _required(value, name)


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
        # Halo accepts seconds precision and normalizes the value to UTC in its
        # response.
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
