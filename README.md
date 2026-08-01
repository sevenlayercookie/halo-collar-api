# Unofficial Halo Collar Python client

> [!IMPORTANT]
> This is an unofficial project. It is not affiliated with, endorsed by, or
> supported by Halo Collar.

This project is a conservative Python client for the Halo Collar REST and
SignalR APIs.

## Documentation

- [Detailed Python and upstream API reference](API.md)
- [CLI usage](#using-the-cli)
- [Safety and privacy](#safety-and-privacy)
- [Roadmap](#roadmap)

Implemented functionality:

- OAuth 2.0 password login using the Android profile
- OAuth 2.0 Authorization Code login with PKCE through Halo's hosted login page
- Refresh-token login and automatic access-token refresh/rotation
- Public application configuration
- Account collars and connectivity
- Read-only installed firmware and server-managed update progress
- Pet listing (including collarless pets), details, creation, editing, and
  optional telemetry refresh, plus containment and beacon mode changes
- The aggregate map view, geofence create/rename/move/delete, and safe-zone preview
- User profile, complete beacon management, subscription, and in-app notifications
- Walk history, remote pause/stop commands, completed-walk submission and images
- Notification history and marking notifications read
- Correction-rule reading/editing and the sound/vibration intensity catalog
- Training course progress and course launch links
- Halo server-clock synchronization
- Collar locate tone and push-notification registration
- One-shot instant corrections and unsaved sound/vibration/static collar tests
- Async SignalR streams for live telemetry, data-state, collar-sync, and
  notification-hub events

BLE rolling codes, DGNSS forwarding, and ordinary walk initiation are not
implemented.

## Safety and privacy

An instant correction or direct collar test is a physical action. The server
returning `success` means the cloud accepted the command; it does not prove the
collar executed it. The client never retries after a transport failure or
ambiguous dispatch. A definite HTTP 401 is refreshed and retried once because
the API rejected the original request before accepting the command. A timeout is
reported as an unknown outcome.

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

Runtime networking uses [HTTPX](https://www.python-httpx.org/) for HTTP and
[websockets](https://websockets.readthedocs.io/) for SignalR. Python 3.10 or
newer is required.

## Login with email and password

The simplest headless login uses the Android profile's password grant:

```bash
halo --timezone America/Chicago auth login --password
```

It prompts for the account email and securely prompts for the account password.
The password is sent only to Halo's HTTPS token endpoint and is never written to
the state file. The resulting access token, rotating refresh token, and Android
OAuth profile are stored in the owner-only state file. Future API calls refresh
automatically; you do not need to enter the password again unless the refresh
token dies or is revoked.

Both the iOS and Android OAuth client credentials are embedded in the package
because they are application constants rather than per-user secrets. No login
mode needs you to supply one.

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
halo --timezone America/Chicago auth login
```

The command uses the iOS profile and opens Halo's hosted login page with
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
halo --timezone America/Chicago auth login --no-browser --browser-capture
```

Transfer the capture to the machine running this tool and enter its path. Only
the `Location` callback is read from it; browser cookies are ignored and never
copied into Halo's state file. Callback state is validated locally and Halo
validates the PKCE verifier during the token exchange.

To bootstrap from an existing capture without putting tokens in shell history:

```bash
halo --timezone America/Chicago auth login --from-refresh-token
```

The refresh token is prompted without echo. Halo binds refresh tokens to the
client that issued them, so pick the matching `--platform`; the stored session
records which client it belongs to and will not be reused under another one. The
session is stored outside the repository in an atomic state file with `0600`
permissions on POSIX systems. On
Linux the default is `~/.local/state/halo-collar/state.json`. Use `halo auth logout`
to remove the state and command counters.

If Halo returns `invalid_grant`, the refresh token has expired, was revoked, or
the one-time authorization grant is invalid. The client stops and asks for a new
login and removes the dead tokens while retaining non-token settings. It never
stores the Halo account password.

## Using the CLI

Commands are `noun verb`:

```bash
halo                      # concise help
halo help                 # every noun
halo help pet             # every verb on a noun
halo pet add --help       # one command, with examples
```

Every noun and every verb answers `-h`/`--help`, and `halo <noun>` on its own
prints that noun's help rather than an error.

These flags work on every command, before or after the verb:

| Flag | Effect |
| --- | --- |
| `--json` | Print Halo's data as JSON instead of a table; live streams are already JSONL |
| `--plain` | Tab-separated rows with no alignment, for `grep` and `awk`; live streams remain JSONL |
| `--quiet`, `-q` | Suppress notices on stderr; data and errors still print |
| `--no-input` | Never prompt; commands needing confirmation fail unless `--yes` |
| `--state-file` | Override the owner-only credential/counter state path |
| `--timezone` | IANA timezone sent in Halo-Client |

Data goes to stdout and notices go to stderr, so `halo pet list > pets.txt`
captures only the pets while still telling you what happened.

Output is a table wherever the payload is shallow enough for one. Scalar fields
become a `FIELD`/`VALUE` table, an embedded list of flat records becomes its own
table under its field name, and a small nested object becomes a table of its
own, in the same key order `--json` uses:

```
$ halo account subscription
FIELD                  VALUE
accessLevel            basic
accountActivationDate  2026-06-09T12:45:45.8419848Z

FEATURES
ID                        ISENABLED
viewpetbehaviortraining   false
refreshtelemetry          true
…
```

Complex payloads remain JSON so no fields are lost or misrepresented.

`--full` prints the unredacted payload, which is JSON in practice because those
payloads are the nested ones. Booleans
and nulls print as Halo spells them, `true` and `null`, rather than as English;
a `-` in a column means there was nothing to show, which is not the same as Halo
returning null.

Exit codes: `0` success, `1` nothing to do or the user cancelled, `2` usage or
API error, `3` stale command number, `4` correction outcome unknown, `5` safety
check refused, `130` interrupted.

## Read data

```bash
halo auth status
halo system config
halo collar list
halo pet list
halo fence list
halo pet show PET_ID
halo pet show PET_ID --refresh-telemetry
halo account map
halo account map --latitude 37.4219983 --longitude -122.084
halo walk list --page 1 --page-size 30
halo notification list --page 1 --page-size 30
halo account profile
halo beacon list
halo account subscription
halo notification inbox
halo correction config
halo system time
halo video list
```

`halo video list` prints a `name -> HLS stream URL` list of the 22 videos the
apps play — onboarding, training, and subscription screens — pulled out of the
configuration payload, with `--full` adding thumbnails and the section each came
from. Nothing here is account data: the URLs carry no signature and
`/configuration/` needs no login, so this is the one read command that works
logged out, and the streams play directly in `mpv`, `ffplay`, or VLC. The same
payload also holds the collar's 21 correction sounds and 6 vibration files as
plain `.mp3` URLs under `halo correction config`.

`halo notification inbox` reads `/portal-notification/my/in-app/`, a different
feed from the `/notification/my/query` history behind `halo notification list`.
Halo sends all twenty-one notification columns for every type and nulls the ones
that do not apply, so `notification list` shows when, which pet, the type, and
the one field that type populates — battery percentage, correction count, zone,
walk duration, or beacon — with the rest behind `--full`. The paging line goes
to stderr, so a redirected `notification list` captures only rows.
`halo account profile` summarizes by default because the payload carries your
email addresses, avatar URL, and referral link; `beacon list`, `account
subscription`, `notification inbox`, and `correction config` print in full
because nothing in their returned payloads needs hiding.

`halo collar list`, `halo pet list`, `halo fence list`, and `halo account map`
print privacy-reduced summaries rather than full Wi-Fi, coordinate, and
signed-report-URL data. Pass `--full` to any of them for Halo's complete
response. Nothing is withheld from you: the summary is only the default so that
a command you ran to check a battery level does not put your home coordinates
into a screenshot, a shell history, or a pasted bug report. The Python API
always returns whole payloads.

`halo pet list` reaches every pet on the account, including ones that have never
had a collar assigned and so never appear in `halo collar list`. Use `halo pet
show PET_ID` for the full object; it is unsummarized and needs no `--full`.

`halo fence list` lists the account's geofences, which Halo returns only inside
the map payload. The summary keeps names, enabled state, zone types with a point
count, and per-pet sync status, and drops the zone polygons, the fence address,
and the signed thumbnail URL.

`halo account map` calls `/account/my/map`, which the app polls on its home screen. One
response returns `pets` (each with its collar embedded), `geoFencesInfo`, and
`corrections`, so prefer it over several separate calls when polling. The apps
always send a viewport centre, but Halo returns the whole account without one,
so the coordinates are optional here and on `HaloClient.account_map`. The
summary counts `corrections` rather than redacting them because the records have
no stable privacy-safe summary shape.

## Stream live events

The CLI can keep either SignalR hub open and print one compact JSON
object per line until `Ctrl-C`:

```bash
halo live telemetry
halo live notifications

# Filters can be combined; repeat --target to allow more than one method.
halo live telemetry --pet-id PET_ID --target HandleIoTTelemetry
```

Every line contains Halo's complete invocation plus a `hub` field:

```json
{"arguments":[{"petId":"PET_ID"}],"hub":"TelemetryHub","target":"HandleIoTTelemetry","type":1}
```

The output is always JSON Lines, regardless of `--json` or `--plain`, so it can
be piped directly into tools such as `jq`. Status and privacy notices go to
stderr and can be suppressed with `--quiet`. The event payload is deliberately
unredacted and telemetry may contain precise pet locations; take care when
redirecting, logging, or sharing it. The client refreshes authentication,
maintains heartbeats, and renegotiates automatically after transient
disconnects.

## Locate a collar

```bash
halo collar locate COLLAR_ID
```

This plays the collar's locate tone. It is a physical action, but an
audible-only one — unlike a correction it is not aversive, so no command number
is reserved and Halo answers `204 No Content`.

## Provision a collar

Check whether the printed serial can be added to the authenticated account:

```bash
halo collar check-binding PRINTED_SERIAL
```

Binding also requires the encrypted serial read from the physical collar over
Bluetooth. The printed serial and the hardware `uuId` returned by account APIs
are not known substitutes:

```bash
halo collar bind PRINTED_SERIAL ENCRYPTED_SERIAL
```

The bind command asks you to type the printed serial before changing the
account. Its response contains the server-issued `collar.id`. Attach that UUID,
not the serial number or ESN, to a pet:

```bash
halo pet bind-collar PET_ID COLLAR_ID
halo pet show PET_ID --refresh-telemetry
halo collar show COLLAR_ID
```

The empty successful bind response is only an HTTP acknowledgement. Treat the
attachment as applied when the refreshed pet has the requested `collarInfo.id`
and `isCollarBindingToPetSynchronized: true`; `petInfo.id` on the collar is the
reciprocal check.

Detaching a collar from a pet keeps it registered to the account. Removing it
from the account is a separate operation:

```bash
halo pet unbind-collar PET_ID
halo collar remove COLLAR_ID
```

The account-removal cascade for a collar that is still assigned to a pet has
not been directly observed. Use the two commands in that order for an explicit
two-stage removal. All four write commands ask for confirmation; pass `--yes`
for an intentional non-interactive call. Deleting a pet remains a distinct,
destructive operation and there is no `DELETE /collar/{id}` route.

## Read firmware status

Firmware rollout is server-managed. The accessible client surface is read-only:

```bash
halo firmware list
halo firmware list --full
halo firmware show COLLAR_ID
```

These commands use only `GET /collar/my` and `GET /collar/{id}`. The summary
shows the installed version, target version, asynchronous update state, and the
server's availability and production/beta markers. `--full` includes complete
firmware features and nested target metadata.

An active `firmwareUpdate` can report download, verification, delayed, applying,
failure, or applied states inside an otherwise successful HTTP response. After
installation, confirm that the installed firmware ID/version changed to the
target and that `firmwareUpdate` ultimately returned to null. The Notification
SignalR stream may also carry `FirmwareUpdateIsApplying`,
`FirmwareUpdateApplied`, or `FirmwareUpdateFailed` notifications:

```bash
halo live notifications
```

There is intentionally no firmware start, cancel, package-download, or
beta/production-selection command. Capability strings such as `fota` and
`firmwareupdatecancellation` describe the collar; they do not establish a
customer-facing HTTP operation.

## Pet containment and beacon modes

Containment fences and beacon assignment use separate operations:

```bash
halo pet fences PET_ID off
halo pet beacons PET_ID on
```

Both commands ask you to type the pet ID before changing collar behavior. Pass
`--yes` for an intentional non-interactive call.

The equivalent Python methods are:

```python
mode = client.set_pet_fences_enabled(pet_id, False)
client.set_pet_beacons_assigned(pet_id, True)
```

For containment responses, `desiredMode` is the target accepted by the cloud,
while `telemetry.mode` is the last state reported by the collar. If
`desiredMode.fencesOn` differs from `telemetry.mode.fencesOn`, the requested
change has not yet been confirmed as applied. Responses remain dictionaries so
additional server fields are preserved without assigning them speculative
meaning. Turning fences off can disable physical containment; verify the
collar-reported mode before relying on the change.

## Beacon management

List calls return account beacons together with the server's valid range
configuration:

```bash
halo beacon list
halo beacon check-name Kitchen
halo beacon check-binding BEACON_SERIAL
halo beacon sync BEACON_ID
```

Add, update, and delete require confirmation or `--yes`:

```bash
halo beacon add --name Kitchen --serial-number BEACON_SERIAL \
    --model-type Usb --action-type KeepAway --should-notify \
    --range-level 3 --radius-in-decibel -50
halo beacon update BEACON_ID --name "Back Door" \
    --action-type IgnoreFences --range-level 5 --radius-in-decibel -57
halo beacon delete BEACON_ID
```

The Python methods are `beacon_name_is_available()`,
`check_beacon_binding()`, `add_beacon()`, `update_beacon()`, and
`delete_beacon()`. `update_beacon()` sends only supplied keyword fields;
explicit `None` sends JSON null, while omission leaves the field out.

Beacon records are account-scoped. `set_pet_beacons_assigned(pet_id, enabled)`
enables or disables the account's beacon configuration as a whole for one pet;
it does not select an individual beacon. Poll `beacon_pet_sync(beacon_id)` until
the pet's status is `completed`. `pending` means distribution is in progress,
and `skipped` is preserved as a distinct server result.

Phone-discovered battery observations can be uploaded from a JSON array:

```bash
halo beacon telemetry readings.json
```

Each element uses PascalCase `SerialNumber` and `BatteryChargePercent`.
`upload_beacon_telemetry()` updates cloud telemetry only; it does not configure
the nearby physical beacon.

## Endpoint coverage

Supported upstream routes:

| Method | Path | Client method |
| --- | --- | --- |
| GET | `/configuration/` | `configuration()`, `videos()` |
| GET | `/collar/my` | `collars()`, `firmware_statuses()` |
| GET | `/collar/{id}` | `collar()`, `firmware_status()` |
| PUT | `/collar/check-can-be-bound-to-user` | `check_collar_binding()` |
| PUT | `/collar/bind-to-user` | `bind_collar()` |
| POST | `/collar/{id}/unbind-from-user` | `unbind_collar_from_user()` |
| GET | `/pet/my` | `pets()` |
| GET | `/pet/{id}` | `pet()` |
| PUT | `/pet/{id}/bind-collar` | `bind_collar_to_pet()` |
| PUT | `/pet/{id}/unbind-collar` | `unbind_collar_from_pet()` |
| GET | `/account/my/map` | `account_map()`, `geofences()`, `geo_fence_pet_sync()` |
| POST | `/account/mobile-data` | `register_mobile_device()` |
| GET | `/user-profile/` | `user_profile()` |
| GET | `/beacon/my` | `beacons()`, `beacon_pet_sync()` |
| PUT | `/beacon/check-name-uniqueness` | `beacon_name_is_available()` |
| POST | `/beacon` | `add_beacon()` |
| PUT | `/beacon/{id}` | `update_beacon()` |
| DELETE | `/beacon/{id}` | `delete_beacon()` |
| PUT | `/beacon/telemetry` | `upload_beacon_telemetry()` |
| PUT | `/beacon/check-can-be-bound-to-user` | `check_beacon_binding()` |
| GET | `/subscription/my/` | `subscription()` |
| GET | `/walk/my` | `walks()` |
| GET | `/walk/{id}/summary` | `walk_summary()` |
| POST | `/walk/{id}/set-is-paused` | `set_walk_paused()` |
| POST | `/walk/{id}/stop` | `stop_walk()` |
| POST | `/walk/{id}/mark-ended` | `mark_walk_ended()` |
| PUT | `/walk/{id}/trail-thumbnail` | `upload_walk_trail_thumbnail()` |
| PUT | `/walk/{id}/pet/{petId}/trail-image` | `upload_walk_pet_trail_image()` |
| GET | `/notification/my/query` | `notifications()` |
| GET | `/portal-notification/my/in-app/` | `portal_notifications()` |
| GET | `/mapbox/request/my` | `mapbox_requests()` |
| GET | `/system/server-date-time` | `server_time()` |
| GET | `/pet/colors` | `pet_colors()` |
| GET | `/pet/{id}/correction-rules` | `pet_correction_rules()` |
| GET | `/correction-rule/configuration-v2` | `correction_rule_configuration()` |
| PUT | `/correction-rule` | `update_correction_rules()` |
| PUT | `/correction-rule/test-on-collar` | `test_correction_on_collar()` |
| GET | `/training/my-v2` | `training()` |
| GET | `/training/user/course-launch-link/{curriculum}/{course}` | `training_course_link()` |
| POST | `/pet/{id}/run-instant-correction/` | `send_instant_correction()` |
| POST | `/pet/add` | `add_pet()` |
| PUT | `/pet/{id}` | `update_pet()` |
| DELETE | `/pet/{id}` | `delete_pet()` |
| PUT | `/pet/{id}/instant-mode` | `set_pet_fences_enabled()` |
| PUT | `/beacon/set-is-assigned/{petId}` | `set_pet_beacons_assigned()` |
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

Paths use the route-specific spelling and trailing slashes expected by the
upstream API. `/walk/my` pages with `page`/`pageSize` while
`/notification/my/query` pages with `Page`/`PageSize`; that inconsistency is
Halo's, not a typo.

Request DTO keys use PascalCase. Response dictionaries retain the lower
camel-case keys returned by Halo rather than normalizing them.

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
requires all five fields. `halo pet update` reads the pet first and fills in
whatever you did not pass, which keeps a single `--weight-kg` from blanking the
name and breed:

```bash
halo pet colors                      # colorHex must come from this list
halo pet add --name Scout --color-hex "#FF7A00" --breed goldenretriever \
    --birthday 2021-04-17 --weight-kg 28.5
halo pet update PET_ID --weight-kg 29.2
halo pet delete PET_ID
```

Saving marks the collar's configuration `outdated` until it next syncs. A new
pet has no collar until one is bound, so it appears in `halo pet list` but not in
`halo collar list`.

`delete_pet()` removes the pet and the history Halo keeps under it. Halo answers
200 with an empty body rather than returning what it deleted, so nothing here
undoes it; `halo pet delete` asks you to type the pet's name first.

### Walks

An ordinary phone-started walk cannot be initiated through HTTP. The phone
generates the walk UUID locally and sends it to participating collars while
enabling leash mode over Bluetooth. This client deliberately has no
`start_walk()` method.

Once a walk exists, its HTTP operations are available:

```bash
halo walk summary WALK_ID
halo walk pause WALK_ID COLLAR_ID
halo walk resume WALK_ID COLLAR_ID
halo walk stop WALK_ID COLLAR_ID --stop-option ForceSetFencesOn
```

Pause, resume, and stop ask you to type the collar ID. Pass `--yes` for an
intentional non-interactive command.

```python
from halo_collar import HaloClient, WalkStopOption

with HaloClient() as halo:
    halo.set_walk_paused(walk_id, collar_id, True)
    halo.set_walk_paused(walk_id, collar_id, False)
    halo.stop_walk(
        walk_id,
        collar_id,
        stop_option=WalkStopOption.FORCE_SET_FENCES_ON,
    )
```

Pause and stop return Halo's command-result dictionary unchanged. A
`result: "success"` acknowledges the command but does not prove that the collar
applied it. Confirm pause using fresh telemetry for the same walk ID and
`isPaused`; confirm stop when fresh collar telemetry reports `walk: null`.
Stopping one collar does not finalize a multi-pet walk.

Finalization submits aggregate PascalCase summary dictionaries, then uploads
the rendered overall and per-pet trail images separately:

```bash
halo walk mark-ended WALK_ID summary.json
halo walk upload-thumbnail WALK_ID overview.png
halo walk upload-pet-image WALK_ID PET_ID pet-trail.png
```

The CLI summary file is one JSON object containing `StartedAt`, `EndedAt`,
`Pets`, `User`, and `LocationName`, with the same nested fields shown below.
Use `--content-type image/jpeg` when an uploaded file is a JPEG; image commands
default to `image/png`.

```python
halo.mark_walk_ended(
    walk_id,
    started_at="2026-07-30T18:10:00Z",
    ended_at="2026-07-30T18:41:12Z",
    pets=[{
        "Id": pet_id,
        "CollarId": collar_id,
        "WalkedDurationInSeconds": 1815,
        "WalkedDistanceInMeters": 2431.7,
        "FeedbacksCount": 2,
        "Timestamp": "2026-07-30T18:41:10Z",
    }],
    user={
        "TotalDuration": "00:31:12",
        "WalkedDuration": "00:30:15",
        "WalkedDistanceInMeters": 2388.4,
    },
    location_name="Rochester, Minnesota",
)
halo.upload_walk_trail_thumbnail(walk_id, overview_png)
halo.upload_walk_pet_trail_image(walk_id, pet_id, pet_trail_png)
```

These requests do not upload raw coordinate arrays. `walk_summary(walk_id)`
returns the completed summary; `endedAt` confirms summary creation, while
non-null `trailThumbnailImageUrl` and `pets[].trailImageUrl` confirm that the
separate images are available. Summary submission and image upload may complete
at different times.

### Fences

Halo has no endpoint that lists fences on their own. `geofences()` and `halo
fence list` read the `geoFencesInfo.geoFencesToDisplay` array out of the map payload,
so listing fences costs one `/account/my/map` call. Note `geoFencesTotalCount`:
`halo account map` reports it, and a count larger than the returned array means Halo
truncated the list.

Fence geometry is a list of `(latitude, longitude)` corners; Halo needs at least
three. The submitted preview points describe the warning boundary, and Halo
returns the generated safe-zone geometry before the client saves:

```python
points = [(40.0001, -75.0001), (40.0002, -75.00015), (40.0003, -75.00005)]

with HaloClient() as halo:
    halo.geo_fence_safe_zones(points)          # preview, changes nothing
    halo.geo_fence_name_is_available("Back yard")  # False when taken
    fence = halo.add_geo_fence("Back yard", points)
    sync = halo.geo_fence_pet_sync(fence["geoFence"]["id"])
```

From the CLI, boundary corners are repeated `--point LAT,LON` flags in order,
and at least three are required:

```bash
halo fence add "Back yard" --point 40.0001,-75.0001 \
    --point 40.0002,-75.00015 --point 40.0003,-75.00005
halo fence move FENCE_ID --point 40.0001,-75.0001 \
    --point 40.0002,-75.00015 --point 40.0004,-75.00005
```

`add_geo_fence`, `update_geo_fence_location`, and `delete_geo_fence` change where
the collar corrects the dog, and take effect once the collar syncs. Each CLI
command asks you to type the fence name or id first, and `--yes` skips that for
deliberate automation. Halo does not return the boundary that a move or delete
replaced, so re-drawing it is manual.

Halo fences are account-scoped and automatically associated with the account's
pets. Each fence's `petsSync` entries describe the distribution result using
`petId`, `isAssigned`, and a synchronization `status` of `unknown`, `pending`,
`completed`, or `skipped`. Collared pets progress from `pending` to `completed`.
For collarless pets, `skipped` is expected and does not by itself mean the fence
is unassigned. `geo_fence_pet_sync(fence_id)` reads this list.

`currentGeoFenceId` is different: it is a nullable, response-only pet field
identifying the fence last reported as active by the collar. It may match
`telemetry.geoFence.id`, but it is not the complete assignment set and is not
sent by `update_pet`. Multiple account fences can be synchronized to one pet at
the same time; the collar determines which one it currently reports.

After creating or updating a fence, poll `account_map()` until the relevant
`petsSync.status` values are `completed` and the pet reports
`isFencesSynchronized: true`. Granular assignment, unassignment, and
`currentGeoFenceId` mutation are unsupported and undocumented; the client does
not guess routes or add these response-only fields to pet updates.

The optional `analytics=` argument carries the app's fence-quality telemetry
(building proximity warnings and similar). It is accepted but not required, and
this client sends `null` by default.

### Device registration and MobileId

Every instant correction carries a `MobileId`. The apps obtain it by posting
device details to `/account/mobile-data` after login and reusing the integer
Halo returns:

```bash
halo device register
```

The id is stored in the state file and used by later corrections.
`InternalMobileId` is the same per-installation UUID this client already sends
as `appInstanceId`, so registering twice re-reads one id rather than piling up
devices. `Platform` follows the OAuth profile, while the model, manufacturer,
and version describe the machine actually running this client rather than an
invented handset; `register_mobile_device()` takes overrides for all of them.

Until you register, corrections fall back to `DEFAULT_MOBILE_ID = 2`. The value
is per installation, so the fallback is unlikely to be the id Halo associates
with you. Register the device before sending corrections; the exact server-side
role of this field is unknown.

### Endpoints handling sensitive data

`generate_ecommerce_login_magic_code()` mints a single-use code that signs the
account into the Halo store. It is a credential — do not log it.

`lookup_parcels()` and `halo parcel lookup --latitude LAT --longitude LON` proxy a third-party property
database that the fence editor uses to detect buildings. Responses contain real
owner names and mailing addresses for whoever owns the land, including
neighbors, so the command prints a warning to stderr and is not summarized —
there is nothing to redact when the records are the point. Its envelope's `body`
is a JSON-encoded *string* that must be parsed a second time.

### Not implemented

The apps also talk to the collar over BLE and drive flows we could not reproduce
without extra hardware. See the [Roadmap](#roadmap) for what is missing and why.

## Send a correction

Correction types:

| API value | Expiry |
| --- | ---: |
| `Warning` | 4 seconds |
| `FirstTime` | 4 seconds |
| `Escalation` | 4 seconds |
| `ReturnWhistle` | 7 seconds |
| `GoodBehavior` | 7 seconds |
| `HeadingHome` | 7 seconds |

The first call for a pet needs the **next known** command number:

```bash
halo correction send PET_ID GoodBehavior --command-number 13
```

The counter is reserved atomically before network dispatch and subsequent calls
increment it:

```bash
halo correction send PET_ID ReturnWhistle
```

The CLI checks that the assigned collar reports `socketconnected`, synchronizes
with Halo's server clock, explains the physical-action caveat, and asks you to
type the pet's name. `--yes` exists for deliberate automation.

If the official app advanced the counter, Halo returns `oldcommandnumber`. The
client stores Halo's reported current number but **does not retry**; rerun the
command only after confirming the action again.

## Edit and test correction rules

Read the pet's rule IDs and the server's current asset/level catalog before
editing:

```bash
halo correction rules PET_ID
halo correction config
```

Each rule ID already identifies its pet and escalation slot. A single-rule edit
therefore sends no pet ID or escalation value, and omitted rules are left alone:

```bash
halo correction update RULE_ID Sound --level 3 --sound-id SOUND_ID
halo correction update RULE_ID Vibration --vibration-id VIBRATION_ID
halo correction update RULE_ID Shock --level 1
```

`Shock` is the HTTP name for static feedback. Every update requires confirmation
because it changes future collar behavior. A successful response proves the
server stored the edit, not that the collar installed it. Read the rules again,
then wait for a fresh collar response whose overall
`configurationSyncStatus` is `uptodate`; Halo exposes no correction-specific
applied version or timestamp.

Test a proposed setting without saving it as a rule:

```bash
halo correction test PET_ID Sound --level 1 --sound-id SOUND_ID --command-number 13
halo correction test PET_ID Vibration --vibration-id VIBRATION_ID
halo correction test PET_ID Shock --level 1
```

The collar test is a one-shot physical command. It checks connectivity, reserves
the same per-pet command counter used by instant corrections, uses Halo's server
clock, expires after 30 seconds by default, and is never retried after ambiguous
dispatch. Remove the collar from the dog and start at the lowest safe level.

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

Read responses remain dictionaries because the upstream schema can change
independently of this package.

## Live events with SignalR

`HaloSignalRClient` is an async, receive-only companion to `HaloClient`. It
performs Halo's two-stage Azure negotiation, completes the JSON
SignalR handshake, sends keepalives, and repeats the complete negotiation after
a transient disconnect so that neither the Halo access token nor Azure's
short-lived connection token is reused incorrectly.

```python
import asyncio

from halo_collar import HaloClient, HaloSignalRClient, SignalRHub


async def follow() -> None:
    with HaloClient() as halo:
        async with HaloSignalRClient(halo, hub=SignalRHub.TELEMETRY) as live:
            async for event in live:
                print(event.target, event.pet_id, event.sequence_code)


asyncio.run(follow())
```

Telemetry events arrive as `SignalREvent` objects. `target` is the server method
(`HandleIoTTelemetry`, `HandleDataStateChanged`, or
`HandleCollarDataSynchronized`), `arguments` and `raw`
preserve Halo's complete payload, and the `pet_id` and `sequence_code`
properties pull out the two common routing fields without hiding anything.
Pass `SignalRHub.NOTIFICATIONS` to connect to `NotificationHub`.

The reader runs in a background task so brief work by the consumer does not
stop socket heartbeats. Its queue is bounded: a consumer that stays behind
raises `SignalRBackpressureError` rather than silently dropping position
updates. One `HaloSignalRClient` supports one event consumer; create a second
client to consume the other hub concurrently.

## Roadmap

Nothing here is implemented. The request shapes for these features are not part
of this client's supported API.

### Additional write operations

These fields are returned by supported reads, but their write operations are
not implemented:

- **Collar network settings.** `wiFiExtendedSettings` and
  `cellularExtendedSettings`.
- **Calibration.** The firmware advertises `gpscalibration`,
  `compasscalibration`, and `manualgpscalibration`.
- **Account/profile edits** implied by `hasChangeEmailRequest`,
  `hasCompletedQuestionnaire`, and `hasFinishedUserGuide`.

### Beyond REST

- **Halo Dog Park.** `app-dogpark-halo-prod.azurewebsites.net` is a separate
  service that takes the same bearer token; `GET /configuration` returns chat and
  scheduling settings. Note that this client already requests the `api.dogpark`
  OAuth scope at login but never uses it.
- **BLE.** Rolling codes and direct collar communication, used when the collar is
  in range, for setup, and to initiate an ordinary phone-started walk. HTTP can
  pause, stop, finalize, and retrieve an existing walk but cannot reproduce that
  start sequence.
- **Training content.** `training_course_link()` returns a SCORM launch URL that
  sets CloudFront signed cookies; the videos behind it are HLS with AES keys
  under separate signed URLs. Downloading them means following that chain rather
  than calling an API.

### Client ergonomics

- Pagination helpers that iterate `walks()` and `notifications()` instead of
  making callers track `pageNumber` against `totalNumberOfPages`.
- Typed models. Responses are deliberately plain dictionaries today because the
  upstream schema can change without notice; typing them is worthwhile once the
  shapes prove stable.
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
