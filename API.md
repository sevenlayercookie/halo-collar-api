# Halo Collar Python API reference

> [!IMPORTANT]
> This is an unofficial, reverse-engineered client. It is not affiliated with,
> endorsed by, or supported by Halo Collar. The interfaces described here were
> inferred from observed iOS and Android app traffic and may change without
> notice.

This document describes the public Python API, live SignalR API, persistence
model, exceptions, and observed HTTP routes in `halo-collar` 0.1.0. See the
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
    CommandCounterUnknownError,
    CorrectionOutcomeUnknownError,
    CorrectionType,
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
)
```

HTTP responses intentionally remain dictionaries and lists. The upstream schema
is reverse-engineered and can change independently of this package.

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
| `password_login(username, password)` | `TokenSet` | Use the observed Android password grant. The password is not retained. |
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

`OAuthClientProfile` describes an observed official-app OAuth identity:

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

`refresh_login(force=False)` returns the current usable `TokenSet` or refreshes
and persists it. Authenticated requests refresh once after a definite HTTP 401.
The client does not automatically retry other network failures.

### Read methods

| Method | Return type | Observed operation |
| --- | --- | --- |
| `configuration()` | `dict` | Public application configuration. No login required. |
| `videos()` | `list[dict]` | HLS video assets derived locally from `configuration()`. |
| `collars()` | `list[dict]` | Collars on the account, including connectivity and telemetry. |
| `pets()` | `list[dict]` | All pets, including pets without collars. |
| `pet(pet_id, *, refresh_telemetry=False)` | `dict` | One pet, optionally requesting fresher collar telemetry. |
| `account_map(latitude=None, longitude=None, *, refresh_telemetry=False, max_corrections_count=20)` | `dict` | Aggregate pets, collars, geofences, and recent corrections. Pass both coordinates or neither. |
| `geofences()` | `list[dict]` | Geofences extracted locally from `account_map()`. |
| `walks(*, page=1, page_size=30)` | `dict` | One page of recorded walks. |
| `notifications(*, page=1, page_size=30)` | `dict` | One page of notification history. |
| `portal_notifications()` | `dict \| list` | In-app portal messages, a separate feed from notification history. |
| `mapbox_requests()` | `dict \| list` | Account map-provider requests. |
| `pet_colors()` | `list[dict]` | Collar colors accepted by pet creation and updates. |
| `correction_rule_configuration()` | `dict` | Sound and vibration intensity configuration. |
| `pet_correction_rules(pet_id)` | `dict` | Correction rules configured for one pet. |
| `training()` | `dict` | Account training-course progress. |
| `training_course_link(curriculum_id, course_name)` | `str` | One-time external SCORM launch URL. |
| `user_profile()` | `dict` | Account profile. |
| `beacons()` | `dict \| list` | Registered beacons and available ranges. |
| `subscription()` | `dict \| list` | Subscription, limits, and enabled features. |
| `server_time()` | `datetime` | Halo's UTC clock used for correction expiration. |
| `collar_for_pet(pet_id)` | `dict` | Find a pet's assigned collar from `collars()`. |
| `collar_is_online(collar)` | `bool` | Test locally for a socket-connected Wi-Fi or cellular adapter. |

`walks()` and `notifications()` return the observed paging envelope:
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

### Pet methods

| Method | Return type | Behavior |
| --- | --- | --- |
| `pet_name_is_available(name, *, pet_id=None)` | `bool` | Check name uniqueness; pass the current id while renaming. |
| `add_pet(*, name, color_hex, breed, birthday, weight_kg)` | `dict` | Create a pet without a collar. |
| `update_pet(pet_id, *, name, color_hex, breed, birthday, weight_kg)` | `dict` | Fully replace a pet profile. All fields are required. |
| `delete_pet(pet_id)` | `None` | Permanently delete a pet and its Halo history. |

`birthday` accepts an ISO date string or `datetime`. `color_hex` should be one
of the values returned by `pet_colors()`.

### Geofence methods

Location points are `(latitude, longitude)` tuples. At least three points are
required.

| Method | Return type | Behavior |
| --- | --- | --- |
| `geo_fence_safe_zones(location_points, *, analytics=None)` | `list` | Preview safe zones before saving a boundary. |
| `geo_fence_name_is_available(name, *, geo_fence_id=None)` | `bool` | Check name uniqueness; pass the current id while renaming. |
| `add_geo_fence(name, location_points, *, public_visibility_type="Private", analytics=None)` | `dict` | Create a containment fence. |
| `rename_geo_fence(geo_fence_id, name)` | `None` | Rename without changing the boundary. |
| `update_geo_fence_location(geo_fence_id, location_points, *, analytics=None)` | `dict` | Replace the complete boundary. |
| `delete_geo_fence(geo_fence_id)` | `dict` | Permanently delete the fence. |

Creating, moving, or deleting a fence changes the boundary used to contain a
dog once the collar synchronizes. Preview and verify points before writing.

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

Observed `CorrectionType` values:

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

Observed telemetry targets are `HandleIoTTelemetry`,
`HandleDataStateChanged`, and `HandleCollarDataSynchronized`. Unknown numeric
SignalR message types are ignored for forward compatibility; malformed protocol
records terminate the stream.

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

## Observed HTTP route map

These are upstream Halo routes called by `HaloClient`; this project does not run
an HTTP server of its own.

| Method | Upstream path | Python method |
| --- | --- | --- |
| `GET` | `/configuration/` | `configuration()`, `videos()` |
| `GET` | `/collar/my/` | `collars()`, `collar_for_pet()` |
| `GET` | `/pet/my` | `pets()` |
| `GET` | `/pet/{id}/` | `pet()` |
| `GET` | `/account/my/map` | `account_map()`, `geofences()` |
| `POST` | `/account/mobile-data` | `register_mobile_device()` |
| `GET` | `/user-profile/` | `user_profile()` |
| `GET` | `/beacon/my/` | `beacons()` |
| `GET` | `/subscription/my/` | `subscription()` |
| `GET` | `/walk/my` | `walks()` |
| `GET` | `/notification/my/query` | `notifications()` |
| `GET` | `/portal-notification/my/in-app/` | `portal_notifications()` |
| `GET` | `/mapbox/request/my` | `mapbox_requests()` |
| `GET` | `/system/server-date-time` | `server_time()` |
| `GET` | `/pet/colors` | `pet_colors()` |
| `GET` | `/pet/{id}/correction-rules` | `pet_correction_rules()` |
| `GET` | `/correction-rule/configuration-v2` | `correction_rule_configuration()` |
| `GET` | `/training/my-v2` | `training()` |
| `GET` | `/training/user/course-launch-link/{curriculum}/{course}` | `training_course_link()` |
| `POST` | `/pet/{id}/run-instant-correction/` | `send_instant_correction()` |
| `POST` | `/pet/add` | `add_pet()` |
| `PUT` | `/pet/{id}` | `update_pet()` |
| `DELETE` | `/pet/{id}` | `delete_pet()` |
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

Route spelling, capitalization, and trailing slashes match captured app traffic.
No stability guarantee is possible for these undocumented upstream interfaces.
