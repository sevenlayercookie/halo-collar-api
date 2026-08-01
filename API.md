# Halo Collar Python API reference

> [!IMPORTANT]
> This is an unofficial client. It is not affiliated with, endorsed by, or
> supported by Halo Collar. Upstream interfaces may change without notice.

This document describes the public Python API, live SignalR API, persistence
model, exceptions, and supported HTTP routes in `halo-collar` 0.1.0. See the
[README](README.md) for installation, CLI usage, safety guidance, and the
[roadmap](README.md#roadmap).

## Installation

Python 3.10 or newer is required.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

The package installs the `halo` command and exposes its Python interfaces from
`halo_collar`.

## Public imports

```python
from halo_collar import (
    ANDROID_CLIENT_SECRET,
    ANDROID_PROFILE,
    IOS_CLIENT_SECRET,
    IOS_PROFILE,
    AuthenticationError,
    AuthorizationFlow,
    BeaconActionType,
    BeaconCorrectionEscalationType,
    BeaconModelType,
    CommandCounterUnknownError,
    CorrectionOutcomeUnknownError,
    CorrectionRuleKindType,
    CorrectionRuleUpdate,
    CorrectionType,
    FirmwareUpdateStatus,
    HaloAPIError,
    HaloClient,
    HaloError,
    HaloOAuth,
    HaloSignalRClient,
    HaloSignalRError,
    InvalidCallbackError,
    LoginRequiredError,
    OAuthClientProfile,
    SignalRBackpressureError,
    SignalRConnectionError,
    SignalREvent,
    SignalRHub,
    SignalRNegotiationError,
    SignalRProtocolError,
    StaleCommandNumberError,
    StateStore,
    TokenSet,
    UnsafeCorrectionError,
    WalkStopOption,
)
```

HTTP responses intentionally remain dictionaries and lists. The upstream schema
can change independently of this package.

## Authentication

The simplest supported login is through the CLI:

```bash
halo auth login --password
```

For accounts requiring an interactive identity provider or MFA, use the hosted
browser flow:

```bash
halo auth login
```

Both commands save a rotating OAuth session in the owner-only state file. Later
`HaloClient` and `HaloSignalRClient` instances load and refresh that session
automatically.

### `HaloOAuth`

```python
HaloOAuth(
    client_secret: str,
    *,
    profile: OAuthClientProfile = IOS_PROFILE,
    http: httpx.Client | None = None,
    auth_base_url: str = "https://auth.halocollar.com",
)
```

`HaloOAuth` is a synchronous context manager. It owns and closes its HTTP client
unless one is supplied.

| Method | Returns | Purpose |
| --- | --- | --- |
| `begin_login()` | `AuthorizationFlow` | Create the hosted-login URL, PKCE verifier, state, and nonce. |
| `complete_login(callback_url, flow)` | `TokenSet` | Validate a `haloapp://callback` URL and exchange its authorization code. |
| `complete_login_from_browser_capture(capture, flow)` | `TokenSet` | Extract the callback from raw HTTP or a WebInspector HAR export. |
| `password_login(username, password)` | `TokenSet` | Use the Android password grant. The password is not retained. |
| `refresh(refresh_token)` | `TokenSet` | Exchange and rotate a refresh token. |
| `close()` | `None` | Close an internally owned HTTP client. |

The password grant only accepts `ANDROID_PROFILE`. The browser flow defaults to
`IOS_PROFILE`. Halo binds refresh tokens to the OAuth client that issued them,
so a stored iOS session cannot be silently reused as an Android session or vice
versa.

### Programmatic password login

```python
import getpass

from halo_collar import ANDROID_CLIENT_SECRET, ANDROID_PROFILE, HaloOAuth, StateStore

password = getpass.getpass("Halo password: ")
with HaloOAuth(ANDROID_CLIENT_SECRET, profile=ANDROID_PROFILE) as oauth:
    tokens = oauth.password_login("account@example.com", password)
password = ""

StateStore().save_session(
    tokens,
    client_id=ANDROID_PROFILE.client_id,
    app_version=ANDROID_PROFILE.app_version,
)
```

Do not put an account password, access token, refresh token, callback URL, or
browser capture in source code, shell history, logs, or a Git repository.

### Authentication models

`OAuthClientProfile` describes a Halo mobile OAuth identity:

- `name`
- `client_id`
- `app_version`
- `token_user_agent`

`AuthorizationFlow` contains:

- `url`
- `code_verifier`
- `state`
- `nonce`

`TokenSet` contains:

- `access_token`
- `refresh_token`
- `expires_at`
- `token_type` (default: `Bearer`)
- `scope`

`TokenSet.is_expired` compares the recorded expiry with the current UTC time.

## Synchronous REST client

### Construction and lifecycle

```python
HaloClient(
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
    api_base_url: str = "https://api.halocollar.com",
    auth_base_url: str | None = None,
)
```

Typical use:

```python
from halo_collar import HaloClient

with HaloClient() as halo:
    pets = halo.pets()
```

The constructor loads tokens and client metadata from `StateStore` when they are
not supplied. It generates and persists an app-instance UUID, sends the selected
timezone in Halo's client header, and owns its `httpx.Client` unless a client is
injected. Use `close()` when not using the context manager.

Outbound JSON DTO keys follow Halo's PascalCase request contract. Returned
dictionaries preserve the server's lower camel-case response keys.

`refresh_login(force=False)` returns the current usable `TokenSet` or refreshes
and persists it. Authenticated requests refresh once after a definite HTTP 401.
The client does not automatically retry other network failures.

### Read methods

| Method | Return type | Operation |
| --- | --- | --- |
| `configuration()` | `dict` | Public application configuration. No login required. |
| `videos()` | `list[dict]` | HLS video assets derived locally from `configuration()`. |
| `collars()` | `list[dict]` | Collars on the account, including connectivity and telemetry. |
| `collar(collar_id)` | `dict` | One account collar and its current `petInfo` relationship. |
| `firmware_statuses()` | `list[dict]` | Installed firmware and server-managed update snapshots for every collar. |
| `firmware_status(collar_id)` | `dict` | One collar's focused firmware snapshot. |
| `pets()` | `list[dict]` | All pets, including pets without collars. |
| `pet(pet_id, *, refresh_telemetry=False)` | `dict` | One pet, optionally requesting fresher collar telemetry. |
| `set_pet_fences_enabled(pet_id, enabled)` | `dict` | Set the desired containment mode and return desired plus last-reported collar state. |
| `set_pet_beacons_assigned(pet_id, assigned)` | `dict \| None` | Change beacon assignment through its separate route; returns the response object or `None` for an empty success. |
| `account_map(latitude=None, longitude=None, *, refresh_telemetry=False, max_corrections_count=20)` | `dict` | Aggregate pets, collars, geofences, and recent corrections. Pass both coordinates or neither. |
| `geofences()` | `list[dict]` | Account-scoped fences extracted locally from `account_map()`. |
| `geo_fence_pet_sync(geo_fence_id)` | `list[dict]` | Automatic per-pet fence distribution state extracted from the map response. |
| `walks(*, page=1, page_size=30)` | `dict` | One page of recorded walks. |
| `walk_summary(walk_id)` | `dict` | One completed walk and its trail-image URLs. |
| `notifications(*, page=1, page_size=30)` | `dict` | One page of notification history. |
| `portal_notifications()` | `dict \| list` | In-app portal messages, a separate feed from notification history. |
| `mapbox_requests()` | `dict \| list` | Account map-provider requests. |
| `pet_colors()` | `list[dict]` | Collar colors accepted by pet creation and updates. |
| `correction_rule_configuration()` | `dict` | Allowed sounds, vibrations, and intensity levels. |
| `pet_correction_rules(pet_id)` | `dict` | Correction rules configured for one pet. |
| `training()` | `dict` | Account training-course progress. |
| `training_course_link(curriculum_id, course_name)` | `str` | One-time external SCORM launch URL. |
| `user_profile()` | `dict` | Account profile and read-only completion flags. |
| `onboarding_progress()` | `dict` | Versioned onboarding-progress record. |
| `questionnaire()` | `dict` | Saved account questionnaire; an absent questionnaire is an API error. |
| `beacons()` | `dict \| list` | Registered beacons and available ranges. |
| `beacon_pet_sync(beacon_id)` | `list[dict]` | One beacon's asynchronous per-pet distribution state. |
| `subscription()` | `dict \| list` | Subscription, limits, and enabled features. |
| `server_time()` | `datetime` | Halo's UTC clock used for correction expiration. |
| `collar_for_pet(pet_id)` | `dict` | Find a pet's assigned collar from `collars()`. |
| `collar_is_online(collar)` | `bool` | Test locally for a socket-connected Wi-Fi or cellular adapter. |

`walks()` and `notifications()` return the paging envelope:
`pageNumber`, `pageSize`, `totalNumberOfPages`, `totalNumberOfItems`, and
`results`.

### Account, notification, and device methods

| Method | Return type | Behavior |
| --- | --- | --- |
| `register_mobile_device(*, model=None, manufacturer=None, version_string=None, platform_name=None, idiom="Phone")` | `int` | Register this installation and persist Halo's `mobileId`. |
| `set_notification_status(notification_ids, *, status="Read")` | `None` | Mark one or more history notifications read or unread. |
| `generate_ecommerce_login_magic_code()` | `dict` | Mint a single-use Halo-store login credential. Do not log the result. |
| `lookup_parcels(latitude, longitude, *, page=1, results_per_page=1)` | `dict` | Query public property records used by the fence editor. Results may contain real owner names and mailing addresses. |
| `find_collar(collar_id)` | `None` | Play a collar's audible locate tone. |
| `subscribe_push_notifications(device_handle, *, platform_type="Android")` | `None` | Register a push-notification device token. |
| `unsubscribe_push_device(device_handle)` | `None` | Remove a push-notification device token. |

The ecommerce magic code and push device handles are credentials. Parcel data is
public-record information about real people. Treat all three accordingly.

### Profile, onboarding, questionnaire, and email methods

| Method | Return type | Behavior |
| --- | --- | --- |
| `update_profile_name(first_name, last_name)` | `dict \| None` | Replace the profile's first and last name. |
| `upload_profile_avatar(image, *, filename="avatar.png", content_type="image/png")` | `None` | Upload image bytes as the `icon` multipart field. |
| `delete_profile_avatar()` | `None` | Remove the current profile avatar. |
| `update_onboarding_progress(*, version, steps, progress_state)` | `dict` | Save the versioned onboarding DTO and return Halo's normalized response. |
| `save_questionnaire(questionnaire)` | `dict \| None` | Save a complete PascalCase questionnaire DTO. |
| `check_user_can_change_email(email)` | `None` | Validate a prospective email address without starting the change. |
| `request_email_change(email)` | `None` | Send an email-change confirmation code to the new address. |
| `confirm_email_change(code)` | `None` | Confirm the pending email change. |
| `resend_email_change_confirmation()` | `None` | Resend the pending confirmation message. |
| `cancel_email_change()` | `str` | Restore or cancel the pending email change. |
| `delete_account()` | `None` | Permanently delete the authenticated account. |

`update_profile_name()` is intentionally not a generic profile patch: only
first and last name are sent. There is also no client setter for
`hasCompletedQuestionnaire` or `hasFinishedUserGuide`. Saving a questionnaire
indirectly establishes the former; a successful `questionnaire()` call is the
reliable completion check. The latter remains response-only state.

`update_onboarding_progress()` requires the version returned by
`onboarding_progress()`. If Halo reports that the version is out of date,
reload the progress object and submit the revised DTO instead of blindly
retrying the stale one. `steps` accepts step ID strings or dictionaries with an
`Id`/`id`, and the client serializes each item as `{"Id": "..."}`.

`save_questionnaire()` preserves the supplied object so callers can include the
complete upstream PascalCase `UserQuestionnaireDto`, including current or future
choice fields. Profile/avatar writes and the email validation, request,
confirmation, and resend calls may have empty successful responses, so their
methods return `None`.

Email changes use the normal authenticated session plus the emailed code; the
client sends no password or step-up credential. After confirmation, refresh
`user_profile()` to verify the email/current-email value and that
`hasChangeEmailRequest` is false. Treat `delete_account()` as irreversible.

### Collar provisioning methods

| Method | Return type | Behavior |
| --- | --- | --- |
| `check_collar_binding(serial_number)` | `dict` | Check whether a printed serial can be bound to this account. |
| `bind_collar(serial_number, encrypted_serial_number)` | `dict` | Bind the physical collar to the account using its BLE-derived encrypted serial. |
| `bind_collar_to_pet(pet_id, collar_id)` | `None` | Attach an account collar to a pet using the server-issued collar UUID. |
| `unbind_collar_from_pet(pet_id)` | `None` | Detach the pet relationship while keeping the collar on the account. |
| `unbind_collar_from_user(collar_id)` | `None` | Remove the collar from the authenticated account. |
| `pet_collar_binding_is_synchronized(pet, collar_id)` | `bool` | Test a refreshed pet snapshot for the requested binding ID and a true synchronization flag. |
| `collar_is_assigned_to_pet(collar, pet_id)` | `bool` | Test a collar snapshot for the reciprocal `petInfo.id` relationship. |

Account binding and pet attachment are separate. Only `bind_collar()` takes the
encrypted serial obtained from the physical collar; `bind_collar_to_pet()` takes
the `collar.id` returned by that account-binding response.

The pet bind and unbind routes return an empty HTTP success before collar
synchronization is necessarily complete. For attachment, fetch
`pet(pet_id, refresh_telemetry=True)` until
`pet_collar_binding_is_synchronized()` is true, then confirm the reciprocal
relationship with `collar()` and `collar_is_assigned_to_pet()`. For detach,
`collarInfo: null` on the pet and `petInfo: null` on the collar are stronger
confirmation than the nullable synchronization flag.

`unbind_collar_from_user()` is distinct from pet detachment. The server cascade
for an assigned collar has not been directly observed, so callers that want an
explicit two-stage removal should detach it from its pet first. There is no
`DELETE /collar/{id}` method; `delete_pet()` deletes the pet entity, not its
collar.

### Firmware status

Firmware access is read-only and reuses the proven collar endpoints:

```python
all_statuses = halo.firmware_statuses()       # GET /collar/my
one_status = halo.firmware_status("COLLAR_ID")  # GET /collar/{id}
state = halo.firmware_update_state(one_status)
```

Each focused status preserves the complete nested `firmware` and
`firmwareUpdate` objects alongside `collarId`, `serialNumber`, `collarType`,
`hasFirmwareUpdatesAvailable`, and the derived raw `updateStatus` string.
`firmware_update_state()` returns a known `FirmwareUpdateStatus`, preserves an
unknown future state as a string, and returns `None` when no update state exists.

Known states are:

| `FirmwareUpdateStatus` member | Wire value |
| --- | --- |
| `UNKNOWN` | `unknown` |
| `DOWNLOAD_DELAYED_INCOMPATIBLE_NETWORK` | `downloadDelayedIncompatibleNetwork` |
| `DOWNLOAD_DELAYED_LOW_BATTERY` | `downloadDelayedLowBattery` |
| `DOWNLOADING` | `downloading` |
| `DOWNLOAD_FAILED` | `downloadFailed` |
| `VERIFYING` | `verifying` |
| `VERIFY_FAILED` | `verifyFailed` |
| `APPLY_DELAYED_NOT_CHARGING` | `applyDelayedNotCharging` |
| `APPLYING` | `applying` |
| `APPLY_FAILED` | `applyFailed` |
| `DOWNLOAD_DELAYED_NOT_ON_CHARGER` | `downloadDelayedNotOnCharger` |
| `APPLIED` | `applied` |
| `DOWNLOAD_NOT_STARTED` | `downloadNotStarted` |

Delayed and failed states are data inside a successful collar response, not
necessarily HTTP errors. Strong completion evidence is an installed firmware
ID/version matching the target followed by `firmwareUpdate: null`.
`firmwareLatestProduction` and `firmwareLatestBeta` describe the installed
release; they are not channel selectors.

There is no supported firmware start, cancel, package URL, manifest, or release
channel request. Firmware capability strings do not establish such routes. The
existing NotificationHub stream may carry `FirmwareUpdateIsApplying`,
`FirmwareUpdateApplied`, and `FirmwareUpdateFailed` notification types; they
remain available in each `SignalREvent.raw` payload.

### Beacon methods

| Method | Return type | Behavior |
| --- | --- | --- |
| `beacons()` | `dict \| list` | Account beacons, available ranges, default range, and optional unknown-beacon defaults. |
| `beacon_name_is_available(name, *, beacon_id=None)` | `bool` | Check a new name or exclude the current beacon while renaming. |
| `check_beacon_binding(serial_number)` | `dict` | Check whether a serial is free, bound here, or bound to another account. |
| `add_beacon(*, name, serial_number, model_type, action_type, should_notify, ...)` | `dict` | Add or bind a physical beacon. |
| `update_beacon(beacon_id, **supplied_fields)` | `dict` | Update only supplied settings; explicit `None` sends JSON null. |
| `delete_beacon(beacon_id)` | `None` | Delete or unbind the account beacon record. |
| `upload_beacon_telemetry(readings)` | `None` | Upload phone-observed serial/battery readings to cloud telemetry. |
| `set_pet_beacons_assigned(pet_id, assigned)` | `dict \| None` | Enable or disable the complete account beacon configuration for one pet. |
| `beacon_pet_sync(beacon_id)` | `list[dict]` | Read `pending`, `completed`, or `skipped` distribution state. |

`BeaconModelType`, `BeaconActionType`, and
`BeaconCorrectionEscalationType` expose the known PascalCase request values.
Response dictionaries preserve Halo's lower-camel enum strings.

The per-pet assignment route contains no beacon id and therefore does not
assign one selected beacon. After changing it, poll `beacon_pet_sync()` for
`status == "completed"`. Battery telemetry uploads update the cloud record but
do not configure physical hardware.

### Pet methods

| Method | Return type | Behavior |
| --- | --- | --- |
| `pet_name_is_available(name, *, pet_id=None)` | `bool` | Check name uniqueness; pass the current id while renaming. |
| `add_pet(*, name, color_hex, breed, birthday, weight_kg)` | `dict` | Create a pet without a collar. |
| `update_pet(pet_id, *, name, color_hex, breed, birthday, weight_kg)` | `dict` | Fully replace a pet profile. All fields are required. |
| `delete_pet(pet_id)` | `None` | Permanently delete a pet and its Halo history. |
| `set_pet_fences_enabled(pet_id, enabled)` | `dict` | Set containment on or off without changing beacon assignment. |
| `set_pet_beacons_assigned(pet_id, assigned)` | `dict \| None` | Enable or disable beacon assignment without changing containment mode. |

`birthday` accepts an ISO date string or `datetime`. `color_hex` should be one
of the values returned by `pet_colors()`.

Containment requests send `ModePatch.FencesOn` and leave
`ModePatch.BeaconsOn` null. In the response, `desiredMode` is the target the
cloud accepted; `telemetry.mode` is the collar's last reported state. A
difference between those values means the change has not yet been confirmed as
applied by the collar. The client preserves any additional response members
without interpreting them. Disabling fences can remove physical containment;
verify `telemetry.mode` before relying on the change.

### Geofence methods

Location points are `(latitude, longitude)` tuples. At least three points are
required.

| Method | Return type | Behavior |
| --- | --- | --- |
| `geo_fence_safe_zones(location_points, *, analytics=None)` | `list` | Preview safe zones before saving a boundary. |
| `geo_fence_name_is_available(name, *, geo_fence_id=None)` | `bool` | Check name uniqueness; pass the current id while renaming. |
| `geo_fence_pet_sync(geo_fence_id)` | `list[dict]` | Read automatic distribution status for each account pet. |
| `add_geo_fence(name, location_points, *, public_visibility_type="Private", analytics=None)` | `dict` | Create a containment fence. |
| `rename_geo_fence(geo_fence_id, name)` | `None` | Rename without changing the boundary. |
| `update_geo_fence_location(geo_fence_id, location_points, *, analytics=None)` | `dict` | Replace the complete boundary. |
| `delete_geo_fence(geo_fence_id)` | `dict` | Permanently delete the fence. |

Creating, moving, or deleting a fence changes the boundary used to contain a
dog once the collar synchronizes. Preview and verify points before writing.
The points sent to `geo_fence_safe_zones()` describe the warning boundary; its
response contains the generated safe-zone geometry.

Fences are account-scoped and automatically associated with the account's pets.
Fence objects carry `petsSync` distribution entries with `petId`, `isAssigned`,
and synchronization status (`unknown`, `pending`, `completed`, or `skipped`).
Collarless pets normally report `skipped`; that does not imply unassignment.

A pet's nullable `currentGeoFenceId` is separate, response-only state identifying
the fence last reported active by the collar. Multiple fences may be synchronized
to a pet simultaneously, and `update_pet()` does not send this field.

After creating or updating a fence, poll `account_map()` until
`petsSync.status == "completed"` and the pet reports
`isFencesSynchronized == true`. Granular assignment, unassignment, and current
fence mutation are unsupported and undocumented.

### Walk methods

| Method | Return type | Behavior |
| --- | --- | --- |
| `walks(*, page=1, page_size=30)` | `dict` | Page through completed walk summaries. |
| `walk_summary(walk_id)` | `dict` | Fetch one completed summary. |
| `set_walk_paused(walk_id, collar_id, paused)` | `dict` | Pause or resume one participating collar. |
| `stop_walk(walk_id, collar_id, *, stop_option=WalkStopOption.DEFAULT)` | `dict` | Stop one collar without finalizing the overall walk. |
| `mark_walk_ended(walk_id, *, started_at, ended_at, pets, user, location_name)` | `None` | Submit timestamps and aggregate per-pet/user statistics. |
| `upload_walk_trail_thumbnail(walk_id, image, *, filename="trail-thumbnail.png", content_type="image/png")` | `None` | Upload rendered overall trail-image bytes. |
| `upload_walk_pet_trail_image(walk_id, pet_id, image, *, filename="trail-image.png", content_type="image/png")` | `None` | Upload rendered per-pet trail-image bytes. |

`WalkStopOption` provides `DEFAULT`, `FORCE_KEEP_FENCES_MODE`, and
`FORCE_SET_FENCES_ON`. Pause and stop response dictionaries are preserved
because non-success business results are useful to callers and their observed
wire casing is not yet stable.

A successful pause/stop result is command acknowledgement. Confirm pause from
fresh telemetry where the same walk has the desired `isPaused`; confirm stop
when the collar's `telemetry.walk` becomes null.

`mark_walk_ended()` accepts `datetime` or ISO strings for its top-level
timestamps. Nested `pets` and `user` dictionaries use the upstream PascalCase
DTO keys. User durations are .NET `TimeSpan` strings. The summary carries no raw
path-point arrays.

There is intentionally no `start_walk()` method. A normal mobile start requires
a locally generated UUID and Bluetooth leash-mode commands. Final summary and
image processing are separate: poll `walk_summary()` for `endedAt`, then for
non-null trail image URLs.

### Correction rules and collar tests

Persistent rule updates use identified items rather than replacing the whole
pet collection:

```python
from halo_collar import CorrectionRuleKindType, CorrectionRuleUpdate

updated = halo.update_correction_rules(
    [
        CorrectionRuleUpdate(
            correction_rule_id="RULE_ID",
            kind_type=CorrectionRuleKindType.SOUND,
            level=3,
            sound_id="SOUND_ID",
        )
    ]
)
```

```python
update_correction_rules(items: Sequence[CorrectionRuleUpdate]) -> dict
```

Every item sends `CorrectionRuleId`, `KindType`, `Level`, `SoundId`, and
`VibrationId` with PascalCase keys. Known `CorrectionRuleKindType` values are
`Vibration`, `Sound`, and `Shock`; `Shock` is the wire name for static feedback.
Obtain rule IDs from `pet_correction_rules()` and valid asset IDs and levels
from `correction_rule_configuration()`. A rule ID implicitly selects the pet and
its existing escalation slot. Omitted rules are not deleted or reset, and
duplicate IDs in one batch are rejected locally.

The response is the complete lower-camel correction-rules object. Its
`lastCorrectionRulesUpdated` value confirms server storage only. Verify the
edited rule through `pet_correction_rules()`, then wait for the assigned collar's
general `configurationSyncStatus` to return to `uptodate`. Halo exposes no
correction-specific applied timestamp, version, or hash.

A direct test uses the proposed settings without saving a persistent rule:

```python
test_correction_on_collar(
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
) -> dict
```

This is a physical action. The method requires the modality's matching asset or
level, checks for a socket-connected collar by default, reads Halo's server
clock, and reserves the same per-pet command counter used by instant
corrections. Transport failures raise `CorrectionOutcomeUnknownError` and are
not retried. `oldCommandNumber` reconciles local state and raises
`StaleCommandNumberError`; the command is still not retried. A `success` result
is cloud acknowledgement, not proof of physical execution or persistent-rule
installation.

### Instant corrections

```python
send_instant_correction(
    pet_id: str,
    correction_type: CorrectionType | str,
    *,
    command_number: int | None = None,
    require_online: bool = True,
) -> dict
```

Supported `CorrectionType` values:

| Enum member | API value | Expiration |
| --- | --- | ---: |
| `WARNING` | `Warning` | 4 seconds |
| `FIRST_TIME` | `FirstTime` | 4 seconds |
| `ESCALATION` | `Escalation` | 4 seconds |
| `RETURN_WHISTLE` | `ReturnWhistle` | 7 seconds |
| `GOOD_BEHAVIOR` | `GoodBehavior` | 7 seconds |
| `HEADING_HOME` | `HeadingHome` | 7 seconds |

The first correction for a pet requires the next known `command_number`.
`StateStore` reserves the number before network dispatch and increments it for
later calls.

This is a physical action. By default the method refuses unless the assigned
collar reports a socket-connected Wi-Fi or cellular adapter. A transport failure
after dispatch raises `CorrectionOutcomeUnknownError` and is deliberately not
retried because the collar may already have acted. A stale command number is
reconciled locally but also is not retried.

## Async SignalR client

`HaloSignalRClient` is receive-only. It negotiates with Halo, follows the Azure
SignalR redirect, completes the JSON protocol handshake, maintains heartbeats,
and renegotiates after transient disconnects.

```python
HaloSignalRClient(
    halo: HaloClient,
    *,
    hub: SignalRHub | str = SignalRHub.TELEMETRY,
    base_url: str = "https://halo-prod-sockets-app.azurewebsites.net",
    http: httpx.AsyncClient | None = None,
    connector=None,
    queue_size: int = 256,
    max_reconnect_attempts: int | None = 8,
    reconnect_delay: float = 0.5,
    max_reconnect_delay: float = 15.0,
    handshake_timeout: float = 10.0,
    server_timeout: float = 45.0,
    signalr_ping_interval: float = 15.0,
    max_message_size: int = 1024 * 1024,
    max_negotiation_redirects: int = 5,
)
```

Available hubs:

- `SignalRHub.TELEMETRY` / `"TelemetryHub"`
- `SignalRHub.NOTIFICATIONS` / `"NotificationHub"`

Typical use:

```python
import asyncio

from halo_collar import HaloClient, HaloSignalRClient, SignalRHub


async def main() -> None:
    with HaloClient() as halo:
        async with HaloSignalRClient(halo, hub=SignalRHub.TELEMETRY) as live:
            await live.wait_connected(timeout=15)
            async for event in live:
                print(event.target, event.pet_id, event.sequence_code)


asyncio.run(main())
```

Public members:

| Member | Purpose |
| --- | --- |
| `is_connected` | Whether the SignalR JSON handshake is currently complete. |
| `start()` | Start connecting in a background task. |
| `wait_connected(timeout=None)` | Wait for the first successful handshake or propagate a terminal failure. |
| `events()` | Return the stream's sole asynchronous event iterator. |
| `close()` | Stop reconnecting and release the socket and owned HTTP client. |
| `async for event in live` | Shorthand for iterating `events()`. |

One `HaloSignalRClient` supports one consumer. Its bounded queue raises
`SignalRBackpressureError` instead of silently dropping live location updates.
Set `max_reconnect_attempts=None` for unlimited reconnect attempts.

### `SignalREvent`

| Field or property | Type | Meaning |
| --- | --- | --- |
| `hub` | `SignalRHub` | Source hub. |
| `target` | `str` | Server method name. |
| `arguments` | `list[Any]` | Invocation arguments. |
| `raw` | `dict[str, Any]` | Complete decoded SignalR invocation. |
| `pet_id` | `str \| None` | Convenience lookup for a common `petId` argument. |
| `sequence_code` | `Any \| None` | Telemetry manifest ordering marker, when present. |

Supported telemetry targets are `HandleIoTTelemetry`,
`HandleDataStateChanged`, and `HandleCollarDataSynchronized`. Unknown numeric
SignalR message types are ignored for forward compatibility; malformed protocol
records terminate the stream. NotificationHub payloads remain unmodified in
`raw`, including firmware notification types when Halo emits them.

## State storage

```python
StateStore(path: pathlib.Path | str | None = None)
```

The default Linux path is `~/.local/state/halo-collar/state.json`. The directory
and files are restricted to the current OS user, writes are atomic, and a
sidecar lock coordinates concurrent processes.

| Method | Purpose |
| --- | --- |
| `read()` | Return the complete state mapping. |
| `load_tokens()` | Load a validated `TokenSet`. |
| `save_tokens(tokens)` | Replace tokens without changing profile metadata. |
| `clear_tokens()` | Remove OAuth tokens but retain settings and counters. |
| `auth_profile()` | Return stored OAuth client id and app version. |
| `save_session(tokens, *, client_id, app_version)` | Atomically bind tokens to their OAuth client. |
| `settings()` | Return persisted non-token settings. |
| `update_settings(**settings)` | Merge string settings. |
| `reserve_command_number(pet_id, explicit=None)` | Atomically reserve the next correction number. |
| `reconcile_command_number(pet_id, current)` | Advance a counter after a stale-number response. |
| `clear()` | Delete credentials, settings, and counters. |

The state file contains live credentials. Never commit or share it. Owner-only
file permissions reduce accidental exposure but are not a substitute for disk
encryption.

## Exceptions

All package exceptions inherit from `HaloError`.

```text
HaloError
├── AuthenticationError
│   ├── LoginRequiredError
│   └── InvalidCallbackError
├── HaloAPIError
├── HaloSignalRError
│   ├── SignalRNegotiationError
│   ├── SignalRConnectionError
│   ├── SignalRProtocolError
│   └── SignalRBackpressureError
├── UnsafeCorrectionError
├── CommandCounterUnknownError
├── CorrectionOutcomeUnknownError
└── StaleCommandNumberError
```

`HaloAPIError` exposes `status_code`, `method`, and `path` when available.
`StaleCommandNumberError` exposes Halo's `current_command_number` and decoded
response.

## HTTP route map

These are upstream Halo routes called by `HaloClient`; this project does not run
an HTTP server of its own.

| Method | Upstream path | Python method |
| --- | --- | --- |
| `GET` | `/configuration/` | `configuration()`, `videos()` |
| `GET` | `/collar/my` | `collars()`, `collar_for_pet()`, `firmware_statuses()` |
| `GET` | `/collar/{id}` | `collar()`, `firmware_status()` |
| `PUT` | `/collar/check-can-be-bound-to-user` | `check_collar_binding()` |
| `PUT` | `/collar/bind-to-user` | `bind_collar()` |
| `POST` | `/collar/{id}/unbind-from-user` | `unbind_collar_from_user()` |
| `GET` | `/pet/my` | `pets()` |
| `GET` | `/pet/{id}` | `pet()` |
| `PUT` | `/pet/{id}/bind-collar` | `bind_collar_to_pet()` |
| `PUT` | `/pet/{id}/unbind-collar` | `unbind_collar_from_pet()` |
| `GET` | `/account/my/map` | `account_map()`, `geofences()`, `geo_fence_pet_sync()` |
| `POST` | `/account/mobile-data` | `register_mobile_device()` |
| `GET` | `/user-profile` | `user_profile()` |
| `PUT` | `/user-profile` | `update_profile_name()` |
| `PUT` | `/user-profile/me/icon` | `upload_profile_avatar()` |
| `DELETE` | `/user-profile/me/icon` | `delete_profile_avatar()` |
| `GET` | `/user-profile/onboarding/progress` | `onboarding_progress()` |
| `PUT` | `/user-profile/onboarding/progress` | `update_onboarding_progress()` |
| `GET` | `/user-profile/questionnaire` | `questionnaire()` |
| `PUT` | `/user-profile/questionnaire` | `save_questionnaire()` |
| `POST` | `/account/check-user-can-change-email` | `check_user_can_change_email()` |
| `POST` | `/account/email-change-request` | `request_email_change()` |
| `POST` | `/account/email-change-request/confirm` | `confirm_email_change()` |
| `POST` | `/account/email-change-request/resend-email` | `resend_email_change_confirmation()` |
| `PUT` | `/account/email-change-request` | `cancel_email_change()` |
| `DELETE` | `/account` | `delete_account()` |
| `GET` | `/beacon/my` | `beacons()`, `beacon_pet_sync()` |
| `PUT` | `/beacon/check-name-uniqueness` | `beacon_name_is_available()` |
| `POST` | `/beacon` | `add_beacon()` |
| `PUT` | `/beacon/{id}` | `update_beacon()` |
| `DELETE` | `/beacon/{id}` | `delete_beacon()` |
| `PUT` | `/beacon/telemetry` | `upload_beacon_telemetry()` |
| `PUT` | `/beacon/check-can-be-bound-to-user` | `check_beacon_binding()` |
| `GET` | `/subscription/my/` | `subscription()` |
| `GET` | `/walk/my` | `walks()` |
| `GET` | `/walk/{id}/summary` | `walk_summary()` |
| `POST` | `/walk/{id}/set-is-paused` | `set_walk_paused()` |
| `POST` | `/walk/{id}/stop` | `stop_walk()` |
| `POST` | `/walk/{id}/mark-ended` | `mark_walk_ended()` |
| `PUT` | `/walk/{id}/trail-thumbnail` | `upload_walk_trail_thumbnail()` |
| `PUT` | `/walk/{id}/pet/{petId}/trail-image` | `upload_walk_pet_trail_image()` |
| `GET` | `/notification/my/query` | `notifications()` |
| `GET` | `/portal-notification/my/in-app/` | `portal_notifications()` |
| `GET` | `/mapbox/request/my` | `mapbox_requests()` |
| `GET` | `/system/server-date-time` | `server_time()` |
| `GET` | `/pet/colors` | `pet_colors()` |
| `GET` | `/pet/{id}/correction-rules` | `pet_correction_rules()` |
| `GET` | `/correction-rule/configuration-v2` | `correction_rule_configuration()` |
| `PUT` | `/correction-rule` | `update_correction_rules()` |
| `PUT` | `/correction-rule/test-on-collar` | `test_correction_on_collar()` |
| `GET` | `/training/my-v2` | `training()` |
| `GET` | `/training/user/course-launch-link/{curriculum}/{course}` | `training_course_link()` |
| `POST` | `/pet/{id}/run-instant-correction/` | `send_instant_correction()` |
| `POST` | `/pet/add` | `add_pet()` |
| `PUT` | `/pet/{id}` | `update_pet()` |
| `DELETE` | `/pet/{id}` | `delete_pet()` |
| `PUT` | `/pet/{id}/instant-mode` | `set_pet_fences_enabled()` |
| `PUT` | `/beacon/set-is-assigned/{petId}` | `set_pet_beacons_assigned()` |
| `PUT` | `/pet/check-name-uniqueness` | `pet_name_is_available()` |
| `PUT` | `/notification/status` | `set_notification_status()` |
| `POST` | `/geo-fence/safe-zones` | `geo_fence_safe_zones()` |
| `PUT` | `/geo-fence/check-name-uniqueness` | `geo_fence_name_is_available()` |
| `POST` | `/geo-fence/add` | `add_geo_fence()` |
| `PUT` | `/geo-fence/{id}` | `rename_geo_fence()` |
| `PUT` | `/geo-fence/{id}/location` | `update_geo_fence_location()` |
| `DELETE` | `/geo-fence/{id}` | `delete_geo_fence()` |
| `POST` | `/account/generate-ecommerce-login-magic-code` | `generate_ecommerce_login_magic_code()` |
| `POST` | `/report-all/api/parcels` | `lookup_parcels()` |
| `PUT` | `/collar/{id}/find` | `find_collar()` |
| `PUT` | `/push-notification/subscribe` | `subscribe_push_notifications()` |
| `PUT` | `/push-notification/unsubscribe-device` | `unsubscribe_push_device()` |

Route spelling, capitalization, and trailing slashes match the upstream API.
No stability guarantee is possible for these upstream interfaces.
