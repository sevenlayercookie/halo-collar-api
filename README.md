# Unofficial Halo Collar Python client

This project is a conservative Python client for REST API supported from the
Halo Collar iOS and Android apps. It is not affiliated with or supported by Halo.

Implemented, supported functionality:

- OAuth 2.0 password login using the supported Android client
- OAuth 2.0 Authorization Code login with PKCE through Halo's hosted login page
- Refresh-token login and automatic access-token refresh/rotation
- Public application configuration
- Account collars and connectivity
- Pet listing (including collarless pets), details, creation, editing, and
  optional telemetry refresh
- The aggregate map view, geofence create/rename/move/delete, and safe-zone preview
- User profile, beacons, subscription, and in-app notifications
- Walk history, notification history, and marking notifications read
- Correction rules and the sound/vibration intensity catalog
- Training course progress and course launch links
- Halo server-clock synchronization
- Collar locate tone and push-notification registration
- One-shot instant corrections for the six supported correction enums

SignalR/WebSocket support, BLE rolling codes, DGNSS forwarding, walk recording,
beacon mutations, and collar provisioning are not implemented.

## Safety and privacy

An instant correction is a physical action. The server returning `success` means
the cloud accepted the command; it does not prove the collar executed it. The
client never retries after a transport failure or ambiguous dispatch. A definite
HTTP 401 is refreshed and retried once because the API rejected the original
request before accepting the command. A timeout is reported as an unknown
outcome.

Before testing a correction:

1. Remove the collar from the dog.
2. Configure the lowest safe feedback level in the official app.
3. Confirm what each correction enum does with your specific configuration.
4. Avoid issuing corrections from this client and the official app concurrently.

Never commit debug archives, account passwords, access/refresh tokens, browser
cookies, hardware identifiers, Wi-Fi details, signed report URLs, or GPS
coordinates. `private-logs/` and local environment files are ignored by Git.

## Install

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

