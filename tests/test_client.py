from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from halo_collar import (
    ANDROID_CLIENT_SECRET,
    IOS_CLIENT_SECRET,
    CorrectionOutcomeUnknownError,
    CorrectionType,
    HaloAPIError,
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
            # Halo reports the parallel-call version on every response, so the
            # correction's own clock read is what warms it; no extra call.
            return httpx.Response(
                200,
                json={"serverDateTime": "2026-07-26T20:00:00Z"},
                headers={"Halo-ParallelCall-Version": "27"},
            )
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
            return httpx.Response(
                200,
                json="2026-07-26T20:00:00Z",
                headers={"Halo-ParallelCall-Version": "27"},
            )
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
    # This is about the 401 retry, not the warm-up read a cold client makes.
    client._parallel_call_version = "27"
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


def _stub_client(
    tmp_path,
    handler,
    *,
    store: StateStore | None = None,
    parallel_call_version: str = "27",
) -> HaloClient:
    """Build a client on a mock transport.

    The parallel-call version defaults to a warm one so that tests about request
    bodies see only their own request; a real client starts cold and reads the
    clock before its first write. Pass ``"0"`` to exercise that.
    """

    client = HaloClient(
        client_secret="secret",
        tokens=tokens(),
        store=store or StateStore(tmp_path / "state.json"),
        app_instance_id="app-instance",
        amplitude_session_id="1700000000000",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client._parallel_call_version = parallel_call_version
    return client


def test_account_map_sends_the_captured_viewport_parameters(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"pets": []})

    client = _stub_client(tmp_path, handler)
    assert client.account_map(37.4219983, -122.084) == {"pets": []}

    query = parse_qs(requests[0].url.query.decode())
    assert query == {
        "viewport.center.latitude": ["37.4219983"],
        "viewport.center.longitude": ["-122.084"],
        "RefreshTelemetry": ["False"],
        "MaxCorrectionsCount": ["20"],
    }
    assert requests[0].headers["Halo-Amplitude-SessionId"] == "1700000000000"


def test_geofences_read_the_map_without_a_viewport(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "pets": [],
                "corrections": [],
                "geoFencesInfo": {
                    "geoFencesToDisplay": [{"id": "fence-1", "name": "Home"}],
                    "geoFencesTotalCount": 1,
                },
            },
        )

    client = _stub_client(tmp_path, handler)
    assert client.geofences() == [{"id": "fence-1", "name": "Home"}]

    assert requests[0].url.path == "/account/my/map"
    query = parse_qs(requests[0].url.query.decode())
    assert query == {"RefreshTelemetry": ["False"], "MaxCorrectionsCount": ["20"]}


def test_registering_a_device_stores_the_mobile_id_corrections_carry(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"mobileId": 3})

    store = StateStore(tmp_path / "state.json")
    client = _stub_client(tmp_path, handler, store=store)

    assert client.mobile_id == 2
    assert client.register_mobile_device() == 3

    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/account/mobile-data"
    assert body["InternalMobileId"] == "app-instance"
    assert body["Platform"] == "iOS"
    assert sorted(body) == [
        "Idiom",
        "InternalMobileId",
        "Manufacturer",
        "Model",
        "Platform",
        "VersionString",
    ]
    assert client.mobile_id == 3
    assert store.settings()["mobile_id"] == "3"


