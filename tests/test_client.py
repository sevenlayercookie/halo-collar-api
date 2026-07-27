from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from halo_collar import (
    ANDROID_CLIENT_SECRET,
    IOS_CLIENT_SECRET,
    CorrectionOutcomeUnknownError,
    CorrectionType,
    HaloClient,
    LoginRequiredError,
    StaleCommandNumberError,
    StateStore,
    TokenSet,
)


def tokens() -> TokenSet:
    return TokenSet(
        access_token="access",
        refresh_token="refresh",
        expires_at=datetime.now(timezone.utc).timestamp() + 3600,
    )


def test_correction_uses_server_time_and_never_retries(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/collar/my/":
            return httpx.Response(
                200,
                json=[
                    {
                        "petInfo": {"id": "pet-1", "name": "Dog"},
                        "telemetry": {
                            "wiFi": {"status": "socketconnected"},
                            "cellular": {"status": "disconnected"},
                        },
                    }
                ],
                headers={"Halo-ParallelCall-Version": "15"},
            )
        if request.url.path == "/system/server-date-time":
            return httpx.Response(200, json="2026-07-26T20:00:00Z")
        if request.url.path == "/pet/pet-1/run-instant-correction/":
            return httpx.Response(
                200,
                json={"result": "success", "currentCommandNumber": None},
            )
        raise AssertionError(request.url)

    store = StateStore(tmp_path / "state.json")
    client = HaloClient(
        client_secret="secret",
        tokens=tokens(),
        store=store,
        app_instance_id="app-instance",
        timezone_name="UTC",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.send_instant_correction(
        "pet-1",
        CorrectionType.GOOD_BEHAVIOR,
        command_number=13,
    )
    assert result["result"] == "success"
    correction = requests[-1]
    assert correction.headers["Halo-ParallelCall-Version"] == "15"
    body = __import__("json").loads(correction.content)
    assert body == {
        "MobileId": 2,
        "CommandNumber": 13,
        "ExpirationDate": "2026-07-26T20:00:07.000Z",
        "CorrectionType": "GoodBehavior",
    }


def test_old_command_reconciles_without_retry(tmp_path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/system/server-date-time":
            return httpx.Response(200, json={"serverDateTime": "2026-07-26T20:00:00Z"})
        return httpx.Response(
            409,
            json={"result": "oldcommandnumber", "currentCommandNumber": 20},
        )

    store = StateStore(tmp_path / "state.json")
    client = HaloClient(
        client_secret="secret",
        tokens=tokens(),
        store=store,
        app_instance_id="app-instance",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(StaleCommandNumberError) as raised:
        client.send_instant_correction(
            "pet-1",
            "Warning",
            command_number=13,
            require_online=False,
        )
    assert raised.value.current_command_number == 20
    assert calls == 2
    assert store.reserve_command_number("pet-1") == 21


def test_transport_error_has_unknown_outcome_and_reserved_counter(tmp_path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/system/server-date-time":
            return httpx.Response(200, json="2026-07-26T20:00:00Z")
        raise httpx.ReadTimeout("timed out", request=request)

    store = StateStore(tmp_path / "state.json")
    client = HaloClient(
        client_secret="secret",
        tokens=tokens(),
        store=store,
        app_instance_id="app-instance",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(CorrectionOutcomeUnknownError):
        client.send_instant_correction(
            "pet-1",
            "Escalation",
            command_number=5,
            require_online=False,
        )
    assert calls == 2
    assert store.reserve_command_number("pet-1") == 6


def test_401_reauthenticates_and_retries_once(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "auth.example":
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 7200,
                },
            )
        if len([item for item in requests if item.url.path == "/collar/my/"]) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json=[])

    store = StateStore(tmp_path / "state.json")
    client = HaloClient(
        client_secret="secret",
        tokens=tokens(),
        store=store,
        app_instance_id="app-instance",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        auth_base_url="https://auth.example",
    )
    assert client.collars() == []
    assert [request.url.path for request in requests] == [
        "/collar/my/",
        "/connect/token",
        "/collar/my/",
    ]


def test_android_profile_drives_refresh_and_api_header(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "auth.example":
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 7200,
                },
            )
        return httpx.Response(200, json=[])

    store = StateStore(tmp_path / "state.json")
    store.save_session(
        TokenSet("expired", "refresh", 0),
        client_id="halo.app.android",
        app_version="2.12.0.590",
    )
    client = HaloClient(
        store=store,
        app_instance_id="stable-guid",
        timezone_name="America/Chicago",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        auth_base_url="https://auth.example",
    )
    assert client.collars() == []

    refresh_body = parse_qs(requests[0].content.decode())
    assert refresh_body["client_id"] == ["halo.app.android"]
    assert refresh_body["client_secret"] == [ANDROID_CLIENT_SECRET]
    assert refresh_body["refresh_token"] == ["refresh"]
    header = parse_qs(requests[1].headers["Halo-Client"])
    assert header == {
        "clientId": ["halo.app.android"],
        "version": ["2.12.0.590"],
        "appInstanceId": ["stable-guid"],
        "timezone": ["America/Chicago"],
    }
    assert store.load_tokens().refresh_token == "new-refresh"


@pytest.mark.parametrize(
    ("client_id", "expected"),
    [
        ("halo.app.android", ANDROID_CLIENT_SECRET),
        ("halo.app.ios", IOS_CLIENT_SECRET),
    ],
)
def test_embedded_secret_is_used_when_nothing_is_stored(
    tmp_path,
    monkeypatch,
    client_id,
    expected,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"access_token": "new", "refresh_token": "new", "expires_in": 7200},
        )

    for name in ("HALO_CLIENT_SECRET", "HALO_ANDROID_CLIENT_SECRET", "HALO_IOS_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    store = StateStore(tmp_path / "state.json")
    store.save_tokens(TokenSet("expired", "refresh", 0))
    client = HaloClient(
        store=store,
        client_id=client_id,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        auth_base_url="https://auth.example",
    )

    client.refresh_login()

    body = parse_qs(requests[0].content.decode())
    assert body["client_id"] == [client_id]
    assert body["client_secret"] == [expected]


def test_environment_secret_overrides_the_embedded_credential(tmp_path, monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"access_token": "new", "refresh_token": "new", "expires_in": 7200},
        )

    monkeypatch.delenv("HALO_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("HALO_ANDROID_CLIENT_SECRET", "rotated-secret")
    store = StateStore(tmp_path / "state.json")
    store.save_session(
        TokenSet("expired", "refresh", 0),
        client_id="halo.app.android",
        app_version="2.12.0.590",
    )
    client = HaloClient(
        store=store,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        auth_base_url="https://auth.example",
    )

    client.refresh_login()

    assert parse_qs(requests[0].content.decode())["client_secret"] == ["rotated-secret"]


def test_stored_session_from_another_profile_is_not_reused(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save_session(
        TokenSet("ios-access", "ios-refresh", 0),
        client_id="halo.app.ios",
        app_version="2.12.0.1030",
    )
    client = HaloClient(
        store=store,
        client_id="halo.app.android",
        http=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )

    assert client.tokens is None
    with pytest.raises(LoginRequiredError, match="halo.app.ios"):
        client.refresh_login()


def test_post_401_refreshes_and_retries_only_once(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "auth.example":
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 7200,
                },
            )
        if len([item for item in requests if item.url.path == "/example"]) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    client = HaloClient(
        client_secret="secret",
        tokens=tokens(),
        store=StateStore(tmp_path / "state.json"),
        app_instance_id="app-instance",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        auth_base_url="https://auth.example",
    )
    response = client._request("POST", "/example", json_body={"value": 1})
    assert response.json() == {"ok": True}
    assert [request.url.path for request in requests] == [
        "/example",
        "/connect/token",
        "/example",
    ]


def test_invalid_refresh_clears_only_tokens(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.example":
            return httpx.Response(400, json={"error": "invalid_grant"})
        raise AssertionError("API request should not be sent after a failed pre-refresh")

    store = StateStore(tmp_path / "state.json")
    store.save_session(
        TokenSet("expired", "dead-refresh", 0),
        client_id="halo.app.android",
        app_version="2.12.0.590",
    )
    client = HaloClient(
        store=store,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        auth_base_url="https://auth.example",
    )
    with pytest.raises(LoginRequiredError):
        client.collars()
    with pytest.raises(LoginRequiredError):
        store.load_tokens()
    assert store.auth_profile()["client_id"] == "halo.app.android"
