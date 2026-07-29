from __future__ import annotations

import argparse
import json

import pytest

from halo_collar import ANDROID_CLIENT_SECRET, TokenSet, cli
from halo_collar.output import Output


@pytest.fixture
def interactive(monkeypatch):
    """Pretend stdin is a terminal, so confirmation prompts are reachable."""

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)


def args(**overrides) -> argparse.Namespace:
    """A namespace shaped like one the parser produces for a leaf command."""

    return argparse.Namespace(**overrides)


def test_password_login_prompts_securely_and_stores_android_session(
    tmp_path,
    monkeypatch,
    capsys,
    interactive,
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
            "auth",
            "login",
            "--password",
        ]
    )

    assert result == 0
    # Notices are stderr so that piped stdout stays clean.
    assert "Login successful with the Halo Android profile" in capsys.readouterr().err
    assert prompted == ["Halo account password (input hidden; never stored): "]
    state = json.loads(state_path.read_text())
    assert state["auth_profile"]["client_id"] == "halo.app.android"
    assert state["auth_profile"]["app_version"] == "2.12.0.590"
    assert state["tokens"]["refresh_token"] == "refresh"
    assert state["settings"]["timezone"] == "America/Chicago"
    assert "person@example.com" not in state_path.read_text()
    assert "account-password" not in state_path.read_text()


def test_no_arguments_prints_concise_help(capsys) -> None:
    assert cli.main([]) == 0

    out = capsys.readouterr().out
    assert "USAGE" in out
    assert "halo <noun> <verb>" in out
    assert "halo pet list" in out
    assert cli.SUPPORT_URL in out


def test_a_noun_without_a_verb_shows_that_nouns_help(capsys) -> None:
    assert cli.main(["pet"]) == 0

    out = capsys.readouterr().out
    assert "halo pet" in out
    for verb in ("list", "show", "add", "update", "delete", "colors"):
        assert verb in out


def test_help_reaches_every_level(capsys) -> None:
    assert cli.main(["help"]) == 0
    assert "<noun>" in capsys.readouterr().out

    assert cli.main(["help", "fence"]) == 0
    assert "move" in capsys.readouterr().out

    assert cli.main(["help", "pet", "add"]) == 0
    out = capsys.readouterr().out
    assert "--color-hex" in out
    assert "EXAMPLES" in out


def test_every_leaf_command_documents_itself() -> None:
    """A command nobody can read the help for is not finished."""

    parser = cli.build_parser()
    nouns = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    checked = 0
    for noun, noun_parser in nouns.choices.items():
        if noun == "help":
            continue
        verbs = [
            action
            for action in noun_parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        assert verbs, f"{noun} has no verbs"
        assert noun_parser.description, f"{noun} has no description"
        for verb, leaf in verbs[0].choices.items():
            assert leaf.description, f"{noun} {verb} has no description"
            assert "EXAMPLES" in (leaf.epilog or ""), f"{noun} {verb} has no examples"
            assert cli.SUPPORT_URL in (leaf.epilog or ""), f"{noun} {verb} has no support link"
            checked += 1
    assert checked > 20


def test_retired_commands_say_where_they_went(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["pets"])

    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "unknown command 'pets'" in err
    assert "halo pet list" in err


def test_a_near_miss_suggests_a_command(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["fence-delete"])

    assert "halo fence delete" in capsys.readouterr().err


def test_global_flags_work_before_or_after_the_verb() -> None:
    parser = cli.build_parser()

    before = parser.parse_args(["--json", "pet", "list"])
    after = parser.parse_args(["pet", "list", "--json"])

    assert cli._flag(before, "as_json", False) is True
    assert cli._flag(after, "as_json", False) is True
    assert cli._flag(parser.parse_args(["pet", "list"]), "as_json", False) is False


def test_table_output_aligns_and_plain_output_is_tab_separated(capsys) -> None:
    rows = [
        {"name": "Julep", "collar": None, "online": True},
        {"name": "Mallard", "collar": "26h5160491th", "online": False},
    ]
    columns = [cli.Column("NAME", "name"), cli.Column("COLLAR", "collar")]

    Output().emit(rows, rows=rows, columns=columns)
    table = capsys.readouterr().out.splitlines()
    assert table[0].split() == ["NAME", "COLLAR"]
    assert table[1].startswith("Julep")
    # A missing value reads as a dash rather than an empty column.
    assert table[1].split()[1] == "-"

    Output(plain=True).emit(rows, rows=rows, columns=columns)
    assert capsys.readouterr().out.splitlines()[0] == "Julep\t-"

    Output(as_json=True).emit(rows, rows=rows, columns=columns)
    assert json.loads(capsys.readouterr().out) == rows


def test_booleans_read_as_words_in_a_table(capsys) -> None:
    rows = [{"online": True, "enabled": False}]
    Output().emit(
        rows, rows=rows, columns=[cli.Column("ONLINE", "online"), cli.Column("ENABLED", "enabled")]
    )

    assert capsys.readouterr().out.splitlines()[1].split() == ["yes", "no"]


def test_quiet_silences_notices_but_never_data(capsys) -> None:
    out = Output(quiet=True)
    out.note("this is a notice")
    out.emit({"kept": True})

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"kept": True}


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

    summary = cli.safe_pet_summary(pets)

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
    summary = cli.safe_fence_summary(fences())

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


def test_fence_rows_count_zones_and_assigned_pets() -> None:
    rows = cli._fence_rows(cli.safe_fence_summary(fences()))

    assert rows[0]["zones"] == 2
    assert rows[0]["petsSync"] == 1


