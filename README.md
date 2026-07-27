# Unofficial Halo Collar Python client

This project is a conservative Python client for REST traffic observed from the
Halo Collar iOS and Android apps. It is not affiliated with or supported by Halo.

Implemented, observed functionality:

- OAuth 2.0 password login using the observed Android client
- OAuth 2.0 Authorization Code login with PKCE through Halo's hosted login page
- Refresh-token login and automatic access-token refresh/rotation
- Public application configuration
- Account collars and connectivity
- Pet details and optional telemetry refresh
- User profile, beacons, subscription, and in-app notifications
- Halo server-clock synchronization
- One-shot instant corrections for the six observed correction enums

SignalR/WebSocket traffic, BLE rolling codes, DGNSS forwarding, push
registration, and unobserved mutations are intentionally not implemented.

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

Never commit packet captures, account passwords, access/refresh tokens, browser
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

The simplest headless login uses the password grant observed in Android traffic:

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

The command uses the observed iOS profile and opens Halo's hosted login page with
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
halo pet PET_ID
halo pet PET_ID --refresh-telemetry
```

`halo collars` prints a privacy-reduced summary rather than full Wi-Fi and
telemetry data.

## Send a correction

Observed correction types:

| API value | Observed expiry |
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

    # Physical action: there is intentionally no retry.
    result = halo.send_instant_correction(
        "PET_ID",
        CorrectionType.GOOD_BEHAVIOR,
        command_number=13,  # only needed to initialize the local counter
    )
```

Read responses remain dictionaries because the reverse-engineered schema can
change independently of this package.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
ruff check .
```

Tests use HTTPX's in-memory transport and do not contact Halo.