def test_a_stored_mobile_id_is_reused_and_a_bad_one_falls_back(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.update_settings(mobile_id="7")
    client = _stub_client(tmp_path, lambda request: httpx.Response(200, json={}), store=store)
    assert client.mobile_id == 7

    store.update_settings(mobile_id="not-a-number")
    client = _stub_client(tmp_path, lambda request: httpx.Response(200, json={}), store=store)
    assert client.mobile_id == 2


def test_registration_rejects_a_mobile_id_that_is_not_an_integer(tmp_path) -> None:
    client = _stub_client(tmp_path, lambda request: httpx.Response(200, json={"mobileId": "3"}))

    with pytest.raises(HaloAPIError):
        client.register_mobile_device()


def test_videos_are_gathered_from_wherever_the_configuration_holds_them(tmp_path) -> None:
    configuration = {
        "lms": {
            "trainingWelcomeVideo": {
                "videoStreamUrl": "https://cdn.example/welcome.m3u8",
                "thumbnailUrl": "https://cdn.example/welcome.jpg",
            }
        },
        "onboarding": {
            "screens": [
                {
                    "getStartedVideo": {
                        "videoStreamUrl": "https://cdn.example/started.m3u8",
                        "thumbnailUrl": None,
                    }
                }
            ]
        },
        "collar": {"images": {"squareSmallImageUrl": "https://cdn.example/collar.png"}},
    }
    client = _stub_client(tmp_path, lambda _: httpx.Response(200, json=configuration))

    videos = client.videos()

    # Nested under a list, and the plain image is not mistaken for a video.
    assert sorted(video["name"] for video in videos) == [
        "getStartedVideo",
        "trainingWelcomeVideo",
    ]
    started = next(video for video in videos if video["name"] == "getStartedVideo")
    assert started["section"] == "onboarding.screens.0"
    assert started["thumbnailUrl"] is None


def test_a_mutation_learns_the_parallel_call_version_before_writing(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/system/server-date-time":
            return httpx.Response(
                200,
                json="2026-07-29T12:00:00Z",
                headers={"Halo-ParallelCall-Version": "27"},
            )
        return httpx.Response(200, json={"id": "pet-1"})

    client = _stub_client(tmp_path, handler, parallel_call_version="0")
    client.delete_pet("pet-1")

    # Halo rejects a write carrying the placeholder version, so the clock is
    # read first and the write carries what it reported.
    assert [request.url.path for request in requests] == [
        "/system/server-date-time",
        "/pet/pet-1",
    ]
    assert requests[0].headers["Halo-ParallelCall-Version"] == "0"
    assert requests[1].headers["Halo-ParallelCall-Version"] == "27"
    assert requests[1].method == "DELETE"


def test_a_known_parallel_call_version_is_not_re_fetched(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[], headers={"Halo-ParallelCall-Version": "31"})

    client = _stub_client(tmp_path, handler, parallel_call_version="0")
    client.pets()
    client.delete_pet("pet-1")

    assert [request.url.path for request in requests] == ["/pet/my", "/pet/pet-1"]
    assert requests[1].headers["Halo-ParallelCall-Version"] == "31"


def test_account_map_rejects_half_a_viewport(tmp_path) -> None:
    client = _stub_client(tmp_path, lambda request: httpx.Response(200, json={}))

    with pytest.raises(ValueError):
        client.account_map(37.4219983)


def test_geofences_reject_an_unexpected_shape(tmp_path) -> None:
    client = _stub_client(
        tmp_path,
        lambda request: httpx.Response(200, json={"geoFencesInfo": {"geoFencesToDisplay": {}}}),
    )

    with pytest.raises(HaloAPIError):
        client.geofences()


def test_paged_endpoints_use_halos_inconsistent_parameter_casing(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    client = _stub_client(tmp_path, handler)
    client.walks(page=2, page_size=10)
    client.notifications(page=3, page_size=5)

    assert parse_qs(requests[0].url.query.decode()) == {"page": ["2"], "pageSize": ["10"]}
    assert parse_qs(requests[1].url.query.decode()) == {"Page": ["3"], "PageSize": ["5"]}


@pytest.mark.parametrize("bad", [0, -1, True])
def test_paging_rejects_values_halo_would_not_accept(tmp_path, bad) -> None:
    client = _stub_client(tmp_path, lambda _: httpx.Response(200, json={}))
    with pytest.raises(ValueError):
        client.walks(page=bad)


def test_find_collar_puts_an_empty_body_and_tolerates_204(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    client = _stub_client(tmp_path, handler)
    assert client.find_collar("11111111-1111-4111-8111-111111111111") is None
    assert requests[0].method == "PUT"
    assert requests[0].url.path == "/collar/11111111-1111-4111-8111-111111111111/find"
    assert requests[0].content == b""


def test_find_collar_rejects_path_injection(tmp_path) -> None:
    client = _stub_client(tmp_path, lambda _: httpx.Response(204))
    with pytest.raises(ValueError):
        client.find_collar("../pet/x/run-instant-correction")


def test_push_subscription_matches_the_captured_bodies(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    client = _stub_client(tmp_path, handler)
    client.subscribe_push_notifications("token-1")
    client.unsubscribe_push_device("token-1")

    import json as _json

    assert _json.loads(requests[0].content) == {
        "PlatformType": "Android",
        "DeviceHandle": "token-1",
    }
    assert _json.loads(requests[1].content) == {"DeviceHandle": "token-1"}
    with pytest.raises(ValueError):
        client.subscribe_push_notifications("  ")


def test_pets_requires_a_list_of_objects(tmp_path) -> None:
    client = _stub_client(tmp_path, lambda _: httpx.Response(200, json={"pets": []}))
    with pytest.raises(HaloAPIError):
        client.pets()


def test_pets_lists_pets_that_have_no_collar(tmp_path) -> None:
    # /collar/my/ only reaches pets with a collar bound, so this endpoint is the
    # only way to see a pet whose collarInfo is still null.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/pet/my"
        return httpx.Response(
            200,
            json=[
                {"id": "pet-1", "name": "Alpha", "collarInfo": None},
                {"id": "pet-2", "name": "Bravo", "collarInfo": {"id": "collar-1"}},
            ],
        )

    client = _stub_client(tmp_path, handler)
    assert [pet["name"] for pet in client.pets()] == ["Alpha", "Bravo"]


def test_geo_fence_add_matches_the_captured_body(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"geoFence": {"zones": []}})

    client = _stub_client(tmp_path, handler)
    client.add_geo_fence(
        "My Fence 1",
        [(40.0001, -75.0001), (40.0002, -75.00015), (40.0003, -75.00005)],
    )

    import json as _json

    assert _json.loads(requests[0].content) == {
        "Name": "My Fence 1",
        "LocationPoints": [
            {"Latitude": 40.0001, "Longitude": -75.0001},
            {"Latitude": 40.0002, "Longitude": -75.00015},
            {"Latitude": 40.0003, "Longitude": -75.00005},
        ],
        "PublicVisibilityType": "Private",
        "Analytics": None,
    }


def test_fence_boundary_needs_an_enclosing_shape(tmp_path) -> None:
    client = _stub_client(tmp_path, lambda _: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="three location points"):
        client.add_geo_fence("Two points", [(40.0, -75.0), (40.1, -75.1)])


@pytest.mark.parametrize(
    "points",
    [
        [(91.0, -75.0), (40.0, -75.0), (40.1, -75.1)],
        [(40.0, -181.0), (40.0, -75.0), (40.1, -75.1)],
    ],
)
def test_fence_rejects_out_of_range_coordinates(tmp_path, points) -> None:
    client = _stub_client(tmp_path, lambda _: httpx.Response(200, json={}))
    with pytest.raises(ValueError):
        client.add_geo_fence("Bad", points)


def test_geo_fence_name_check_sends_null_id_when_creating(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    client = _stub_client(tmp_path, handler)
    assert client.geo_fence_name_is_available("My Fence 1") is True
    assert (
        client.geo_fence_name_is_available(
            "Back yard", geo_fence_id="22222222-2222-4222-8222-222222222222"
        )
        is True
    )

    import json as _json

    assert _json.loads(requests[0].content) == {"Id": None, "Name": "My Fence 1"}
    assert _json.loads(requests[1].content) == {
        "Id": "22222222-2222-4222-8222-222222222222",
        "Name": "Back yard",
    }


def test_delete_geo_fence_uses_delete_and_validates_the_id(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "success"})

    client = _stub_client(tmp_path, handler)
    assert client.delete_geo_fence("22222222-2222-4222-8222-222222222222") == {"status": "success"}
    assert requests[0].method == "DELETE"
    with pytest.raises(ValueError):
        client.delete_geo_fence("../pet/x")


def test_update_pet_sends_every_field_as_a_full_replacement(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "pet-1"})

    client = _stub_client(tmp_path, handler)
    client.update_pet(
        "33333333-3333-4333-8333-333333333333",
        name="Rex",
        color_hex="#FF6D29",
        breed="IrishSetter",
        birthday=datetime(2024, 11, 24, tzinfo=timezone.utc),
        weight_kg=24.947610018960187,
    )

    import json as _json

    assert _json.loads(requests[0].content) == {
        "Name": "Rex",
        "ColorHex": "#FF6D29",
        "Breed": "IrishSetter",
        "Birthday": "2024-11-24T00:00:00Z",
        "WeightKg": 24.947610018960187,
    }


def test_notification_status_requires_ids_and_marks_read(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    client = _stub_client(tmp_path, handler)
    client.set_notification_status(["44444444-4444-4444-8444-444444444444"])

    import json as _json

    assert _json.loads(requests[0].content) == {
        "Ids": ["44444444-4444-4444-8444-444444444444"],
        "Status": "Read",
    }
    with pytest.raises(ValueError):
        client.set_notification_status([])


def test_parcel_lookup_builds_the_captured_point_literal(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": 200, "body": "{}"})

    client = _stub_client(tmp_path, handler)
    client.lookup_parcels(40.00015, -75.00012)

    import json as _json

    assert _json.loads(requests[0].content) == {
        "spatial_intersect": "POINT(-75.00012 40.00015)",
        "rpp": 1,
        "page": 1,
        "si_srid": 4326,
        "v": 8,
    }


def test_course_launch_link_returns_the_external_url(tmp_path) -> None:
    launch = "https://cloud.scorm.com/api/cloud/registration/launch/00000000-0000-4000-8000-000000000000"
    client = _stub_client(tmp_path, lambda _: httpx.Response(200, json=launch))
    assert client.training_course_link("2024-curriculum-update-v1b509", "CollarFitting") == launch


def test_add_pet_matches_the_captured_body(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "55555555", "collarInfo": None})

    client = _stub_client(tmp_path, handler)
    created = client.add_pet(
        name="Scout",
        color_hex="#E8C00F",
        breed="GoldenRetriever",
        birthday=datetime(2026, 4, 1, tzinfo=timezone.utc),
        weight_kg=9.071858188712795,
    )

    import json as _json

    assert requests[0].url.path == "/pet/add"
    assert _json.loads(requests[0].content) == {
        "Name": "Scout",
        "ColorHex": "#E8C00F",
        "Breed": "GoldenRetriever",
        "Birthday": "2026-04-01T00:00:00Z",
        "WeightKg": 9.071858188712795,
    }
    assert created["collarInfo"] is None


def test_pet_name_check_sends_null_id_when_creating(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    client = _stub_client(tmp_path, handler)
    assert client.pet_name_is_available("Scout") is True

    import json as _json

    assert requests[0].url.path == "/pet/check-name-uniqueness"
    assert _json.loads(requests[0].content) == {"Id": None, "Name": "Scout"}


def test_taken_name_is_reported_as_unavailable_not_an_error(tmp_path) -> None:
    client = _stub_client(tmp_path, lambda _: httpx.Response(409))
    assert client.pet_name_is_available("Scout") is False
    assert client.geo_fence_name_is_available("Back yard") is False


def test_name_check_still_raises_on_unexpected_failures(tmp_path) -> None:
    client = _stub_client(tmp_path, lambda _: httpx.Response(500))
    with pytest.raises(HaloAPIError):
        client.pet_name_is_available("Scout")
