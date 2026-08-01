from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from halo_collar import (
    ANDROID_CLIENT_SECRET,
    IOS_CLIENT_SECRET,
    BeaconActionType,
    BeaconCorrectionEscalationType,
    BeaconModelType,
    CorrectionOutcomeUnknownError,
    CorrectionRuleKindType,
    CorrectionRuleUpdate,
    CorrectionType,
    FirmwareUpdateStatus,
    HaloAPIError,
    HaloClient,
    LoginRequiredError,
    StaleCommandNumberError,
    StateStore,
    TokenSet,
    WalkStopOption,
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
        if request.url.path == "/collar/my":
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


def test_update_correction_rules_sends_an_item_level_pascal_case_batch(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "correctionRules": [{"id": "rule-sound", "kindType": "sound"}],
                "areCorrectionRulesDefault": False,
                "lastCorrectionRulesUpdated": "2026-07-30T18:42:00Z",
            },
        )

    client = _stub_client(tmp_path, handler)
    updated = client.update_correction_rules(
        [
            CorrectionRuleUpdate(
                "rule-sound",
                CorrectionRuleKindType.SOUND,
                level=3,
                sound_id="sound-1",
            ),
            CorrectionRuleUpdate(
                "rule-vibration",
                "Vibration",
                vibration_id="vibration-1",
            ),
            CorrectionRuleUpdate("rule-shock", "Shock", level=4),
        ]
    )

    assert updated["lastCorrectionRulesUpdated"] == "2026-07-30T18:42:00Z"
    assert requests[0].method == "PUT"
    assert requests[0].url.path == "/correction-rule"
    assert json.loads(requests[0].content) == {
        "Items": [
            {
                "CorrectionRuleId": "rule-sound",
                "KindType": "Sound",
                "Level": 3,
                "SoundId": "sound-1",
                "VibrationId": None,
            },
            {
                "CorrectionRuleId": "rule-vibration",
                "KindType": "Vibration",
                "Level": None,
                "SoundId": None,
                "VibrationId": "vibration-1",
            },
            {
                "CorrectionRuleId": "rule-shock",
                "KindType": "Shock",
                "Level": 4,
                "SoundId": None,
                "VibrationId": None,
            },
        ]
    }


@pytest.mark.parametrize(
    "items",
    [
        [],
        [CorrectionRuleUpdate("rule-1", "Sound", level=3)],
        [CorrectionRuleUpdate("rule-1", "Vibration", level=1, vibration_id="vibration-1")],
        [CorrectionRuleUpdate("rule-1", "Shock", sound_id="sound-1")],
        [
            CorrectionRuleUpdate("rule-1", "Shock", level=1),
            CorrectionRuleUpdate("rule-1", "Shock", level=2),
        ],
    ],
)
def test_update_correction_rules_rejects_unsafe_or_ambiguous_items(tmp_path, items) -> None:
    client = _stub_client(tmp_path, lambda _: pytest.fail("request should not be sent"))

    with pytest.raises(ValueError):
        client.update_correction_rules(items)