def test_map_summary_redacts_pets_and_fences_and_counts_corrections() -> None:
    summary = cli.safe_map_summary(
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
    rendered = json.dumps(summary)
    for secret in ("40.0001", "SECRETSIG", "Springfield"):
        assert secret not in rendered


def test_map_summary_tolerates_missing_sections() -> None:
    assert cli.safe_map_summary({}) == {
        "pets": None,
        "fences": None,
        "geoFencesTotalCount": None,
        "correctionCount": None,
    }


def test_profile_summary_hides_email_avatar_and_referral_link() -> None:
    summary = cli.safe_profile_summary(
        {
            "id": "user-1",
            "userId": "auth-1",
            "firstName": "Pat",
            "lastName": "Quinn",
            "email": "person@example.com",
            "currentEmail": "person@example.com",
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


def test_pet_update_keeps_unspecified_fields(capsys) -> None:
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

    result = cli._pet_update(
        args(pet_id="pet-1", name=None, color_hex=None, breed=None, birthday=None, weight_kg=31.0),
        FakeClient(),
        Output(),
    )

    assert result == 0
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

    with pytest.raises(ValueError) as excinfo:
        cli._pet_update(
            args(
                pet_id="pet-1",
                name=None,
                color_hex=None,
                breed=None,
                birthday=None,
                weight_kg=None,
            ),
            FakeClient(),
            Output(),
        )

    assert "birthday" in str(excinfo.value)
    assert "breed" in str(excinfo.value)


def test_fence_mutations_cancel_unless_the_identifier_is_typed(
    monkeypatch, capsys, interactive
) -> None:
    class FakeClient:
        def __init__(self):
            self.calls = 0

        def add_geo_fence(self, name, points):
            self.calls += 1
            return {"geoFence": {"id": "fence-1", "name": name}}

        def update_geo_fence_location(self, fence_id, points):
            self.calls += 1
            return {"status": "success"}

    points = ["40.0,-75.0", "40.1,-75.1", "40.2,-75.2"]
    add_args = args(name="Back yard", point=points, yes=False, full=False)
    move_args = args(fence_id="fence-1", point=points, yes=False)
    client = FakeClient()

    monkeypatch.setattr("builtins.input", lambda _: "not the name")
    assert cli._fence_add(add_args, client, Output()) == 1
    assert cli._fence_move(move_args, client, Output()) == 1
    assert client.calls == 0
    assert "no fence was created" in capsys.readouterr().err

    monkeypatch.setattr("builtins.input", lambda _: "Back yard")
    assert cli._fence_add(add_args, client, Output()) == 0
    monkeypatch.setattr("builtins.input", lambda _: "fence-1")
    assert cli._fence_move(move_args, client, Output()) == 0
    assert client.calls == 2


def test_a_destructive_command_refuses_rather_than_assume_consent(monkeypatch) -> None:
    """Without a terminal there is nobody to ask, and silence is not a yes."""

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    class FakeClient:
        def pet(self, pet_id):
            return {"name": "Alpha"}

        def delete_pet(self, pet_id):  # pragma: no cover - must never run
            raise AssertionError("deleted without confirmation")

    with pytest.raises(ValueError) as excinfo:
        cli._pet_delete(args(pet_id="pet-1", yes=False), FakeClient(), Output())

    assert "--yes" in str(excinfo.value)


def test_no_input_blocks_a_prompt_even_on_a_terminal(interactive) -> None:
    class FakeClient:
        def pet(self, pet_id):
            return {"name": "Alpha"}

        def delete_pet(self, pet_id):  # pragma: no cover - must never run
            raise AssertionError("deleted without confirmation")

    with pytest.raises(ValueError):
        cli._pet_delete(args(pet_id="pet-1", yes=False, no_input=True), FakeClient(), Output())


def test_yes_skips_the_prompt_for_scripts(capsys) -> None:
    deleted: list[str] = []

    class FakeClient:
        def pet(self, pet_id):
            return {"name": "Alpha"}

        def delete_pet(self, pet_id):
            deleted.append(pet_id)

    assert cli._pet_delete(args(pet_id="pet-1", yes=True), FakeClient(), Output()) == 0
    assert deleted == ["pet-1"]
    assert "Deleted Alpha" in capsys.readouterr().err


def test_pet_delete_cancels_unless_the_name_is_typed(monkeypatch, capsys, interactive) -> None:
    class FakeClient:
        def __init__(self):
            self.deleted: list[str] = []

        def pet(self, pet_id):
            return {"id": pet_id, "name": "Alpha"}

        def delete_pet(self, pet_id):
            self.deleted.append(pet_id)

    client = FakeClient()

    monkeypatch.setattr("builtins.input", lambda _: "wrong")
    assert cli._pet_delete(args(pet_id="pet-1", yes=False), client, Output()) == 1
    assert client.deleted == []
    assert "was not deleted" in capsys.readouterr().err

    monkeypatch.setattr("builtins.input", lambda _: "Alpha")
    assert cli._pet_delete(args(pet_id="pet-1", yes=False), client, Output()) == 0
    assert client.deleted == ["pet-1"]


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

    add_args = args(
        name="Back yard",
        point=["40.0,-75.0", "40.1,-75.1", "40.2,-75.2"],
        yes=True,
        full=False,
    )

    assert cli._fence_add(add_args, FakeClient(), Output()) == 0
    out = capsys.readouterr().out
    assert "fence-1" in out
    assert "SECRETSIG" not in out

    add_args.full = True
    assert cli._fence_add(add_args, FakeClient(), Output()) == 0
    assert "SECRETSIG" in capsys.readouterr().out
