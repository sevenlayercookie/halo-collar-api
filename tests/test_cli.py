from __future__ import annotations

import argparse
import json

import pytest

from halo_collar import ANDROID_CLIENT_SECRET, TokenSet, cli


def test_password_login_prompts_securely_and_stores_android_session(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    prompted: list[str] = []

    class FakeOAuth:
        def __init__(self, client_secret, *, profile):
            assert client_secret == ANDROID_CLIENT_SECRET
            assert profile.client_id == "halo.app.android"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def password_login(self, username, password):
            assert username == "person@example.com"
            assert password == "account-password"
            return TokenSet("access", "refresh", 4_000_000_000.0)

    def fake_getpass(prompt: str) -> str:
        prompted.append(prompt)
        return "account-password"

    monkeypatch.setattr(cli, "HaloOAuth", FakeOAuth)
    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    monkeypatch.setattr("builtins.input", lambda _: "person@example.com")
    state_path = tmp_path / "state.json"

    result = cli.main(
        [
            "--state-file",
            str(state_path),
            "--timezone",
            "America/Chicago",
            "login",
            "--password",
        ]
    )

    assert result == 0
    assert "Login successful with the Halo Android profile" in capsys.readouterr().out
    assert prompted == ["Halo account password (input hidden; never stored): "]
    state = json.loads(state_path.read_text())
    assert state["auth_profile"]["client_id"] == "halo.app.android"
    assert state["auth_profile"]["app_version"] == "2.12.0.590"
    assert state["tokens"]["refresh_token"] == "refresh"
    assert state["settings"]["timezone"] == "America/Chicago"
    assert "person@example.com" not in state_path.read_text()
    assert "account-password" not in state_path.read_text()


def test_pet_summary_hides_coordinates_and_report_urls() -> None:
    pets = [
        {
            "id": "pet-1",
            "name": "Alpha",
            "breed": "goldenretriever",
            "collarInfo": None,
            "isCollarEverAssigned": False,
            "fencesState": "allapplied",
            "beaconsState": "notapplied",
            "telemetry": None,
            "reports": [{"id": "report-1", "url": "https://signed.example/report-1"}],
        },
        {
            "id": "pet-2",
            "name": "Bravo",
            "breed": "irishsetter",
            "collarInfo": {
                "id": "collar-1",
                "serialNumber": "SN-1",
                "wiFiExtendedSettings": {"ssid": "Home Network"},
            },
            "isCollarEverAssigned": True,
            "fencesState": "allapplied",
            "beaconsState": "notapplied",
            "telemetry": {"latitude": 40.0001, "longitude": -75.0001},
            "reports": [{"id": "report-2", "url": "https://signed.example/report-2"}],
        },
    ]

    summary = cli._safe_pet_summary(pets)

    assert summary[0]["collar"] is None
    assert summary[0]["isCollarEverAssigned"] is False
    assert summary[1]["collar"] == {"id": "collar-1", "serialNumber": "SN-1"}
    rendered = json.dumps(summary)
    for secret in ("40.0001", "-75.0001", "signed.example", "Home Network"):
        assert secret not in rendered


def fences() -> list[dict[str, object]]:
    return [
        {
            "id": "fence-1",
            "name": "Home",
            "description": None,
            "activityType": "active",
            "isEnabled": True,
            "publicVisibilityType": "private",
            "address": {"city": "Springfield", "publicPlaceName": None},
            "thumbnailUrl": "https://haloprodst.blob.core.windows.net/f.png?sig=SECRETSIG&se=z",
            "petsSync": [{"petId": "pet-1", "isAssigned": True, "status": "completed"}],
            "zones": [
                {
                    "type": "safe",
                    "locationPoints": [
                        {"latitude": 40.0001, "longitude": -75.0001},
                        {"latitude": 40.0002, "longitude": -75.0002},
                    ],
                },
                {
                    "type": "danger",
                    "locationPoints": [{"latitude": 40.0003, "longitude": -75.0003}],
                },
            ],
        }
    ]


def test_fence_summary_hides_zone_coordinates_address_and_thumbnail() -> None:
    summary = cli._safe_fence_summary(fences())

    assert summary[0]["name"] == "Home"
    assert summary[0]["isEnabled"] is True
    assert summary[0]["zones"] == [
        {"type": "safe", "pointCount": 2},
        {"type": "danger", "pointCount": 1},
    ]
    assert summary[0]["petsSync"] == [{"petId": "pet-1", "isAssigned": True, "status": "completed"}]
    rendered = json.dumps(summary)
    for secret in ("40.0001", "-75.0003", "SECRETSIG", "Springfield", "blob.core.windows.net"):
        assert secret not in rendered


def test_map_summary_redacts_pets_and_fences_and_counts_corrections() -> None:
    summary = cli._safe_map_summary(
        {
            "pets": [
                {
                    "id": "pet-1",
                    "name": "Alpha",
                    "telemetry": {"latitude": 40.0001, "longitude": -75.0001},
                }
            ],
            "geoFencesInfo": {"geoFencesToDisplay": fences(), "geoFencesTotalCount": 3},
            "corrections": [{"id": "correction-1"}, {"id": "correction-2"}],
        }
    )

    assert summary["geoFencesTotalCount"] == 3
    assert summary["correctionCount"] == 2
    assert summary["pets"][0]["name"] == "Alpha"
    assert summary["fences"][0]["zones"] == [
        {"type": "safe", "pointCount": 2},
        {"type": "danger", "pointCount": 1},
    ]
    rendered = json.dumps(summary)
    for secret in ("40.0001", "SECRETSIG", "Springfield"):
        assert secret not in rendered


def test_profile_summary_hides_email_avatar_and_referral_link() -> None:
    summary = cli._safe_profile_summary(
        {
            "id": "user-1",
            "userId": "auth-1",
            "firstName": "Pat",
            "lastName": "Quinn",
            "email": "person@example.com",
            "currentEmail": "person@example.com",
            "updatedEmail": "new@example.com",
            "iconUrl": "https://haloprodst.blob.core.windows.net/avatar.png?sig=SECRETSIG",
            "hasChangeEmailRequest": False,
            "onboardingProgressState": "finished",
            "referralCoupon": {
                "amount": 20,
                "canShare": True,
                "referralLink": "https://halo.example/r/SECRETCODE",
            },
        }
    )

    assert summary["firstName"] == "Pat"
    assert summary["referralCoupon"] == {"amount": 20, "canShare": True}
    rendered = json.dumps(summary)
    for secret in ("example.com", "SECRETSIG", "SECRETCODE", "blob.core.windows.net"):
        assert secret not in rendered


def test_points_require_three_pairs() -> None:
    assert cli._points(["40.0,-75.0", "40.1,-75.1", "40.2,-75.2"]) == [
        (40.0, -75.0),
        (40.1, -75.1),
        (40.2, -75.2),
    ]
    for bad in (["40.0,-75.0", "40.1,-75.1"], ["40.0"], ["40.0,-75.0,1", "a,b", "40.2,-75.2"]):
        with pytest.raises(ValueError):
            cli._points(bad)


def test_pet_update_keeps_unspecified_fields(monkeypatch, capsys) -> None:
    sent: dict[str, object] = {}

    class FakeClient:
        def pet(self, pet_id):
            assert pet_id == "pet-1"
            return {
                "name": "Alpha",
                "colorHex": "#ff0000",
                "breed": "goldenretriever",
                "birthday": "2021-04-17T00:00:00Z",
                "weightKg": 28.5,
            }

        def update_pet(self, pet_id, **fields):
            sent.update(fields, pet_id=pet_id)
            return {"id": pet_id, **fields}

    args = argparse.Namespace(
        pet_id="pet-1",
        name=None,
        color_hex=None,
        breed=None,
        birthday=None,
        weight_kg=31.0,
    )

    assert cli._update_pet(args, FakeClient()) == 0
    assert sent == {
        "pet_id": "pet-1",
        "name": "Alpha",
        "color_hex": "#ff0000",
        "breed": "goldenretriever",
        "birthday": "2021-04-17T00:00:00Z",
        "weight_kg": 31.0,
    }
    assert "Alpha" in capsys.readouterr().out


def test_pet_update_refuses_to_blank_a_field_halo_requires() -> None:
    class FakeClient:
        def pet(self, pet_id):
            return {"name": "Alpha", "colorHex": "#ff0000", "breed": None, "weightKg": 28.5}

    args = argparse.Namespace(
        pet_id="pet-1",
        name=None,
        color_hex=None,
        breed=None,
        birthday=None,
        weight_kg=None,
    )

    with pytest.raises(ValueError) as excinfo:
        cli._update_pet(args, FakeClient())
    assert "birthday" in str(excinfo.value)
    assert "breed" in str(excinfo.value)


def test_fence_mutations_cancel_unless_the_identifier_is_typed(monkeypatch, capsys) -> None:
    class FakeClient:
        def __init__(self):
            self.calls = 0

        def add_geo_fence(self, name, points):
            self.calls += 1
            return {"id": "fence-1"}

        def update_geo_fence_location(self, fence_id, points):
            self.calls += 1
            return {"id": fence_id}

    points = ["40.0,-75.0", "40.1,-75.1", "40.2,-75.2"]
    add_args = argparse.Namespace(name="Back yard", point=points, yes=False, full=False)
    move_args = argparse.Namespace(fence_id="fence-1", point=points, yes=False)

    monkeypatch.setattr("builtins.input", lambda _: "not the name")
    client = FakeClient()
    assert cli._add_fence(add_args, client) == 1
    assert cli._move_fence(move_args, client) == 1
    assert client.calls == 0
    assert "no fence was created" in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", lambda _: "Back yard")
    assert cli._add_fence(add_args, client) == 0
    monkeypatch.setattr("builtins.input", lambda _: "fence-1")
    assert cli._move_fence(move_args, client) == 0
    assert client.calls == 2


def test_fence_add_summarizes_the_nested_fence_halo_returns(capsys) -> None:
    class FakeClient:
        def add_geo_fence(self, name, points):
            return {
                "geoFence": {
                    "id": "fence-1",
                    "name": name,
                    "isEnabled": True,
                    "thumbnailUrl": "https://haloprodst.blob.core.windows.net/f.png?sig=SECRETSIG",
                    "zones": [
                        {"type": "safe", "locationPoints": [{"latitude": 40.0, "longitude": -75.0}]}
                    ],
                }
            }

    args = argparse.Namespace(
        name="Back yard",
        point=["40.0,-75.0", "40.1,-75.1", "40.2,-75.2"],
        yes=True,
        full=False,
    )

    assert cli._add_fence(args, FakeClient()) == 0
    out = capsys.readouterr().out
    assert '"id": "fence-1"' in out
    assert "SECRETSIG" not in out
    assert '"pointCount": 1' in out

    args.full = True
    assert cli._add_fence(args, FakeClient()) == 0
    assert "SECRETSIG" in capsys.readouterr().out


def test_video_index_qualifies_only_the_names_that_repeat() -> None:
    index = cli._video_index(
        [
            {
                "name": "introVideo",
                "section": "lms",
                "videoStreamUrl": "https://cdn.example/lms-intro.m3u8",
            },
            {
                "name": "introVideo",
                "section": "onboarding",
                "videoStreamUrl": "https://cdn.example/onboarding-intro.m3u8",
            },
            {
                "name": "packWalkVideo",
                "section": "subscription",
                "videoStreamUrl": "https://cdn.example/pack-walk.m3u8",
            },
        ]
    )

    assert index == {
        "lms.introVideo": "https://cdn.example/lms-intro.m3u8",
        "onboarding.introVideo": "https://cdn.example/onboarding-intro.m3u8",
        "packWalkVideo": "https://cdn.example/pack-walk.m3u8",
    }


def test_pet_delete_cancels_unless_the_name_is_typed(monkeypatch, capsys) -> None:
    class FakeClient:
        def __init__(self):
            self.deleted: list[str] = []

        def pet(self, pet_id):
            return {"id": pet_id, "name": "Alpha"}

        def delete_pet(self, pet_id):
            self.deleted.append(pet_id)

    args = argparse.Namespace(pet_id="pet-1", yes=False)
    client = FakeClient()

    monkeypatch.setattr("builtins.input", lambda _: "wrong")
    assert cli._delete_pet(args, client) == 1
    assert client.deleted == []
    assert "was not deleted" in capsys.readouterr().out

    monkeypatch.setattr("builtins.input", lambda _: "Alpha")
    assert cli._delete_pet(args, client) == 0
    assert client.deleted == ["pet-1"]
    assert "Deleted Alpha" in capsys.readouterr().out


def test_map_summary_tolerates_missing_sections() -> None:
    summary = cli._safe_map_summary({})

    assert summary == {
        "pets": None,
        "fences": None,
        "geoFencesTotalCount": None,
        "correctionCount": None,
    }