def test_collar_test_uses_server_time_counter_and_modality_fields(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/collar/my":
            return httpx.Response(
                200,
                json=[
                    {
                        "petInfo": {"id": "pet-1"},
                        "telemetry": {"wiFi": {"status": "socketconnected"}},
                    }
                ],
                headers={"Halo-ParallelCall-Version": "15"},
            )
        if request.url.path == "/system/server-date-time":
            return httpx.Response(200, json="2026-07-30T18:42:00Z")
        if request.url.path == "/correction-rule/test-on-collar":
            return httpx.Response(
                200,
                json={"result": "success", "currentCommandNumber": 456},
            )
        raise AssertionError(request.url)

    client = _stub_client(tmp_path, handler)
    result = client.test_correction_on_collar(
        "pet-1",
        CorrectionRuleKindType.SOUND,
        sound_id="sound-1",
        sound_intensity_level=3,
        command_number=456,
    )

    assert result == {"result": "success", "currentCommandNumber": 456}
    request = requests[-1]
    assert request.method == "PUT"
    assert request.url.path == "/correction-rule/test-on-collar"
    assert request.headers["Halo-ParallelCall-Version"] == "15"
    assert json.loads(request.content) == {
        "MobileId": 2,
        "CommandNumber": 456,
        "PetId": "pet-1",
        "KindType": "Sound",
        "SoundId": "sound-1",
        "VibrationId": None,
        "SoundIntensityLevel": 3,
        "ShockIntensityLevel": None,
        "ExpirationDate": "2026-07-30T18:42:30.000Z",
    }


def test_collar_test_transport_failure_is_not_retried(tmp_path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/system/server-date-time":
            return httpx.Response(
                200,
                json="2026-07-30T18:42:00Z",
                headers={"Halo-ParallelCall-Version": "27"},
            )
        raise httpx.ReadTimeout("timed out", request=request)

    client = _stub_client(tmp_path, handler)
    with pytest.raises(CorrectionOutcomeUnknownError):
        client.test_correction_on_collar(
            "pet-1",
            "Shock",
            shock_intensity_level=1,
            command_number=10,
            require_online=False,
        )
    assert calls == 2
    assert client.store.reserve_command_number("pet-1") == 11


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
        if len([item for item in requests if item.url.path == "/collar/my"]) == 1:
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
        "/collar/my",
        "/connect/token",
        "/collar/my",
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


def test_account_map_sends_the_expected_viewport_parameters(tmp_path) -> None:
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


def test_account_map_preserves_reported_active_fence_and_assignment_set(tmp_path) -> None:
    payload = {
        "pets": [
            {
                "id": "pet-1",
                "currentGeoFenceId": "fence-1",
                "telemetry": {"geoFence": {"id": "fence-1", "name": "Home"}},
                "fencesState": "allapplied",
                "isFencesSynchronized": True,
            },
            {
                "id": "pet-2",
                "currentGeoFenceId": None,
                "telemetry": None,
            },
        ],
        "geoFencesInfo": {
            "geoFencesToDisplay": [
                {
                    "id": "fence-1",
                    "petsSync": [
                        {"petId": "pet-1", "isAssigned": True, "status": "completed"},
                        {"petId": "pet-2", "isAssigned": True, "status": "skipped"},
                    ],
                },
                {
                    "id": "fence-2",
                    "petsSync": [
                        {"petId": "pet-1", "isAssigned": True, "status": "pending"},
                    ],
                },
            ],
            "geoFencesTotalCount": 2,
        },
        "corrections": [],
    }
    client = _stub_client(tmp_path, lambda _: httpx.Response(200, json=payload))

    result = client.account_map()

    assert result["pets"][0]["currentGeoFenceId"] == "fence-1"
    assert result["pets"][1]["currentGeoFenceId"] is None
    assert [
        fence["petsSync"][0]["status"]
        for fence in result["geoFencesInfo"]["geoFencesToDisplay"]
    ] == ["completed", "pending"]


def test_geo_fence_pet_sync_returns_automatic_distribution_state(tmp_path) -> None:
    sync = [
        {"petId": "pet-1", "isAssigned": True, "status": "completed"},
        {"petId": "pet-2", "isAssigned": True, "status": "skipped"},
    ]
    payload = {
        "geoFencesInfo": {
            "geoFencesToDisplay": [
                {"id": "fence-1", "petsSync": []},
                {"id": "fence-2", "petsSync": sync},
            ]
        }
    }
    client = _stub_client(tmp_path, lambda _: httpx.Response(200, json=payload))

    assert client.geo_fence_pet_sync("fence-2") == sync


@pytest.mark.parametrize(
    "payload",
    [
        {
            "geoFencesInfo": {
                "geoFencesToDisplay": [{"id": "fence-1", "petsSync": None}]
            }
        },
        {"geoFencesInfo": {"geoFencesToDisplay": [{"id": "another-fence", "petsSync": []}]}},
    ],
)
def test_geo_fence_pet_sync_rejects_missing_or_malformed_state(tmp_path, payload) -> None:
    client = _stub_client(tmp_path, lambda _: httpx.Response(200, json=payload))

    with pytest.raises(HaloAPIError):
        client.geo_fence_pet_sync("fence-1")


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


def test_walk_summary_uses_the_single_walk_route(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/walk/walk-1/summary"
        return httpx.Response(
            200,
            json={
                "id": "walk-1",
                "startTrigger": "mobile",
                "endedAt": "2026-07-30T18:41:12Z",
                "trailThumbnailImageUrl": None,
            },
        )

    client = _stub_client(tmp_path, handler)

    assert client.walk_summary("walk-1")["startTrigger"] == "mobile"


def test_walk_pause_and_stop_send_per_collar_commands(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": "success"})

    client = _stub_client(tmp_path, handler)

    assert client.set_walk_paused("walk-1", "collar-1", True) == {"result": "success"}
    assert client.stop_walk(
        "walk-1",
        "collar-1",
        stop_option=WalkStopOption.FORCE_SET_FENCES_ON,
    ) == {"result": "success"}

    assert [request.url.path for request in requests] == [
        "/walk/walk-1/set-is-paused",
        "/walk/walk-1/stop",
    ]
    assert json.loads(requests[0].content) == {
        "CollarId": "collar-1",
        "SetWalkIsPaused": True,
    }
    assert json.loads(requests[1].content) == {
        "CollarId": "collar-1",
        "StopOption": "ForceSetFencesOn",
    }


def test_walk_command_results_are_returned_for_caller_interpretation(tmp_path) -> None:
    client = _stub_client(
        tmp_path,
        lambda _: httpx.Response(200, json={"result": "walkIdMismatch"}),
    )

    assert client.set_walk_paused("walk-1", "collar-1", False) == {
        "result": "walkIdMismatch"
    }


def test_mark_walk_ended_sends_summary_without_raw_points(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    pets = [
        {
            "Id": "pet-1",
            "CollarId": "collar-1",
            "WalkedDurationInSeconds": 1815,
            "WalkedDistanceInMeters": 2431.7,
            "FeedbacksCount": 2,
            "Timestamp": "2026-07-30T18:41:10Z",
        }
    ]
    user = {
        "TotalDuration": "00:31:12",
        "WalkedDuration": "00:30:15",
        "WalkedDistanceInMeters": 2388.4,
    }
    client = _stub_client(tmp_path, handler)

    assert (
        client.mark_walk_ended(
            "walk-1",
            started_at=datetime(2026, 7, 30, 18, 10, tzinfo=timezone.utc),
            ended_at="2026-07-30T18:41:12Z",
            pets=pets,
            user=user,
            location_name="Rochester, Minnesota",
        )
        is None
    )

    assert requests[0].url.path == "/walk/walk-1/mark-ended"
    assert json.loads(requests[0].content) == {
        "StartedAt": "2026-07-30T18:10:00.000Z",
        "EndedAt": "2026-07-30T18:41:12Z",
        "Pets": pets,
        "User": user,
        "LocationName": "Rochester, Minnesota",
    }


def test_walk_image_uploads_use_the_observed_multipart_fields(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    client = _stub_client(tmp_path, handler)
    client.upload_walk_trail_thumbnail(
        "walk-1",
        b"thumbnail-bytes",
        filename="overview.jpg",
        content_type="image/jpeg",
    )
    client.upload_walk_pet_trail_image("walk-1", "pet-1", b"pet-trail-bytes")

    assert [request.url.path for request in requests] == [
        "/walk/walk-1/trail-thumbnail",
        "/walk/walk-1/pet/pet-1/trail-image",
    ]
    assert all(
        request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
        for request in requests
    )
    assert b'name="trail-thumbnail"' in requests[0].content
    assert b'filename="overview.jpg"' in requests[0].content
    assert b"thumbnail-bytes" in requests[0].content
    assert b'name="trail-image"' in requests[1].content
    assert b'filename="trail-image.png"' in requests[1].content
    assert b"pet-trail-bytes" in requests[1].content


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda client: client.set_walk_paused("walk-1", "collar-1", 1), "boolean"),
        (lambda client: client.stop_walk("walk-1", "collar-1", stop_option="bad"), "stop option"),
        (
            lambda client: client.mark_walk_ended(
                "walk-1",
                started_at="2026-07-30T18:10:00Z",
                ended_at="2026-07-30T18:41:12Z",
                pets=[],
                user={},
                location_name=None,
            ),
            "pets",
        ),
        (lambda client: client.upload_walk_trail_thumbnail("walk-1", b""), "bytes"),
    ],
)
def test_walk_mutations_validate_local_inputs(tmp_path, call, message) -> None:
    client = _stub_client(tmp_path, lambda _: pytest.fail("request should not be sent"))

    with pytest.raises(ValueError, match=message):
        call(client)


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


def test_collar_binding_routes_match_the_observed_bodies(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/collar/check-can-be-bound-to-user":
            return httpx.Response(
                200,
                json={
                    "result": True,
                    "collarType": "version5",
                    "collarRequiresReactivationFee": False,
                },
            )
        if request.url.path == "/collar/bind-to-user":
            return httpx.Response(
                200,
                json={
                    "collar": {
                        "id": "collar-1",
                        "type": "version5",
                        "serialNumber": "26h5160491th",
                    },
                    "reportedConfigurationVersionBeforeBinding": "1",
                },
            )
        raise AssertionError(request.url)

    client = _stub_client(tmp_path, handler)
    eligibility = client.check_collar_binding("26h5160491th")
    bound = client.bind_collar("26h5160491th", "encrypted-serial")

    assert eligibility["result"] is True
    assert bound["collar"]["id"] == "collar-1"
    assert [request.method for request in requests] == ["PUT", "PUT"]
    assert [request.url.path for request in requests] == [
        "/collar/check-can-be-bound-to-user",
        "/collar/bind-to-user",
    ]
    assert json.loads(requests[0].content) == {"SerialNumber": "26h5160491th"}
    assert json.loads(requests[1].content) == {
        "SerialNumber": "26h5160491th",
        "EncryptedSerialNumber": "encrypted-serial",
    }


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("check_collar_binding", (" ",)),
        ("bind_collar", (" ", "encrypted-serial")),
        ("bind_collar", ("26h5160491th", "")),
    ],
)
def test_collar_binding_requires_serial_values(tmp_path, method, args) -> None:
    client = _stub_client(tmp_path, lambda _: pytest.fail("request should not be sent"))

    with pytest.raises(ValueError, match="required"):
        getattr(client, method)(*args)


def test_collar_pet_provisioning_routes_and_snapshot_checks(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/collar/collar-1":
            return httpx.Response(
                200,
                json={"id": "collar-1", "petInfo": {"id": "pet-1", "name": "Scout"}},
            )
        if request.method == "GET" and request.url.path == "/pet/pet-1":
            return httpx.Response(
                200,
                json={
                    "id": "pet-1",
                    "collarInfo": {"id": "collar-1"},
                    "isCollarBindingToPetSynchronized": True,
                },
            )
        return httpx.Response(204)

    client = _stub_client(tmp_path, handler)
    client.bind_collar_to_pet("pet-1", "collar-1")
    pet = client.pet("pet-1", refresh_telemetry=True)
    collar = client.collar("collar-1")
    client.unbind_collar_from_pet("pet-1")
    client.unbind_collar_from_user("collar-1")

    assert client.pet_collar_binding_is_synchronized(pet, "collar-1")
    assert not client.pet_collar_binding_is_synchronized(pet, "collar-2")
    assert client.collar_is_assigned_to_pet(collar, "pet-1")
    assert not client.collar_is_assigned_to_pet(collar, "pet-2")
    assert [(request.method, request.url.path) for request in requests] == [
        ("PUT", "/pet/pet-1/bind-collar"),
        ("GET", "/pet/pet-1"),
        ("GET", "/collar/collar-1"),
        ("PUT", "/pet/pet-1/unbind-collar"),
        ("POST", "/collar/collar-1/unbind-from-user"),
    ]
    assert json.loads(requests[0].content) == {"CollarId": "collar-1"}
    assert parse_qs(requests[1].url.query.decode()) == {"RefreshTelemetry": ["True"]}
    assert requests[3].content == b""
    assert requests[4].content == b""


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("collar", ("../collar",)),
        ("bind_collar_to_pet", ("pet-1", "../collar")),
        ("bind_collar_to_pet", ("../pet", "collar-1")),
        ("unbind_collar_from_pet", ("../pet",)),
        ("unbind_collar_from_user", ("../collar",)),
    ],
)
def test_collar_pet_provisioning_rejects_path_injection(tmp_path, method, args) -> None:
    client = _stub_client(tmp_path, lambda _: pytest.fail("request should not be sent"))

    with pytest.raises(ValueError):
        getattr(client, method)(*args)


def test_firmware_status_reads_only_the_proven_collar_routes(tmp_path) -> None:
    requests: list[httpx.Request] = []
    active = {
        "id": "collar-1",
        "serialNumber": "SERIAL-1",
        "type": "version5",
        "firmware": {
            "id": "installed-1",
            "version": "03.08.00",
            "firmwareLatestProduction": False,
            "firmwareLatestBeta": False,
        },
        "hasFirmwareUpdatesAvailable": True,
        "firmwareUpdate": {
            "firmware": {"id": "target-1", "version": "03.09.00"},
            "update": {"status": "downloading"},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/collar/my":
            return httpx.Response(200, json=[active])
        if request.url.path == "/collar/collar-1":
            return httpx.Response(200, json=active)
        raise AssertionError(request.url)

    client = _stub_client(tmp_path, handler)
    statuses = client.firmware_statuses()
    status = client.firmware_status("collar-1")

    assert statuses == [status]
    assert status == {
        "collarId": "collar-1",
        "serialNumber": "SERIAL-1",
        "collarType": "version5",
        "firmware": active["firmware"],
        "hasFirmwareUpdatesAvailable": True,
        "firmwareUpdate": active["firmwareUpdate"],
        "updateStatus": "downloading",
    }
    assert client.firmware_update_state(status) is FirmwareUpdateStatus.DOWNLOADING
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/collar/my"),
        ("GET", "/collar/collar-1"),
    ]


def test_firmware_state_enum_covers_the_proven_wire_values() -> None:
    assert {item.value for item in FirmwareUpdateStatus} == {
        "unknown",
        "downloadDelayedIncompatibleNetwork",
        "downloadDelayedLowBattery",
        "downloading",
        "downloadFailed",
        "verifying",
        "verifyFailed",
        "applyDelayedNotCharging",
        "applying",
        "applyFailed",
        "downloadDelayedNotOnCharger",
        "applied",
        "downloadNotStarted",
    }
    assert FirmwareUpdateStatus.parse("download-delayed-low-battery") is (
        FirmwareUpdateStatus.DOWNLOAD_DELAYED_LOW_BATTERY
    )
    assert HaloClient.firmware_update_state({"updateStatus": "futureState"}) == "futureState"
    assert HaloClient.firmware_update_state({"firmwareUpdate": None}) is None


def test_profile_and_email_change_routes_use_the_expected_dtos(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/user-profile":
            return httpx.Response(200, json={"firstName": "Pat"})
        if request.method == "PUT" and request.url.path == "/user-profile":
            return httpx.Response(200, json={"firstName": "Taylor"})
        if request.method == "GET" and request.url.path == "/user-profile/onboarding/progress":
            return httpx.Response(200, json={"version": 3, "steps": []})
        if request.method == "PUT" and request.url.path == "/user-profile/onboarding/progress":
            return httpx.Response(200, json={"version": 4, "progressState": "fullyCompleted"})
        if request.method == "GET" and request.url.path == "/user-profile/questionnaire":
            return httpx.Response(200, json={"haveTrainedDogsBefore": True})
        if request.method == "PUT" and request.url.path == "/account/email-change-request":
            return httpx.Response(200, json="cancelled")
        return httpx.Response(204)

    client = _stub_client(tmp_path, handler)
    assert client.user_profile() == {"firstName": "Pat"}
    assert client.update_profile_name("Taylor", "Quinn") == {"firstName": "Taylor"}
    client.upload_profile_avatar(b"avatar", filename="avatar.jpg", content_type="image/jpeg")
    client.delete_profile_avatar()
    assert client.onboarding_progress()["version"] == 3
    assert client.update_onboarding_progress(
        version=3,
        steps=["TheHaloCollarApp"],
        progress_state="FullyCompleted",
    )["version"] == 4
    assert client.questionnaire()["haveTrainedDogsBefore"] is True
    assert client.save_questionnaire({"HaveTrainedDogsBefore": True}) is None
    client.check_user_can_change_email("new@example.com")
    client.request_email_change("new@example.com")
    client.confirm_email_change("123456")
    client.resend_email_change_confirmation()
    assert client.cancel_email_change() == "cancelled"
    client.delete_account()

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/user-profile"),
        ("PUT", "/user-profile"),
        ("PUT", "/user-profile/me/icon"),
        ("DELETE", "/user-profile/me/icon"),
        ("GET", "/user-profile/onboarding/progress"),
        ("PUT", "/user-profile/onboarding/progress"),
        ("GET", "/user-profile/questionnaire"),
        ("PUT", "/user-profile/questionnaire"),
        ("POST", "/account/check-user-can-change-email"),
        ("POST", "/account/email-change-request"),
        ("POST", "/account/email-change-request/confirm"),
        ("POST", "/account/email-change-request/resend-email"),
        ("PUT", "/account/email-change-request"),
        ("DELETE", "/account"),
    ]
    assert json.loads(requests[1].content) == {"FirstName": "Taylor", "LastName": "Quinn"}
    assert b'name="icon"' in requests[2].content
    assert b'filename="avatar.jpg"' in requests[2].content
    assert json.loads(requests[5].content) == {
        "Version": 3,
        "Steps": [{"Id": "TheHaloCollarApp"}],
        "ProgressState": "FullyCompleted",
    }
    assert json.loads(requests[7].content) == {"HaveTrainedDogsBefore": True}
    assert json.loads(requests[8].content) == {"Email": "new@example.com"}
    assert json.loads(requests[9].content) == {"Email": "new@example.com"}
    assert json.loads(requests[10].content) == {"Code": "123456"}


@pytest.mark.parametrize(
    "call",
    [
        lambda client: client.update_profile_name("", "Quinn"),
        lambda client: client.update_onboarding_progress(
            version=-1,
            steps=[],
            progress_state="FullyCompleted",
        ),
        lambda client: client.update_onboarding_progress(
            version=1,
            steps=[{}],
            progress_state="FullyCompleted",
        ),
        lambda client: client.save_questionnaire({}),
        lambda client: client.confirm_email_change(""),
    ],
)
def test_profile_writes_validate_required_inputs(tmp_path, call) -> None:
    client = _stub_client(tmp_path, lambda _: pytest.fail("request should not be sent"))
    with pytest.raises(ValueError):
        call(client)


def test_pet_mode_routes_keep_fences_and_beacons_separate(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/pet/pet-1/instant-mode":
            return httpx.Response(
                200,
                json={
                    "desiredMode": {"fencesOn": False, "beaconsOn": True},
                    "telemetry": {
                        "mode": {"fencesOn": True, "beaconsOn": True},
                    },
                },
            )
        if request.url.path == "/beacon/set-is-assigned/pet-1":
            return httpx.Response(200, json={"isAssigned": False})
        raise AssertionError(request.url)

    client = _stub_client(tmp_path, handler)
    mode = client.set_pet_fences_enabled("pet-1", False)
    beacon = client.set_pet_beacons_assigned("pet-1", False)

    assert mode["desiredMode"]["fencesOn"] is False
    assert mode["telemetry"]["mode"]["fencesOn"] is True
    assert beacon == {"isAssigned": False}
    assert [request.method for request in requests] == ["PUT", "PUT"]
    assert [request.url.path for request in requests] == [
        "/pet/pet-1/instant-mode",
        "/beacon/set-is-assigned/pet-1",
    ]
    assert json.loads(requests[0].content) == {
        "ModePatch": {"FencesOn": False, "BeaconsOn": None}
    }
    assert json.loads(requests[1].content) == {"IsAssigned": False}
    assert requests[0].headers["Halo-Amplitude-SessionId"] == "1700000000000"
    assert requests[1].headers["Halo-Amplitude-SessionId"] == "1700000000000"


def test_beacon_assignment_tolerates_an_empty_success_response(tmp_path) -> None:
    client = _stub_client(tmp_path, lambda _: httpx.Response(204))

    assert client.set_pet_beacons_assigned("pet-1", True) is None


@pytest.mark.parametrize(
    ("method", "value"),
    [
        ("set_pet_fences_enabled", 1),
        ("set_pet_fences_enabled", "false"),
        ("set_pet_beacons_assigned", 0),
        ("set_pet_beacons_assigned", None),
    ],
)
def test_pet_mode_routes_require_real_booleans(tmp_path, method, value) -> None:
    client = _stub_client(tmp_path, lambda _: pytest.fail("request should not be sent"))

    with pytest.raises(ValueError, match="boolean"):
        getattr(client, method)("pet-1", value)


@pytest.mark.parametrize(
    "method",
    ["set_pet_fences_enabled", "set_pet_beacons_assigned"],
)
def test_pet_mode_routes_reject_path_injection(tmp_path, method) -> None:
    client = _stub_client(tmp_path, lambda _: pytest.fail("request should not be sent"))

    with pytest.raises(ValueError):
        getattr(client, method)("../pet-2", True)


def test_beacons_uses_the_observed_route_and_preserves_configuration(tmp_path) -> None:
    payload = {
        "beacons": [],
        "availableRanges": [
            {"level": 1, "radiusInDecibel": -42},
            {"level": 3, "radiusInDecibel": -50},
        ],
        "defaultRange": {"level": 3, "radiusInDecibel": -50},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/beacon/my"
        assert request.url.query == b""
        return httpx.Response(200, json=payload)

    client = _stub_client(tmp_path, handler)

    assert client.beacons() == payload


def test_beacon_name_and_binding_checks_match_the_contract(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/beacon/check-name-uniqueness":
            return httpx.Response(204 if len(requests) == 1 else 409)
        return httpx.Response(
            200,
            json={
                "result": False,
                "isBeaconBoundToCurrentUser": True,
                "isBeaconBoundToAnotherUser": False,
            },
        )

    client = _stub_client(tmp_path, handler)

    assert client.beacon_name_is_available("Kitchen") is True
    assert client.beacon_name_is_available("Kitchen", beacon_id="beacon-1") is False
    binding = client.check_beacon_binding("BEACON-SERIAL")

    assert binding["isBeaconBoundToCurrentUser"] is True
    assert json.loads(requests[0].content) == {"Id": None, "Name": "Kitchen"}
    assert json.loads(requests[1].content) == {"Id": "beacon-1", "Name": "Kitchen"}
    assert requests[2].url.path == "/beacon/check-can-be-bound-to-user"
    assert json.loads(requests[2].content) == {"SerialNumber": "BEACON-SERIAL"}


def test_add_beacon_sends_pascal_case_configuration(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={
                "id": "beacon-1",
                "name": "Kitchen",
                "modelType": "usb",
                "petsSync": [
                    {"petId": "pet-1", "status": "pending", "isAssigned": True}
                ],
            },
        )

    client = _stub_client(tmp_path, handler)
    created = client.add_beacon(
        name="Kitchen",
        serial_number="BEACON-SERIAL",
        model_type=BeaconModelType.USB,
        action_type=BeaconActionType.KEEP_AWAY,
        should_notify=True,
        beacon_range={"Level": 3, "RadiusInDecibel": -50},
        is_enabled=True,
        transmission_rate_milliseconds=1000,
        correction_escalation_type=BeaconCorrectionEscalationType.WARNING,
        pet_id="pet-1",
    )

    assert created["id"] == "beacon-1"
    assert json.loads(requests[0].content) == {
        "Name": "Kitchen",
        "SerialNumber": "BEACON-SERIAL",
        "ModelType": "Usb",
        "Range": {"Level": 3, "RadiusInDecibel": -50},
        "IsEnabled": True,
        "ActionType": "KeepAway",
        "ShouldNotify": True,
        "TransmissionRateMilliseconds": 1000,
        "CorrectionEscalationType": "Warning",
        "PetId": "pet-1",
    }


def test_update_beacon_distinguishes_omitted_fields_from_null(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "beacon-1", "name": "Back Door"})

    client = _stub_client(tmp_path, handler)
    updated = client.update_beacon(
        "beacon-1",
        name="Back Door",
        is_enabled=None,
        action_type="ignore-fences",
        beacon_range={"Level": 5, "RadiusInDecibel": -57},
        pet_id=None,
    )

    assert updated["name"] == "Back Door"
    assert requests[0].url.path == "/beacon/beacon-1"
    assert json.loads(requests[0].content) == {
        "Name": "Back Door",
        "IsEnabled": None,
        "ActionType": "IgnoreFences",
        "Range": {"Level": 5, "RadiusInDecibel": -57},
        "PetId": None,
    }


def test_delete_beacon_and_upload_telemetry_use_empty_successes(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    client = _stub_client(tmp_path, handler)

    assert client.delete_beacon("beacon-1") is None
    assert (
        client.upload_beacon_telemetry(
            [
                {"SerialNumber": "BEACON-1", "BatteryChargePercent": 85},
                {"SerialNumber": "BEACON-2", "BatteryChargePercent": 42},
            ]
        )
        is None
    )
    assert requests[0].method == "DELETE"
    assert requests[0].url.path == "/beacon/beacon-1"
    assert requests[0].content == b""
    assert requests[1].url.path == "/beacon/telemetry"
    assert json.loads(requests[1].content) == {
        "BeaconsTelemetry": [
            {"SerialNumber": "BEACON-1", "BatteryChargePercent": 85},
            {"SerialNumber": "BEACON-2", "BatteryChargePercent": 42},
        ]
    }


def test_beacon_pet_sync_reads_async_distribution_state(tmp_path) -> None:
    payload = {
        "beacons": [
            {
                "id": "beacon-1",
                "petsSync": [
                    {"petId": "pet-1", "status": "completed", "isAssigned": True},
                    {"petId": "pet-2", "status": "skipped", "isAssigned": True},
                ],
            }
        ]
    }
    client = _stub_client(tmp_path, lambda _: httpx.Response(200, json=payload))

    assert [item["status"] for item in client.beacon_pet_sync("beacon-1")] == [
        "completed",
        "skipped",
    ]


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            lambda client: client.add_beacon(
                name="Kitchen",
                serial_number="serial",
                model_type="bad",
                action_type="KeepAway",
                should_notify=True,
            ),
            "model type",
        ),
        (
            lambda client: client.update_beacon("beacon-1"),
            "at least one",
        ),
        (
            lambda client: client.add_beacon(
                name="Kitchen",
                serial_number="serial",
                model_type="Usb",
                action_type="KeepAway",
                should_notify=True,
                beacon_range={"Level": 3},
            ),
            "RadiusInDecibel",
        ),
        (
            lambda client: client.upload_beacon_telemetry(
                [{"SerialNumber": "serial", "BatteryChargePercent": 101}]
            ),
            "between 0 and 100",
        ),
    ],
)
def test_beacon_mutations_validate_local_inputs(tmp_path, call, message) -> None:
    client = _stub_client(tmp_path, lambda _: pytest.fail("request should not be sent"))

    with pytest.raises(ValueError, match=message):
        call(client)


def test_push_subscription_matches_the_expected_bodies(tmp_path) -> None:
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
    # /collar/my only reaches pets with a collar bound, so this endpoint is the
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


def test_geo_fence_add_matches_the_expected_body(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "geoFence": {
                    "id": "fence-1",
                    "zones": [],
                    "petsSync": [
                        {"petId": "pet-1", "status": "pending", "isAssigned": True}
                    ],
                },
                "status": "success",
            },
        )

    client = _stub_client(tmp_path, handler)
    result = client.add_geo_fence(
        "My Fence 1",
        [(40.0001, -75.0001), (40.0002, -75.00015), (40.0003, -75.00005)],
    )

    assert result["status"] == "success"
    assert result["geoFence"]["petsSync"] == [
        {"petId": "pet-1", "status": "pending", "isAssigned": True}
    ]
    assert json.loads(requests[0].content) == {
        "Name": "My Fence 1",
        "LocationPoints": [
            {"Latitude": 40.0001, "Longitude": -75.0001},
            {"Latitude": 40.0002, "Longitude": -75.00015},
            {"Latitude": 40.0003, "Longitude": -75.00005},
        ],
        "PublicVisibilityType": "Private",
        "Analytics": None,
    }


def test_safe_zone_preview_sends_warning_boundary_and_returns_generated_zone(tmp_path) -> None:
    requests: list[httpx.Request] = []
    generated = [
        {
            "areaInSquareMeters": 2293.78,
            "type": "safe",
            "locationPoints": [
                {"latitude": 44.0, "longitude": -92.0},
                {"latitude": 44.001, "longitude": -92.0},
                {"latitude": 44.0, "longitude": -92.001},
            ],
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=generated)

    points = [(44.0, -92.0), (44.001, -92.0), (44.0, -92.001)]
    client = _stub_client(tmp_path, handler)

    assert client.geo_fence_safe_zones(points) == generated
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/geo-fence/safe-zones"
    assert json.loads(requests[0].content) == {
        "LocationPoints": [
            {"Latitude": 44.0, "Longitude": -92.0},
            {"Latitude": 44.001, "Longitude": -92.0},
            {"Latitude": 44.0, "Longitude": -92.001},
        ],
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


def test_geo_fence_rename_and_location_update_match_observed_responses(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/geo-fence/fence-1":
            return httpx.Response(200)
        if request.url.path == "/geo-fence/fence-1/location":
            return httpx.Response(200, json={"status": "success"})
        raise AssertionError(request.url)

    points = [(44.0, -92.0), (44.001, -92.0), (44.0, -92.001)]
    analytics = {"AreaInSquareMeters": 1968.91, "Warnings": []}
    client = _stub_client(tmp_path, handler)

    assert client.rename_geo_fence("fence-1", "New name") is None
    assert client.update_geo_fence_location("fence-1", points, analytics=analytics) == {
        "status": "success"
    }
    assert json.loads(requests[0].content) == {"Name": "New name"}
    assert json.loads(requests[1].content) == {
        "LocationPoints": [
            {"Latitude": 44.0, "Longitude": -92.0},
            {"Latitude": 44.001, "Longitude": -92.0},
            {"Latitude": 44.0, "Longitude": -92.001},
        ],
        "Analytics": analytics,
    }


def test_delete_geo_fence_uses_delete_and_validates_the_id(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "success"})

    client = _stub_client(tmp_path, handler)
    assert client.delete_geo_fence("22222222-2222-4222-8222-222222222222") == {"status": "success"}
    assert requests[0].method == "DELETE"
    assert requests[0].content == b""
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


def test_parcel_lookup_builds_the_expected_point_literal(tmp_path) -> None:
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


def test_add_pet_matches_the_expected_body(tmp_path) -> None:
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
