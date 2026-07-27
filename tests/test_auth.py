from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from halo_collar import AuthenticationError, HaloOAuth, InvalidCallbackError, LoginRequiredError
from halo_collar.auth import ANDROID_PROFILE


def test_pkce_login_and_callback_validation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 7200,
                "token_type": "Bearer",
                "scope": "openid offline_access",
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    oauth = HaloOAuth("secret", http=http)
    flow = oauth.begin_login()
    query = parse_qs(urlsplit(flow.url).query)
    assert query["response_type"] == ["code"]
    assert query["response_mode"] == ["query"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["haloapp://callback"]
    assert query["state"] == [flow.state]
    assert "code_challenge" in query
    assert len(flow.code_verifier) == 43

    tokens = oauth.complete_login(
        f"haloapp://callback?code=one-time&state={flow.state}",
        flow,
    )
    assert tokens.access_token == "access"
    assert requests[0].url.path == "/connect/token"
    body = parse_qs(requests[0].content.decode())
    assert body["grant_type"] == ["authorization_code"]
    assert body["code_verifier"] == [flow.code_verifier]
    assert body["client_secret"] == ["secret"]
    assert "Cookie" not in requests[0].headers
    assert requests[0].headers["Accept"] == "*/*"
    assert requests[0].headers["User-Agent"].startswith("Halo/1030 ")


def test_callback_state_must_match() -> None:
    oauth = HaloOAuth(
        "secret",
        http=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )
    flow = oauth.begin_login()
    with pytest.raises(InvalidCallbackError):
        oauth.complete_login("haloapp://callback?code=x&state=wrong", flow)


def test_raw_http_capture_handoff() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 7200,
            },
        )

    oauth = HaloOAuth(
        "secret",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    flow = oauth.begin_login()
    authorize_query = urlsplit(flow.url).query
    capture = (
        f"GET /connect/authorize/callback?{authorize_query} HTTP/1.1\n"
        "Host: auth.halocollar.com\n"
        "\n"
        "HTTP/1.1 302 Found\n"
        f"Location: haloapp://callback?code=one-time&state={flow.state}\n"
        "Set-Cookie: identity=changed\n"
    )
    tokens = oauth.complete_login_from_browser_capture(capture, flow)
    assert tokens.access_token == "access"
    assert "Cookie" not in requests[0].headers


def test_response_location_header_is_sufficient_for_handoff() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 7200,
            },
        )

    oauth = HaloOAuth(
        "secret",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    flow = oauth.begin_login()
    capture = f"Location: haloapp://callback?code=one-time&state={flow.state}\n"

    tokens = oauth.complete_login_from_browser_capture(capture, flow)

    assert tokens.access_token == "access"
    assert "Cookie" not in requests[0].headers


def test_har_capture_handoff() -> None:
    oauth = HaloOAuth(
        "secret",
        http=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "access_token": "access",
                        "refresh_token": "refresh",
                        "expires_in": 7200,
                    },
                )
            )
        ),
    )
    flow = oauth.begin_login()
    capture = json.dumps(
        {
            "log": {
                "entries": [
                    {
                        "request": {
                            "url": flow.url.replace(
                                "/connect/authorize?",
                                "/connect/authorize/callback?",
                            ),
                            "headers": [],
                        },
                        "response": {
                            "headers": [
                                {
                                    "name": "Location",
                                    "value": (
                                        f"haloapp://callback?code=one-time&state={flow.state}"
                                    ),
                                }
                            ]
                        },
                    }
                ]
            }
        }
    )
    tokens = oauth.complete_login_from_browser_capture(capture, flow)
    assert tokens.refresh_token == "refresh"


def test_invalid_refresh_token_requires_login() -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(400, json={"error": "invalid_grant"})
        )
    )
    oauth = HaloOAuth("secret", http=http)
    with pytest.raises(LoginRequiredError, match="login"):
        oauth.refresh("expired")


def test_android_password_grant_and_expiry_skew() -> None:
    requests: list[httpx.Request] = []
    before = datetime.now(timezone.utc).timestamp()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "android-access",
                "refresh_token": "android-refresh",
                "token_type": "Bearer",
                "expires_in": 7200,
                "scope": "openid email offline_access api.dogpark",
            },
        )

    oauth = HaloOAuth(
        "android-secret",
        profile=ANDROID_PROFILE,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    tokens = oauth.password_login(" person@example.com ", "account-password")

    body = parse_qs(requests[0].content.decode())
    assert body == {
        "grant_type": ["password"],
        "client_id": ["halo.app.android"],
        "client_secret": ["android-secret"],
        "scope": ["openid email offline_access api.dogpark"],
        "username": ["person@example.com"],
        "password": ["account-password"],
    }
    assert requests[0].url.path == "/connect/token"
    assert tokens.access_token == "android-access"
    assert tokens.refresh_token == "android-refresh"
    assert before + 7139 <= tokens.expires_at <= datetime.now(timezone.utc).timestamp() + 7141


def test_android_refresh_uses_same_profile_and_rotates_both_tokens() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 7200,
            },
        )

    oauth = HaloOAuth(
        "android-secret",
        profile=ANDROID_PROFILE,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    tokens = oauth.refresh("old-refresh")
    body = parse_qs(requests[0].content.decode())
    assert body["grant_type"] == ["refresh_token"]
    assert body["client_id"] == ["halo.app.android"]
    assert body["client_secret"] == ["android-secret"]
    assert body["refresh_token"] == ["old-refresh"]
    assert tokens.access_token == "rotated-access"
    assert tokens.refresh_token == "rotated-refresh"


def test_model_validation_error_is_safe_and_actionable() -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                400,
                json={
                    "ErrorCode": "Validation",
                    "Message": "Request validation failed",
                    "ValidationErrors": {
                        "CodeVerifier": ["Must contain AABBCCDDEEFF00112233445566778899"]
                    },
                },
                headers={"X-Correlation-ID": "correlation"},
            )
        )
    )
    oauth = HaloOAuth("secret", http=http)
    with pytest.raises(AuthenticationError) as raised:
        oauth.refresh("refresh")
    message = str(raised.value)
    assert "CodeVerifier=Must contain <redacted-value>" in message
    assert "AABBCCDDEEFF" not in message
    assert "correlation-id=correlation" in message