The only runtime dependency is
[HTTPX](https://www.python-httpx.org/). Python 3.10 or newer is required.

## Login with email and password

The simplest headless login uses the Android profile's password grant:

```bash
halo --timezone America/Chicago login --password
```

It prompts for the account email and securely prompts for the account password.
The password is sent only to Halo's HTTPS token endpoint and is never written to
the state file. The resulting access token, rotating refresh token, and Android
OAuth profile are stored in the owner-only state file. Future API calls refresh
automatically; you do not need to enter the password again unless the refresh
token dies or is revoked.

Both the iOS and Android OAuth client credentials are embedded in the package
because they are application constants distributed inside the official apps, not
per-user secrets. No login mode needs you to supply one.

Each profile's credential is resolved in this order: an explicit argument, the
per-profile `HALO_IOS_CLIENT_SECRET` / `HALO_ANDROID_CLIENT_SECRET`, the generic
`HALO_CLIENT_SECRET`, and finally the embedded constant. The app credential is
never written to the state file: it belongs to the app rather than to you, so a
stored copy would only pin you to a stale value once Halo rotates it. If that
happens before a release updates the constant, set the environment variable.
`HALO_CLIENT_SECRET` applies to whichever profile is active, so prefer the
per-profile variable when you use both.

Password grant sends your password directly to Halo's token endpoint and may not
work for accounts that require an interactive identity-provider or MFA step.
Halo could also disable this legacy OAuth grant in the future. The hosted browser
flow remains available as a fallback.

## Login with the hosted browser

```bash
halo --timezone America/Chicago login
```

The command uses the supported iOS profile and opens Halo's hosted login page with
a fresh PKCE verifier, state, and nonce. This project never receives your
email/password or automates the hosted form. After login, paste the response's full
`Location: haloapp://callback?...` value at the hidden prompt. No browser cookies
or other headers are required.

The iOS client credential is embedded, so this flow runs without a secret prompt.
Use `--platform android` to run the same browser flow against the Android client.

If it is easier to save the browser exchange than to copy the URL, you can hand
off a WebInspector HAR export, a raw HTTP callback exchange, or just the 302
response headers containing `Location`:

```bash
halo --timezone America/Chicago login --no-browser --browser-capture
```

Transfer the capture to the machine running this tool and enter its path. Only
the `Location` callback is read from it; browser cookies are ignored and never
copied into Halo's state file. Callback state is validated locally and Halo
validates the PKCE verifier during the token exchange.

To bootstrap from an existing capture without putting tokens in shell history:

```bash
halo --timezone America/Chicago login --from-refresh-token
```

The refresh token is prompted without echo. Halo binds refresh tokens to the
client that issued them, so pick the matching `--platform`; the stored session
records which client it belongs to and will not be reused under another one. The
session is stored outside the repository in an atomic state file with `0600`
permissions on POSIX systems. On
Linux the default is `~/.local/state/halo-collar/state.json`. Use `halo logout`
to remove the state and command counters.

If Halo returns `invalid_grant`, the refresh token has expired, was revoked, or
the one-time authorization grant is invalid. The client stops and asks for a new
login and removes the dead tokens while retaining non-token settings. It never
stores the Halo account password.

## Read data

```bash
halo status
halo configuration
halo collars
halo pets
halo fences
halo pet PET_ID
halo pet PET_ID --refresh-telemetry
halo map
halo map 37.4219983 -122.084
halo walks --page 1 --page-size 30
halo notifications --page 1 --page-size 30
halo profile
halo beacons
halo subscription
halo inbox
halo correction-config
halo server-time
```

`halo inbox` reads `/portal-notification/my/in-app/`, a different feed from the
`/notification/my/query` history behind `halo notifications`. `halo profile`
summarizes by default because the payload carries your email addresses, avatar
URL, and referral link; `beacons`, `subscription`, `inbox`, and
`correction-config` print in full because nothing in their supported payloads
needs hiding.

`halo collars`, `halo pets`, `halo fences`, and `halo map` print privacy-reduced
summaries rather than full Wi-Fi, coordinate, and signed-report-URL data. Pass
`--full` to any of them for Halo's complete response. Nothing is withheld from
you: the summary is only the default so that a command you ran to check a
battery level does not put your home coordinates into a screenshot, a shell
history, or a pasted bug report. The Python API always returns whole payloads.

`halo pets` reaches every pet on the account, including ones that have never had
a collar assigned and so never appear in `halo collars`. Use `halo pet PET_ID`
for the full object; it is unsummarized and needs no `--full`.

`halo fences` lists the account's geofences, which Halo returns only inside the
map payload. The summary keeps names, enabled state, zone types with a point
count, and per-pet sync status, and drops the zone polygons, the fence address,
and the signed thumbnail URL.

`halo map` calls `/account/my/map`, which the app polls on its home screen. One
response returns `pets` (each with its collar embedded), `geoFencesInfo`, and
`corrections`, so prefer it over several separate calls when polling. The apps
always send a viewport centre, but Halo returns the whole account without one,
so the coordinates are optional here and on `HaloClient.account_map`. The
summary counts `corrections` rather than redacting them, because no capture has
ever shown that list populated and there is no verified shape to trust.

## Locate a collar

```bash
halo find-collar COLLAR_ID
```

This plays the collar's locate tone. It is a physical action, but an
audible-only one — unlike a correction it is not aversive, so no command number
is reserved and Halo answers `204 No Content`.

## Endpoint coverage

Supported upstream routes:

| Method | Path | Client method |
| --- | --- | --- |
| GET | `/configuration/` | `configuration()` |
| GET | `/collar/my/` | `collars()` |
| GET | `/pet/my` | `pets()` |
| GET | `/pet/{id}/` | `pet()` |
| GET | `/account/my/map` | `account_map()`, `geofences()` |
| POST | `/account/mobile-data` | `register_mobile_device()` |
| GET | `/user-profile/` | `user_profile()` |
| GET | `/beacon/my/` | `beacons()` |
| GET | `/subscription/my/` | `subscription()` |
| GET | `/walk/my` | `walks()` |
| GET | `/notification/my/query` | `notifications()` |
| GET | `/portal-notification/my/in-app/` | `portal_notifications()` |
| GET | `/mapbox/request/my` | `mapbox_requests()` |
| GET | `/system/server-date-time` | `server_time()` |
| GET | `/pet/colors` | `pet_colors()` |
| GET | `/pet/{id}/correction-rules` | `pet_correction_rules()` |
| GET | `/correction-rule/configuration-v2` | `correction_rule_configuration()` |
| GET | `/training/my-v2` | `training()` |
| GET | `/training/user/course-launch-link/{curriculum}/{course}` | `training_course_link()` |
| POST | `/pet/{id}/run-instant-correction/` | `send_instant_correction()` |
| POST | `/pet/add` | `add_pet()` |
| PUT | `/pet/{id}` | `update_pet()` |
| DELETE | `/pet/{id}` | `delete_pet()` |
| PUT | `/pet/check-name-uniqueness` | `pet_name_is_available()` |
| PUT | `/notification/status` | `set_notification_status()` |
| POST | `/geo-fence/safe-zones` | `geo_fence_safe_zones()` |
| PUT | `/geo-fence/check-name-uniqueness` | `geo_fence_name_is_available()` |
| POST | `/geo-fence/add` | `add_geo_fence()` |
| PUT | `/geo-fence/{id}` | `rename_geo_fence()` |
| PUT | `/geo-fence/{id}/location` | `update_geo_fence_location()` |
| DELETE | `/geo-fence/{id}` | `delete_geo_fence()` |
| POST | `/account/generate-ecommerce-login-magic-code` | `generate_ecommerce_login_magic_code()` |
| POST | `/report-all/api/parcels` | `lookup_parcels()` |
| PUT | `/collar/{id}/find` | `find_collar()` |
| PUT | `/push-notification/subscribe` | `subscribe_push_notifications()` |
| PUT | `/push-notification/unsubscribe-device` | `unsubscribe_push_device()` |

Paths are sent exactly as supported, which is why some carry a trailing slash and
others do not. `/walk/my` pages with `page`/`pageSize` while
`/notification/my/query` pages with `Page`/`PageSize`; that inconsistency is
Halo's, not a typo.

### Parallel-call version

Every response carries a `Halo-ParallelCall-Version` header that Halo increments
as account state changes, and it rejects a write carrying a stale one with HTTP
400 `errorCode 3001`, "Parallel call version is obsolete". A fresh client has
never seen a response and so starts with a placeholder, which means a session
whose *first* request is a write would always fail. Before its first write the
client reads `/system/server-date-time` to be told the current value, then sends
the write carrying it. Corrections already read the clock for their expiry, so
this costs them nothing and no write is ever sent twice.

`add_geo_fence` returns the new fence nested under `geoFence` rather than at the
top level, while `update_geo_fence_location` answers `{"status": "success"}` and
returns no geometry.

### Pets

Halo replaces a pet's profile wholesale rather than patching it, so `update_pet`
requires all five fields. `halo pet-update` reads the pet first and fills in
whatever you did not pass, which keeps a single `--weight-kg` from blanking the
name and breed:

```bash
halo pet-colors                      # colorHex must come from this list
halo pet-add --name Scout --color-hex "#FF7A00" --breed goldenretriever \
    --birthday 2021-04-17 --weight-kg 28.5
halo pet-update PET_ID --weight-kg 29.2
halo pet-delete PET_ID
```

Saving marks the collar's configuration `outdated` until it next syncs. A new
pet has no collar until one is bound, so it appears in `halo pets` but not in
`halo collars`.

`delete_pet()` removes the pet and the history Halo keeps under it. Halo answers
200 with an empty body rather than returning what it deleted, so nothing here
undoes it; `halo pet-delete` asks you to type the pet's name first.

### Fences

Halo has no endpoint that lists fences on their own. `geofences()` and `halo
fences` read the `geoFencesInfo.geoFencesToDisplay` array out of the map payload,
so listing fences costs one `/account/my/map` call. Note `geoFencesTotalCount`:
`halo map` reports it, and a count larger than the returned array means Halo
truncated the list.

Fence geometry is a list of `(latitude, longitude)` corners; Halo needs at least
three. The app previews the derived safe zone while dragging, then saves:

```python
points = [(40.0001, -75.0001), (40.0002, -75.00015), (40.0003, -75.00005)]

with HaloClient() as halo:
    halo.geo_fence_safe_zones(points)          # preview, changes nothing
    halo.geo_fence_name_is_available("Back yard")  # False when taken
    fence = halo.add_geo_fence("Back yard", points)
```

From the CLI, boundary corners are repeated `--point LAT,LON` flags in order,
and at least three are required:

```bash
halo fence-add "Back yard" --point 40.0001,-75.0001 \
    --point 40.0002,-75.00015 --point 40.0003,-75.00005
halo fence-move FENCE_ID --point 40.0001,-75.0001 \
    --point 40.0002,-75.00015 --point 40.0004,-75.00005
```

`add_geo_fence`, `update_geo_fence_location`, and `delete_geo_fence` change where
the collar corrects the dog, and take effect once the collar syncs. Each CLI
command asks you to type the fence name or id first, and `--yes` skips that for
deliberate automation. Halo does not return the boundary that a move or delete
replaced, so re-drawing it is manual.

The optional `analytics=` argument carries the app's fence-quality telemetry
(building proximity warnings and similar). It is accepted but not required, and
this client sends `null` by default.

### Device registration and MobileId

Every instant correction carries a `MobileId`. The apps obtain it by posting
device details to `/account/mobile-data` after login and reusing the integer
Halo returns:

```bash
halo register-device
```

The id is stored in the state file and used by later corrections.
`InternalMobileId` is the same per-installation UUID this client already sends
as `appInstanceId`, so registering twice re-reads one id rather than piling up
devices. `Platform` follows the OAuth profile, while the model, manufacturer,
and version describe the machine actually running this client rather than an
invented handset; `register_mobile_device()` takes overrides for all of them.

Until you register, corrections fall back to the constant `DEFAULT_MOBILE_ID = 2`
that this client shipped with. That constant was a guess: the one captured
registration returned `3`, and the value is per-installation, so the fallback is
almost certainly not the id Halo associates with you. Corrections have been
accepted anyway, so what Halo does with the field is unknown.

### Endpoints handling sensitive data

`generate_ecommerce_login_magic_code()` mints a single-use code that signs the
account into the Halo store. It is a credential — do not log it.

`lookup_parcels()` and `halo parcels LAT LON` proxy a third-party property
database that the fence editor uses to detect buildings. Responses contain real
owner names and mailing addresses for whoever owns the land, including
neighbors, so the command prints a warning to stderr and is not summarized —
there is nothing to redact when the records are the point. Its envelope's `body`
is a JSON-encoded *string* that must be parsed a second time.

### Not implemented

This client is REST-only. The apps also open SignalR websockets for live
telemetry, talk to the collar over BLE, and drive flows we could not reproduce
without extra hardware. See the [Roadmap](#roadmap) for what is missing and why.

## Send a correction

Supported correction types:

| API value | Supported expiry |
| --- | ---: |
| `Warning` | 4 seconds |
| `FirstTime` | 4 seconds |
| `Escalation` | 4 seconds |
| `ReturnWhistle` | 7 seconds |
| `GoodBehavior` | 7 seconds |
| `HeadingHome` | 7 seconds |

The first call for a pet needs the **next known** command number:

```bash
halo correct PET_ID GoodBehavior --command-number 13
```

The counter is reserved atomically before network dispatch and subsequent calls
increment it:

```bash
halo correct PET_ID ReturnWhistle
```

The CLI checks that the assigned collar reports `socketconnected`, synchronizes
with Halo's server clock, explains the physical-action caveat, and asks you to
type the pet's name. `--yes` exists for deliberate automation.

If the official app advanced the counter, Halo returns `oldcommandnumber`. The
client stores Halo's reported current number but **does not retry**; rerun the
command only after confirming the action again.

## Python API

```python
import getpass
from halo_collar import (
    ANDROID_CLIENT_SECRET,
    ANDROID_PROFILE,
    CorrectionType,
    HaloClient,
    HaloOAuth,
    StateStore,
)

store = StateStore()
password = getpass.getpass("Halo account password: ")
with HaloOAuth(ANDROID_CLIENT_SECRET, profile=ANDROID_PROFILE) as oauth:
    tokens = oauth.password_login("you@example.com", password)
password = ""
store.save_session(
    tokens,
    client_id=ANDROID_PROFILE.client_id,
    app_version=ANDROID_PROFILE.app_version,
)

with HaloClient() as halo:
    collars = halo.collars()
    pet = halo.pet("PET_ID", refresh_telemetry=True)

    # One aggregate call instead of several, for polling.
    view = halo.account_map(37.4219983, -122.084, refresh_telemetry=True)

    # The coordinates are optional; without them Halo still returns the account.
    fences = halo.geofences()

    # Both return the same paged envelope:
    # {"pageNumber", "pageSize", "totalNumberOfPages", "totalNumberOfItems", "results"}
    walks = halo.walks(page=1, page_size=30)
    alerts = halo.notifications(page=1, page_size=30)

    # Physical action: there is intentionally no retry.
    result = halo.send_instant_correction(
        "PET_ID",
        CorrectionType.GOOD_BEHAVIOR,
        command_number=13,  # only needed to initialize the local counter
    )
```

Read responses remain dictionaries because the upstream schema can
change independently of this package.

## Roadmap

Nothing here is implemented. Everything in the first two groups is unspecified from
API behavior or response payloads currently support, not from documentation, so treat the
request shapes as unknown until support is added. The SignalR entry under
"Beyond REST" is the exception: its handshake and message shapes were captured in
full and are documented below.

### Blocked on hardware or conditions we could not reproduce

- **Walk recording.** `GET /walk/my` is covered, but starting and finishing a walk
  needs a working Bluetooth link to the collar; our attempt never connected, so no
  walk creation was ever produced.
- **Beacon mutations.** `GET /beacon/my` returns `availableRanges`, `beacons`, and
  `defaultRange`, but with no beacon hardware on the account there was nothing to
  add, rename, or remove.
- **Collar provisioning.** Pairing a collar, binding it to a pet, and unbinding or
  deleting one. The app exposes all of these, and a pet carries
  `isCollarBindingToPetSynchronized` and `isCollarEverAssigned`, but our second pet
  had no collar to attach.

### Implied by returned payloads, without write support

These fields come back on `GET`s the client already supports, which means an
endpoint exists to set them:

- **Pet mode.** `mode.fencesOn` / `mode.beaconsOn`, alongside `desiredMode` and
  `desiredModeUpdated`. Turning containment off remotely is safety-relevant and
  deserves the same care as a correction.
- **Fence assignment.** `currentGeoFenceId` selects which fence applies to a pet;
  fence CRUD is covered but choosing the active one is not.
- **Correction rule editing.** `GET /pet/{id}/correction-rules` and
  `/correction-rule/configuration-v2` are read-only here. Writing them changes
  what the collar does to the dog, so it warrants confirmation comparable to
  `send_instant_correction`.
- **Collar network settings.** `wiFiExtendedSettings` and
  `cellularExtendedSettings`.
- **Firmware updates.** `hasFirmwareUpdatesAvailable` and `firmwareUpdate`; the
  firmware feature list also advertises `fota`.
- **Calibration.** The firmware advertises `gpscalibration`,
  `compasscalibration`, and `manualgpscalibration`.
- **Pet deletion**, and account/profile edits implied by `hasChangeEmailRequest`,
  `hasCompletedQuestionnaire`, and `hasFinishedUserGuide`.

### Beyond REST

- **SignalR live telemetry.** The highest-value item, and the only one here whose
  protocol is already fully supported rather than unspecified, so it needs no further
  development. Today the only way to follow a dog is polling
  `account_map()`, which the app itself does every 16 seconds; the socket pushes
  position roughly every 5 seconds instead.

  The apps use Azure SignalR Service's redirect handshake:

  1. `POST https://halo-prod-sockets-app.azurewebsites.net/{TelemetryHub,NotificationHub}/negotiate?negotiateVersion=1`
     with the normal Halo bearer token. The response carries a
     `halo-prod-signalr.service.signalr.net` URL plus a separate short-lived
     `accessToken` for that service.
  2. `POST` that URL's `/negotiate` with the returned token to get a connection id.
  3. Open the `wss://` URL and send the handshake frame
     `{"protocol":"json","version":1}` terminated by `0x1e`, which also separates
     every later frame.

  The connection is receive-only past the handshake — the client never invokes
  anything — so a consumer only has to dispatch three server methods:
  `HandleIoTTelemetry` (the frequent one: `collarSerialNumber`, `petId`,
  `collarTelemetry`, and a `petTelemetry` object with `latitude`, `longitude`,
  `speed`, `orientation`, `activityType`, `safetyStatus`, `geoFence`, `beacon`,
  and a `manifest` with `sequenceCode` for ordering), `HandleDataStateChanged`,
  and `HandleCollarDataSynchronized`.

- **Halo Dog Park.** `app-dogpark-halo-prod.azurewebsites.net` is a separate
  service that takes the same bearer token; `GET /configuration` returns chat and
  scheduling settings. Note that this client already requests the `api.dogpark`
  OAuth scope at login but never uses it.
- **BLE.** Rolling codes and direct collar communication, used when the collar is
  in range and for setup.
- **Training content.** `training_course_link()` returns a SCORM launch URL that
  sets CloudFront signed cookies; the videos behind it are HLS with AES keys
  under separate signed URLs. Downloading them means following that chain rather
  than calling an API.

### Client ergonomics

- Pagination helpers that iterate `walks()` and `notifications()` instead of
  making callers track `pageNumber` against `totalNumberOfPages`.
- Typed models. Responses are deliberately plain dictionaries today because the
  schema is undocumented and can change without notice; typing them is
  worthwhile once the shapes prove stable.
- An async client, since the sync one is a thin wrapper over HTTPX.
- A retry policy for reads. The no-retry rule exists to protect corrections and
  other mutations; idempotent `GET`s could safely back off and retry.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
ruff check .
```

Tests use HTTPX's in-memory transport and do not contact Halo.
