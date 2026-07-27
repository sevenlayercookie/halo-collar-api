"""OAuth 2.0 login and refresh-token support for supported Halo mobile clients."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from .errors import AuthenticationError, InvalidCallbackError, LoginRequiredError
from .models import AuthorizationFlow, TokenSet

AUTH_BASE_URL = "https://auth.halocollar.com"
AUTHORIZE_PATH = "/connect/authorize"
TOKEN_PATH = "/connect/token"
REDIRECT_URI = "haloapp://callback"
SCOPE = "openid email offline_access api.dogpark"
# These are application credentials distributed inside the official mobile apps,
# not per-user secrets. Account passwords and issued OAuth tokens remain private.
IOS_CLIENT_SECRET = "ZfmP^5M2M$98R8A%"
ANDROID_CLIENT_SECRET = "34fcPOX6rChDi83@"


@dataclass(frozen=True, slots=True)
class OAuthClientProfile:
    """Supported metadata that must stay paired with a mobile OAuth session."""

    name: str
    client_id: str
    app_version: str
    token_user_agent: str


IOS_PROFILE = OAuthClientProfile(
    name="iOS",
    client_id="halo.app.ios",
    app_version="2.12.0.1030",
    token_user_agent="Halo/1030 CFNetwork/3890.100.1 Darwin/27.0.0",
)
ANDROID_PROFILE = OAuthClientProfile(
    name="Android",
    client_id="halo.app.android",
    app_version="2.12.0.590",
    token_user_agent="Dalvik/2.1.0 (Linux; U; Android 13)",
)
CLIENT_PROFILES = {
    "ios": IOS_PROFILE,
    "android": ANDROID_PROFILE,
}
EMBEDDED_CLIENT_SECRETS = {
    IOS_PROFILE.client_id: IOS_CLIENT_SECRET,
    ANDROID_PROFILE.client_id: ANDROID_CLIENT_SECRET,
}


def client_profile(client_id: str) -> OAuthClientProfile:
    for profile in CLIENT_PROFILES.values():
        if profile.client_id == client_id:
            return profile
    raise ValueError(f"Unsupported Halo OAuth client ID: {client_id}")


def build_http_client() -> httpx.Client:
    """Create the HTTP client shared by the auth and API layers."""

    return httpx.Client(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=False,
        headers={"Accept": "application/json"},
    )


def resolve_client_secret(
    profile: OAuthClientProfile,
    *,
    explicit: str | None = None,
) -> str | None:
    """Resolve one profile's app credential from the supported sources.

    The embedded constant is the lowest priority so that a release updating it
    reaches everyone, and the environment variables stay available for adopting a
    rotated credential before such a release exists. The generic
    ``HALO_CLIENT_SECRET`` is profile-agnostic; prefer the per-profile variable
    when more than one profile is in use.
    """

    return (
        explicit
        or os.environ.get(f"HALO_{profile.name.upper()}_CLIENT_SECRET")
        or os.environ.get("HALO_CLIENT_SECRET")
        or EMBEDDED_CLIENT_SECRETS.get(profile.client_id)
    )


def _urlsafe_random(byte_count: int = 48) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(byte_count)).rstrip(b"=").decode("ascii")


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class HaloOAuth:
    """Authenticate with one supported Halo mobile OAuth client."""

    def __init__(
        self,
        client_secret: str,
        *,
        profile: OAuthClientProfile = IOS_PROFILE,
        http: httpx.Client | None = None,
        auth_base_url: str = AUTH_BASE_URL,
    ) -> None:
        if not client_secret:
            raise ValueError(f"Halo's {profile.name} client secret is required.")
        self.client_secret = client_secret
        self.profile = profile
        self.auth_base_url = auth_base_url.rstrip("/")
        self._owns_http = http is None
        self.http = http or build_http_client()

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> HaloOAuth:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def begin_login(self) -> AuthorizationFlow:
        # The iOS profile uses a 32-byte verifier (43 base64url chars).
        # Although PKCE permits longer values, match Halo's supported model exactly.
        verifier = _urlsafe_random(32)
        state = _urlsafe_random(32)
        nonce = _urlsafe_random(32)
        query = urlencode(
            {
                "response_type": "code",
                "response_mode": "query",
                "client_id": self.profile.client_id,
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPE,
                "code_challenge": _pkce_challenge(verifier),
                "code_challenge_method": "S256",
                "state": state,
                "nonce": nonce,
                "prompt": "login",
            }
        )
        return AuthorizationFlow(
            url=f"{self.auth_base_url}{AUTHORIZE_PATH}?{query}",
            code_verifier=verifier,
            state=state,
            nonce=nonce,
        )

    def complete_login(self, callback_url: str, flow: AuthorizationFlow) -> TokenSet:
        parsed = urlsplit(callback_url.strip())
        if parsed.scheme.casefold() != "haloapp" or parsed.netloc.casefold() != "callback":
            raise InvalidCallbackError(
                "Expected a callback beginning with haloapp://callback. No credentials were sent."
            )
        # Authorization Code normally uses the query, but accepting a fragment
        # makes pasted callbacks resilient to browser/proxy presentation.
        parameters = parse_qs(parsed.query or parsed.fragment)
        if parameters.get("state", [None])[0] != flow.state:
            raise InvalidCallbackError(
                "The callback state does not match this login attempt. Start login again."
            )
        oauth_error = parameters.get("error", [None])[0]
        if oauth_error:
            description = parameters.get("error_description", ["No details were provided."])[0]
            raise AuthenticationError(f"Halo login returned {oauth_error}: {description}")
        code = parameters.get("code", [None])[0]
        if not code:
            raise InvalidCallbackError("The Halo callback does not contain an authorization code.")
        return self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": flow.code_verifier,
                "client_id": self.profile.client_id,
                "client_secret": self.client_secret,
            }
        )

    def complete_login_from_browser_capture(
        self,
        capture: str,
        flow: AuthorizationFlow,
    ) -> TokenSet:
        """Complete login from a raw HTTP exchange or WebInspector HAR export."""

        return self.complete_login(_callback_location(capture), flow)

    def password_login(self, username: str, password: str) -> TokenSet:
        """Exchange account credentials without retaining them."""

        if self.profile.client_id != ANDROID_PROFILE.client_id:
            raise ValueError("The supported Halo password grant requires the Android profile.")
        username = username.strip()
        if not username:
            raise ValueError("Halo account email is required.")
        if not password:
            raise ValueError("Halo account password is required.")
        return self._token_request(
            {
                "grant_type": "password",
                "client_id": self.profile.client_id,
                "client_secret": self.client_secret,
                "scope": SCOPE,
                "username": username,
                "password": password,
            }
        )

    def refresh(self, refresh_token: str) -> TokenSet:
        if not refresh_token:
            raise LoginRequiredError("No refresh token is available. Run `halo login`.")
        return self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.profile.client_id,
                "client_secret": self.client_secret,
            },
            previous_refresh_token=refresh_token,
        )

    def _token_request(
        self,
        data: dict[str, str],
        *,
        previous_refresh_token: str | None = None,
    ) -> TokenSet:
        headers = {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": self.profile.token_user_agent,
        }
        try:
            response = self.http.post(
                f"{self.auth_base_url}{TOKEN_PATH}",
                data=data,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise AuthenticationError(
                "Could not reach Halo's authentication service. The login was not retried."
            ) from exc

        payload = _json_object(response)
        if response.is_error:
            error = payload.get("error")
            if error == "invalid_grant":
                grant_type = data.get("grant_type")
                if grant_type == "authorization_code":
                    raise LoginRequiredError(
                        "Halo rejected the authorization code. It may be expired, already used, "
                        "or PKCE-mismatched. Start a fresh login."
                    )
                if grant_type == "password":
                    raise LoginRequiredError(
                        "Halo rejected the email or password. Check both values and try again."
                    )
                raise LoginRequiredError("Halo rejected the refresh token. Run `halo login` again.")
            if error == "invalid_client":
                raise AuthenticationError(
                    f"Halo rejected the {self.profile.name} client credentials. The app "
                    "credential may have been rotated; override it with "
                    f"HALO_{self.profile.name.upper()}_CLIENT_SECRET."
                )
            name = str(error or "authentication_error")
            diagnostic = _response_diagnostic(response, payload)
            raise AuthenticationError(
                f"Halo authentication failed with HTTP {response.status_code} ({name}); "
                f"{diagnostic}."
            )

        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token") or previous_refresh_token
        if not isinstance(access_token, str) or not isinstance(refresh_token, str):
            raise AuthenticationError("Halo returned an incomplete token response.")
        try:
            expires_in = max(0, int(payload.get("expires_in", 0)))
        except (TypeError, ValueError) as exc:
            raise AuthenticationError("Halo returned an invalid token expiry.") from exc
        return TokenSet(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=datetime.now(timezone.utc).timestamp() + max(0, expires_in - 60),
            token_type=str(payload.get("token_type", "Bearer")),
            scope=str(payload.get("scope", "")),
        )


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _callback_location(capture: str) -> str:
    """Extract the ``haloapp://callback`` URL from a saved browser capture.

    Accepts the callback URL on its own, a raw HTTP response (or full exchange)
    carrying a ``Location`` header, or a HAR export. All three forms contain the
    same one-time authorization code; ``complete_login`` validates the callback
    state and Halo validates the PKCE verifier during the token exchange.
    """

    if not capture.strip():
        raise InvalidCallbackError("The browser capture is empty.")
    try:
        document = json.loads(capture)
    except json.JSONDecodeError:
        location = _raw_http_location(capture)
    else:
        location = _har_location(document)
    if not location.casefold().startswith("haloapp://callback"):
        raise InvalidCallbackError(
            "The saved response Location is not a haloapp://callback URL."
        )
    return location


def _har_location(document: Any) -> str:
    if not isinstance(document, dict):
        raise InvalidCallbackError("The browser capture JSON is not a HAR object.")
    log = document.get("log")
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        raise InvalidCallbackError("The browser capture has no HAR entries.")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request")
        response = entry.get("response")
        if not isinstance(request, dict) or not isinstance(response, dict):
            continue
        request_url = request.get("url")
        if not isinstance(request_url, str):
            continue
        if urlsplit(request_url).path.rstrip("/") != "/connect/authorize/callback":
            continue
        location = _har_header_map(response.get("headers")).get("location")
        if location is not None:
            return location
    raise InvalidCallbackError(
        "No authorize/callback HAR entry with a Location response header was found."
    )


def _har_header_map(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(headers, list):
        return result
    for header in headers:
        if not isinstance(header, dict):
            continue
        name = header.get("name")
        value = header.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result[name.casefold()] = value
    return result


def _raw_http_location(capture: str) -> str:
    callback = capture.strip()
    if callback.casefold().startswith("haloapp://callback"):
        return callback
    for line in capture.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip().casefold() == "location":
            return value.strip()
    raise InvalidCallbackError("The capture has no haloapp callback or Location header.")


def _response_diagnostic(response: httpx.Response, payload: dict[str, Any]) -> str:
    """Describe an auth failure without including response values or credentials."""

    content_type = response.headers.get("Content-Type", "missing").split(";", 1)[0]
    correlation_id = response.headers.get("X-Correlation-ID", "missing")
    keys = ",".join(sorted(str(key) for key in payload)) or "none"
    result = (
        f"content-type={content_type}, response-bytes={len(response.content)}, "
        f"json-fields={keys}, correlation-id={correlation_id}"
    )
    server_detail = _server_validation(payload)
    return f"{result}, {server_detail}" if server_detail else result


def _server_validation(payload: dict[str, Any]) -> str:
    """Summarize the server's validation explanation with values redacted."""

    parts: list[str] = []
    for key in ("ErrorCode", "Message"):
        value = payload.get(key)
        if isinstance(value, str):
            parts.append(f"{key}={_redact(value)}")

    validation = payload.get("ValidationErrors")
    if isinstance(validation, dict):
        for field, messages in validation.items():
            if isinstance(messages, str):
                messages = [messages]
            elif not isinstance(messages, list):
                messages = []
            detail = "|".join(_redact(item) for item in messages if isinstance(item, str))
            parts.append(f"{field}={detail or 'invalid'}")
    return "server-validation=" + ";".join(parts) if parts else ""


def _redact(value: str) -> str:
    """Remove URLs and credential-shaped tokens from a server-supplied message."""

    value = re.sub(r"\S+://\S+", "<redacted-url>", value)
    value = re.sub(r"\b[A-Za-z0-9_-]{16,}\b", "<redacted-value>", value)
    return " ".join(value.split())[:240]
