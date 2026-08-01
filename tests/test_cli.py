from __future__ import annotations

import argparse
import asyncio
import json

import pytest

from halo_collar import (
    ANDROID_CLIENT_SECRET,
    CorrectionRuleKindType,
    CorrectionRuleUpdate,
    SignalREvent,
    SignalRHub,
    TokenSet,
    cli,
)
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
    for verb in (
        "list",
        "show",
        "add",
        "update",
        "delete",
        "bind-collar",
        "unbind-collar",
        "colors",
        "fences",
        "beacons",
    ):
        assert verb in out


def test_collar_help_lists_binding_commands(capsys) -> None:
    assert cli.main(["collar"]) == 0

    out = capsys.readouterr().out
    for verb in ("list", "show", "locate", "check-binding", "bind", "remove"):
        assert verb in out


def test_firmware_help_is_read_only(capsys) -> None:
    assert cli.main(["firmware"]) == 0

    out = capsys.readouterr().out
    assert "    list" in out
    assert "    show" in out
    assert "    start" not in out
    assert "    cancel" not in out


def test_account_help_lists_profile_and_email_commands(capsys) -> None:
    assert cli.main(["account"]) == 0
    out = capsys.readouterr().out
    for verb in (
        "update-name",
        "avatar-upload",
        "avatar-delete",
        "onboarding",
        "onboarding-update",
        "questionnaire",
        "questionnaire-save",
        "email-check",
        "email-request",
        "email-confirm",
        "email-resend",
        "email-cancel",
        "delete",
    ):
        assert verb in out


def test_beacon_help_lists_management_commands(capsys) -> None:
    assert cli.main(["beacon"]) == 0

    out = capsys.readouterr().out
    for verb in (
        "list",
        "check-name",
        "check-binding",
        "sync",
        "add",
        "update",
        "delete",
        "telemetry",
    ):
        assert verb in out


def test_walk_help_lists_existing_walk_operations(capsys) -> None:
    assert cli.main(["walk"]) == 0

    out = capsys.readouterr().out
    for verb in (
        "list",
        "summary",
        "pause",
        "resume",
        "stop",
        "mark-ended",
        "upload-thumbnail",
        "upload-pet-image",
    ):
        assert verb in out


def test_correction_help_lists_rule_editing_and_collar_testing(capsys) -> None:
    assert cli.main(["correction"]) == 0

    out = capsys.readouterr().out
    for verb in ("send", "rules", "config", "update", "test"):
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


@pytest.mark.parametrize(
    ("verb", "hub"),
    [
        ("telemetry", SignalRHub.TELEMETRY),
        ("notifications", SignalRHub.NOTIFICATIONS),
    ],
)
def test_live_commands_emit_json_lines_and_close_the_stream(
    verb,
    hub,
    monkeypatch,
    capsys,
) -> None:
    streams = []

    class FakeClient:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    class FakeSignalRClient:
        def __init__(self, client, *, hub):
            self.hub = hub
            self.connected = False
            self.closed = False
            self.events = [
                SignalREvent(
                    hub=hub,
                    target="HandleIoTTelemetry",
                    arguments=[{"petId": "pet-1", "latitude": 40.0}],
                    raw={
                        "type": 1,
                        "target": "HandleIoTTelemetry",
                        "arguments": [{"petId": "pet-1", "latitude": 40.0}],
                    },
                )
            ]
            streams.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            self.closed = True

        async def wait_connected(self):
            self.connected = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.events:
                raise StopAsyncIteration
            return self.events.pop(0)

    monkeypatch.setattr(cli, "HaloClient", FakeClient)
    monkeypatch.setattr(cli, "HaloSignalRClient", FakeSignalRClient)

    assert cli.main(["live", verb]) == cli.EXIT_OK

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "hub": hub.value,
        "type": 1,
        "target": "HandleIoTTelemetry",
        "arguments": [{"petId": "pet-1", "latitude": 40.0}],
    }
    assert captured.out.count("\n") == 1
    assert "precise location data" in captured.err
    assert streams[0].connected is True
    assert streams[0].closed is True


def test_live_filters_accept_one_pet_and_multiple_targets() -> None:
    parsed = cli.build_parser().parse_args(
        [
            "live",
            "telemetry",
            "--pet-id",
            "pet-1",
            "--target",
            "HandleIoTTelemetry",
            "--target",
            "HandleDataStateChanged",
        ]
    )
    event = SignalREvent(
        hub=SignalRHub.TELEMETRY,
        target="HandleIoTTelemetry",
        arguments=[{"petId": "pet-1"}],
        raw={},
    )

    assert parsed.pet_id == "pet-1"
    assert parsed.target == ["HandleIoTTelemetry", "HandleDataStateChanged"]
    assert cli._live_event_matches(
        event,
        pet_id=parsed.pet_id,
        targets=set(parsed.target),
    )
    assert not cli._live_event_matches(event, pet_id="pet-2", targets=set())
    assert not cli._live_event_matches(event, pet_id=None, targets={"OtherTarget"})


def test_cancelling_live_stream_closes_signalr(monkeypatch) -> None:
    entered = asyncio.Event()
    closed = False

    class FakeSignalRClient:
        def __init__(self, client, *, hub):
            pass

        async def __aenter__(self):
            entered.set()
            return self

        async def __aexit__(self, *_):
            nonlocal closed
            closed = True

        async def wait_connected(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()
            raise StopAsyncIteration

    async def scenario() -> None:
        monkeypatch.setattr(cli, "HaloSignalRClient", FakeSignalRClient)
        task = asyncio.create_task(
            cli._stream_live_events(
                args(pet_id=None, target=[]),
                object(),
                Output(),
                SignalRHub.TELEMETRY,
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert closed is True


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


def test_booleans_print_as_the_api_spells_them(capsys) -> None:
    rows = [{"online": True, "enabled": False}]
    Output().emit(
        rows, rows=rows, columns=[cli.Column("ONLINE", "online"), cli.Column("ENABLED", "enabled")]
    )

    assert capsys.readouterr().out.splitlines()[1].split() == ["true", "false"]


def test_a_shallow_object_becomes_field_tables_around_its_lists(capsys) -> None:
    Output().emit(
        {
            "accessLevel": "basic",
            "maxCollarsCount": 1,
            "temporaryPrivileges": None,
            "features": [
                {"id": "findcollar", "isEnabled": True},
                {"id": "deleteaccount", "isEnabled": False},
            ],
        }
    )

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["FIELD", "VALUE"]
    assert lines[1].split() == ["accessLevel", "basic"]
    # Sorted, so the run splits either side of the list exactly as --json orders it.
    assert "FEATURES" in lines
    features = lines.index("FEATURES")
    assert lines[features + 1].split() == ["ID", "ISENABLED"]
    assert lines[features + 2].split() == ["findcollar", "true"]
    # A null Halo actually sent reads as null, not as an absent-value dash.
    assert ["temporaryPrivileges", "null"] in [line.split() for line in lines]


def test_a_scalar_prints_bare_rather_than_quoted(capsys) -> None:
    Output().emit("2026-07-29T18:25:25+00:00")

    assert capsys.readouterr().out.strip() == "2026-07-29T18:25:25+00:00"


def test_a_deep_payload_stays_json(capsys) -> None:
    payload = {"collarInfo": {"telemetry": {"wiFi": {"status": "socketconnected"}}}, "id": "pet-1"}
    Output().emit(payload)

    assert json.loads(capsys.readouterr().out) == payload


def test_a_wide_row_stays_json_rather_than_wrapping(capsys) -> None:
    payload = {"results": [{f"column{index}": "x" * 20 for index in range(9)}]}
    Output().emit(payload)

    assert json.loads(capsys.readouterr().out) == payload


def test_an_enormous_field_value_stays_json(capsys) -> None:
    payload = {"status": 200, "body": "x" * 1500}
    Output().emit(payload)

    assert json.loads(capsys.readouterr().out) == payload


def test_one_oversized_collection_still_lets_its_metadata_tabulate(capsys) -> None:
    rows = [{f"column{index}": "x" * 20 for index in range(9)}]
    Output().emit({"pageNumber": 1, "pageSize": 2, "results": rows})

    out = capsys.readouterr().out
    assert "pageNumber" in out.splitlines()[1]
    assert "RESULTS" in out
    assert json.loads(out[out.index("RESULTS") + len("RESULTS") :]) == rows


def test_two_oversized_collections_are_left_alone(capsys) -> None:
    wide = [{f"column{index}": "x" * 20 for index in range(9)}]
    payload = {"pageNumber": 1, "results": wide, "others": wide}
    Output().emit(payload)

    assert json.loads(capsys.readouterr().out) == payload


def test_quiet_silences_notices_but_never_data(capsys) -> None:
    out = Output(quiet=True)
    out.note("this is a notice")
    out.emit({"kept": True})

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "kept" in captured.out and "true" in captured.out


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


def test_account_profile_management_handlers(tmp_path, capsys) -> None:
    avatar = tmp_path / "avatar.jpg"
    avatar.write_bytes(b"avatar-bytes")
    progress = tmp_path / "onboarding.json"
    progress.write_text(
        json.dumps(
            {
                "Version": 3,
                "Steps": [{"Id": "TheHaloCollarApp"}],
                "ProgressState": "FullyCompleted",
            }
        )
    )
    questionnaire = tmp_path / "questionnaire.json"
    questionnaire.write_text(json.dumps({"HaveTrainedDogsBefore": True}))
    calls = []

    class FakeClient:
        def update_profile_name(self, first_name, last_name):
            calls.append(("name", first_name, last_name))
            return {"firstName": first_name}

        def upload_profile_avatar(self, image, **kwargs):
            calls.append(("upload", image, kwargs))

        def delete_profile_avatar(self):
            calls.append(("avatar-delete",))

        def update_onboarding_progress(self, **kwargs):
            calls.append(("onboarding", kwargs))
            return {"version": 4}

        def save_questionnaire(self, value):
            calls.append(("questionnaire", value))
            return None

    client = FakeClient()
    assert (
        cli._account_update_name(
            args(first_name="Taylor", last_name="Quinn"),
            client,
            Output(),
        )
        == 0
    )
    assert (
        cli._account_avatar_upload(
            args(image_file=str(avatar), content_type="image/jpeg"),
            client,
            Output(),
        )
        == 0
    )
    assert cli._account_avatar_delete(args(yes=True), client, Output()) == 0
    assert (
        cli._account_onboarding_update(
            args(progress_file=str(progress)),
            client,
            Output(),
        )
        == 0
    )
    assert (
        cli._account_questionnaire_save(
            args(questionnaire_file=str(questionnaire)),
            client,
            Output(),
        )
        == 0
    )
    assert calls == [
        ("name", "Taylor", "Quinn"),
        ("upload", b"avatar-bytes", {"filename": "avatar.jpg", "content_type": "image/jpeg"}),
        ("avatar-delete",),
        (
            "onboarding",
            {
                "version": 3,
                "steps": [{"Id": "TheHaloCollarApp"}],
                "progress_state": "FullyCompleted",
            },
        ),
        ("questionnaire", {"HaveTrainedDogsBefore": True}),
    ]
    assert "Saved onboarding progress" in capsys.readouterr().err


def test_account_email_handlers_and_deletion_confirmation(capsys) -> None:
    calls = []

    class FakeClient:
        def check_user_can_change_email(self, email):
            calls.append(("check", email))

        def request_email_change(self, email):
            calls.append(("request", email))

        def confirm_email_change(self, code):
            calls.append(("confirm", code))

        def resend_email_change_confirmation(self):
            calls.append(("resend",))

        def cancel_email_change(self):
            calls.append(("cancel",))
            return "cancelled"

        def user_profile(self):
            return {"currentEmail": "person@example.com"}

        def delete_account(self):
            calls.append(("delete",))

    client = FakeClient()
    assert cli._account_email_check(args(email="new@example.com"), client, Output()) == 0
    assert (
        cli._account_email_request(
            args(email="new@example.com", yes=True),
            client,
            Output(),
        )
        == 0
    )
    assert cli._account_email_confirm(args(code="123456", yes=True), client, Output()) == 0
    assert cli._account_email_resend(args(), client, Output()) == 0
    assert cli._account_email_cancel(args(yes=True), client, Output(as_json=True)) == 0
    assert cli._account_delete(args(yes=True), client, Output()) == 0
    assert calls == [
        ("check", "new@example.com"),
        ("request", "new@example.com"),
        ("confirm", "123456"),
        ("resend",),
        ("cancel",),
        ("delete",),
    ]
    assert "Deleted the Halo account" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("handler", "command_args"),
    [
        (cli._account_avatar_delete, {}),
        (cli._account_email_request, {"email": "new@example.com"}),
        (cli._account_email_confirm, {"code": "123456"}),
        (cli._account_email_cancel, {}),
        (cli._account_delete, {}),
    ],
)
def test_account_destructive_profile_commands_need_confirmation(handler, command_args) -> None:
    class FakeClient:
        def user_profile(self):
            return {"email": "person@example.com"}

        def __getattr__(self, _):
            return lambda *_args, **_kwargs: pytest.fail("profile mutation should not be sent")

    with pytest.raises(ValueError, match="--yes"):
        handler(args(**command_args, yes=False, no_input=True), FakeClient(), Output())


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


def test_notification_rows_show_the_field_each_type_populates() -> None:
    rows = [
        {
            "id": "n-1",
            "type": "collarlowbatterythresholdreached",
            "date": "2026-07-13T15:47:32Z",
            "pet": {"id": "pet-1", "name": "Mallard"},
            "status": "read",
            "batteryChargePercent": 4,
            "correctionsCount": None,
            "notificationZone": None,
        },
        {
            "id": "n-2",
            "type": "correctionsapplied",
            "date": "2026-07-14T09:00:00Z",
            "pet": {"id": "pet-1", "name": "Mallard"},
            "status": "unread",
            "batteryChargePercent": None,
            "correctionsCount": 3,
            "notificationZone": "warning",
        },
        {
            "id": "n-3",
            "type": "somethingnew",
            "date": None,
            "pet": None,
            "status": "unread",
            "title": "A type this client has never seen",
        },
    ]

    assert [cli._notification_row(row) for row in rows] == [
        {
            "when": "2026-07-13 15:47",
            "pet": "Mallard",
            "type": "collarlowbatterythresholdreached",
            "detail": "4% battery",
            "status": "read",
            "id": "n-1",
        },
        {
            "when": "2026-07-14 09:00",
            "pet": "Mallard",
            "type": "correctionsapplied",
            "detail": "3 corrections",
            "status": "unread",
            "id": "n-2",
        },
        {
            "when": None,
            "pet": None,
            "type": "somethingnew",
            "detail": "A type this client has never seen",
            "status": "unread",
            "id": "n-3",
        },
    ]


def test_notification_list_tables_the_rows_and_notes_the_page(capsys) -> None:
    class FakeClient:
        def notifications(self, *, page, page_size):
            return {
                "pageNumber": 2,
                "pageSize": 30,
                "totalNumberOfPages": 4,
                "totalNumberOfItems": 97,
                "results": [
                    {
                        "id": "n-1",
                        "type": "collarlowbatterythresholdreached",
                        "date": "2026-07-13T15:47:32Z",
                        "pet": {"name": "Mallard"},
                        "status": "read",
                        "batteryChargePercent": 4,
                    }
                ],
            }

    result = cli._notification_list(args(page=2, page_size=30, full=False), FakeClient(), Output())

    assert result == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0].split() == [
        "WHEN",
        "PET",
        "TYPE",
        "DETAIL",
        "STATUS",
        "ID",
    ]
    assert "4% battery" in captured.out
    # Paging is metadata about the request, so it must not land in the pipe.
    assert "Page 2 of 4 (97 notifications)." in captured.err
    assert "Page 2" not in captured.out


def test_notification_list_full_keeps_the_whole_envelope(capsys) -> None:
    """--full means unredacted, so the paging envelope and every field survive."""

    envelope = {
        "pageNumber": 1,
        "results": [{"id": "n-1", "type": "x", "walkId": None, "calibrationId": "c-1"}],
    }

    class FakeClient:
        def notifications(self, *, page, page_size):
            return envelope

    result = cli._notification_list(args(page=1, page_size=30, full=True), FakeClient(), Output())

    assert result == 0
    out = capsys.readouterr().out
    assert "pageNumber" in out
    assert "CALIBRATIONID" in out
    # The curated view is not applied on top of the raw payload.
    assert "DETAIL" not in out


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


def test_pet_fences_accepts_yes_and_preserves_both_modes(capsys) -> None:
    response = {
        "desiredMode": {"fencesOn": False, "beaconsOn": True},
        "telemetry": {"mode": {"fencesOn": True, "beaconsOn": True}},
    }

    class FakeClient:
        def set_pet_fences_enabled(self, pet_id, enabled):
            assert pet_id == "pet-1"
            assert enabled is False
            return response

    assert (
        cli._pet_fences(
            args(pet_id="pet-1", state="off", yes=True),
            FakeClient(),
            Output(as_json=True),
        )
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == response
    assert "Compare desiredMode with telemetry.mode" in captured.err


@pytest.mark.parametrize(("state", "expected"), [("on", True), ("off", False)])
def test_pet_beacons_maps_cli_state_to_boolean(capsys, state, expected) -> None:
    class FakeClient:
        def set_pet_beacons_assigned(self, pet_id, assigned):
            assert pet_id == "pet-1"
            assert assigned is expected
            return None

    assert (
        cli._pet_beacons(
            args(pet_id="pet-1", state=state, yes=True),
            FakeClient(),
            Output(as_json=True),
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"beacon assignment {state}" in captured.err


@pytest.mark.parametrize(("handler", "state"), [(cli._pet_fences, "off"), (cli._pet_beacons, "on")])
def test_pet_mode_changes_need_confirmation_without_a_terminal(handler, state) -> None:
    class FakeClient:
        def __getattr__(self, _):
            return lambda *_: pytest.fail("pet mode should not be changed")

    with pytest.raises(ValueError, match="--yes"):
        handler(
            args(pet_id="pet-1", state=state, yes=False, no_input=True),
            FakeClient(),
            Output(),
        )


def test_beacon_checks_and_sync_emit_complete_results(capsys) -> None:
    class FakeClient:
        def beacon_name_is_available(self, name, *, beacon_id):
            assert (name, beacon_id) == ("Kitchen", "beacon-1")
            return False

        def check_beacon_binding(self, serial_number):
            assert serial_number == "SERIAL"
            return {"result": True}

        def beacon_pet_sync(self, beacon_id):
            assert beacon_id == "beacon-1"
            return [{"petId": "pet-1", "status": "completed", "isAssigned": True}]

    client = FakeClient()
    assert (
        cli._beacon_check_name(
            args(name="Kitchen", beacon_id="beacon-1"),
            client,
            Output(as_json=True),
        )
        == 0
    )
    assert (
        cli._beacon_check_binding(
            args(serial_number="SERIAL"),
            client,
            Output(as_json=True),
        )
        == 0
    )
    assert (
        cli._beacon_sync(
            args(beacon_id="beacon-1"),
            client,
            Output(as_json=True),
        )
        == 0
    )
    output = capsys.readouterr().out
    decoder = json.JSONDecoder()
    values = []
    while output.strip():
        value, index = decoder.raw_decode(output)
        values.append(value)
        output = output[index:].lstrip()
    assert values == [
        {"available": False},
        {"result": True},
        [{"petId": "pet-1", "status": "completed", "isAssigned": True}],
    ]


def test_beacon_add_builds_range_and_accepts_yes(capsys) -> None:
    class FakeClient:
        def add_beacon(self, **kwargs):
            assert kwargs == {
                "name": "Kitchen",
                "serial_number": "SERIAL",
                "model_type": "Usb",
                "action_type": "KeepAway",
                "should_notify": True,
                "beacon_range": {"Level": 3, "RadiusInDecibel": -50},
                "is_enabled": True,
                "transmission_rate_milliseconds": 1000,
                "correction_escalation_type": "Warning",
                "pet_id": "pet-1",
            }
            return {"id": "beacon-1", "name": "Kitchen"}

    assert (
        cli._beacon_add(
            args(
                name="Kitchen",
                serial_number="SERIAL",
                model_type="Usb",
                action_type="KeepAway",
                should_notify=True,
                is_enabled=True,
                range_level=3,
                radius_in_decibel=-50,
                transmission_rate_ms=1000,
                correction_escalation_type="Warning",
                pet_id="pet-1",
                yes=True,
            ),
            FakeClient(),
            Output(as_json=True),
        )
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"id": "beacon-1", "name": "Kitchen"}
    assert "Inspect petsSync" in captured.err


def test_beacon_update_sends_only_cli_fields_that_were_present(capsys) -> None:
    class FakeClient:
        def update_beacon(self, beacon_id, **kwargs):
            assert beacon_id == "beacon-1"
            assert kwargs == {
                "name": "Back Door",
                "action_type": "IgnoreFences",
                "beacon_range": {"Level": 5, "RadiusInDecibel": -57},
            }
            return {"id": "beacon-1", "name": "Back Door"}

    assert (
        cli._beacon_update(
            args(
                beacon_id="beacon-1",
                name="Back Door",
                action_type="IgnoreFences",
                range_level=5,
                radius_in_decibel=-57,
                yes=True,
            ),
            FakeClient(),
            Output(as_json=True),
        )
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out)["name"] == "Back Door"
    assert "Updated beacon beacon-1" in captured.err


def test_beacon_update_rejects_a_no_op_before_confirmation() -> None:
    with pytest.raises(ValueError, match="at least one"):
        cli._beacon_update(
            args(beacon_id="beacon-1", yes=False, no_input=True),
            object(),
            Output(),
        )


def test_beacon_delete_accepts_yes(capsys) -> None:
    deleted = []

    class FakeClient:
        def delete_beacon(self, beacon_id):
            deleted.append(beacon_id)

    assert (
        cli._beacon_delete(
            args(beacon_id="beacon-1", yes=True),
            FakeClient(),
            Output(),
        )
        == 0
    )
    assert deleted == ["beacon-1"]
    assert "confirm removal" in capsys.readouterr().err


def test_beacon_telemetry_reads_a_json_file(tmp_path, capsys) -> None:
    readings = [{"SerialNumber": "SERIAL", "BatteryChargePercent": 85}]
    path = tmp_path / "telemetry.json"
    path.write_text(json.dumps(readings))

    class FakeClient:
        def upload_beacon_telemetry(self, value):
            assert value == readings

    assert (
        cli._beacon_telemetry(
            args(readings_file=str(path)),
            FakeClient(),
            Output(),
        )
        == 0
    )
    assert "Uploaded beacon battery telemetry" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("handler", "command_args"),
    [
        (
            cli._beacon_add,
            {
                "name": "Kitchen",
                "serial_number": "SERIAL",
                "model_type": "Usb",
                "action_type": "KeepAway",
                "should_notify": True,
                "is_enabled": None,
                "range_level": None,
                "radius_in_decibel": None,
                "transmission_rate_ms": None,
                "correction_escalation_type": None,
                "pet_id": None,
            },
        ),
        (cli._beacon_update, {"beacon_id": "beacon-1", "name": "Kitchen"}),
        (cli._beacon_delete, {"beacon_id": "beacon-1"}),
    ],
)
def test_beacon_mutations_need_confirmation(handler, command_args) -> None:
    class FakeClient:
        def __getattr__(self, _):
            return lambda *_args, **_kwargs: pytest.fail("beacon mutation should not be sent")

    with pytest.raises(ValueError, match="--yes"):
        handler(
            args(**command_args, yes=False, no_input=True),
            FakeClient(),
            Output(),
        )


def test_collar_check_binding_emits_the_complete_result(capsys) -> None:
    response = {
        "result": False,
        "isCollarBoundToCurrentUser": True,
        "collarType": "version5",
    }

    class FakeClient:
        def check_collar_binding(self, serial_number):
            assert serial_number == "26h5160491th"
            return response

    assert (
        cli._collar_check_binding(
            args(serial_number="26h5160491th"),
            FakeClient(),
            Output(as_json=True),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == response


def test_collar_bind_accepts_yes_and_emits_the_result(capsys) -> None:
    response = {
        "collar": {
            "id": "collar-1",
            "type": "version5",
            "serialNumber": "26h5160491th",
        }
    }

    class FakeClient:
        def bind_collar(self, serial_number, encrypted_serial_number):
            assert serial_number == "26h5160491th"
            assert encrypted_serial_number == "encrypted-serial"
            return response

    assert (
        cli._collar_bind(
            args(
                serial_number="26h5160491th",
                encrypted_serial_number="encrypted-serial",
                yes=True,
            ),
            FakeClient(),
            Output(as_json=True),
        )
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == response
    assert "Bound collar 26h5160491th" in captured.err


def test_collar_bind_needs_confirmation_without_a_terminal() -> None:
    class FakeClient:
        def bind_collar(self, *_):
            pytest.fail("collar should not be bound")

    with pytest.raises(ValueError, match="--yes"):
        cli._collar_bind(
            args(
                serial_number="26h5160491th",
                encrypted_serial_number="encrypted-serial",
                yes=False,
                no_input=True,
            ),
            FakeClient(),
            Output(),
        )


def test_collar_show_emits_the_complete_relationship(capsys) -> None:
    response = {"id": "collar-1", "petInfo": {"id": "pet-1", "name": "Scout"}}

    class FakeClient:
        def collar(self, collar_id):
            assert collar_id == "collar-1"
            return response

    assert (
        cli._collar_show(
            args(collar_id="collar-1"),
            FakeClient(),
            Output(as_json=True),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == response


def test_pet_bind_collar_accepts_yes_and_explains_verification(capsys) -> None:
    calls = []

    class FakeClient:
        def bind_collar_to_pet(self, pet_id, collar_id):
            calls.append((pet_id, collar_id))

    assert (
        cli._pet_bind_collar(
            args(pet_id="pet-1", collar_id="collar-1", yes=True),
            FakeClient(),
            Output(),
        )
        == 0
    )
    assert calls == [("pet-1", "collar-1")]
    notice = capsys.readouterr().err
    assert "halo pet show pet-1 --refresh-telemetry" in notice
    assert "halo collar show collar-1" in notice


def test_pet_unbind_collar_accepts_yes_and_keeps_account_binding(capsys) -> None:
    calls = []

    class FakeClient:
        def unbind_collar_from_pet(self, pet_id):
            calls.append(pet_id)

    assert (
        cli._pet_unbind_collar(
            args(pet_id="pet-1", yes=True),
            FakeClient(),
            Output(),
        )
        == 0
    )
    assert calls == ["pet-1"]
    assert "collarInfo=null" in capsys.readouterr().err


def test_collar_remove_accepts_yes_and_warns_about_confirmation(capsys) -> None:
    calls = []

    class FakeClient:
        def unbind_collar_from_user(self, collar_id):
            calls.append(collar_id)

    assert (
        cli._collar_remove(
            args(collar_id="collar-1", yes=True),
            FakeClient(),
            Output(),
        )
        == 0
    )
    assert calls == ["collar-1"]
    assert "absent from `halo collar list`" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("handler", "command_args"),
    [
        (cli._pet_bind_collar, {"pet_id": "pet-1", "collar_id": "collar-1"}),
        (cli._pet_unbind_collar, {"pet_id": "pet-1"}),
        (cli._collar_remove, {"collar_id": "collar-1"}),
    ],
)
def test_collar_relationship_mutations_need_confirmation(handler, command_args) -> None:
    class FakeClient:
        def __getattr__(self, _):
            return lambda *_args, **_kwargs: pytest.fail("mutation should not be sent")

    with pytest.raises(ValueError, match="--yes"):
        handler(
            args(**command_args, yes=False, no_input=True),
            FakeClient(),
            Output(),
        )


def test_firmware_list_summarizes_and_preserves_full_status(capsys) -> None:
    statuses = [
        {
            "collarId": "collar-1",
            "serialNumber": "SERIAL-1",
            "firmware": {
                "version": "03.08.00",
                "formattedVersion": "03.08.00",
                "features": ["fota"],
                "firmwareLatestProduction": False,
                "firmwareLatestBeta": False,
            },
            "hasFirmwareUpdatesAvailable": True,
            "firmwareUpdate": {
                "firmware": {"version": "03.09.00"},
                "update": {"status": "downloading"},
            },
            "updateStatus": "downloading",
        }
    ]

    class FakeClient:
        def firmware_statuses(self):
            return statuses

    assert cli._firmware_list(args(full=False), FakeClient(), Output()) == 0
    summary = capsys.readouterr().out
    assert "03.08.00" in summary
    assert "03.09.00" in summary
    assert "downloading" in summary
    assert "fota" not in summary

    assert (
        cli._firmware_list(
            args(full=True),
            FakeClient(),
            Output(as_json=True),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == statuses


def test_firmware_show_emits_one_complete_status(capsys) -> None:
    status = {
        "collarId": "collar-1",
        "firmware": {"version": "03.08.00"},
        "firmwareUpdate": None,
        "updateStatus": None,
    }

    class FakeClient:
        def firmware_status(self, collar_id):
            assert collar_id == "collar-1"
            return status

    assert (
        cli._firmware_show(
            args(collar_id="collar-1"),
            FakeClient(),
            Output(as_json=True),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == status


def test_correction_update_sends_one_identified_rule(capsys) -> None:
    response = {
        "correctionRules": [{"id": "rule-1", "kindType": "sound"}],
        "lastCorrectionRulesUpdated": "2026-07-30T18:42:00Z",
    }

    class FakeClient:
        def update_correction_rules(self, items):
            assert items == [
                CorrectionRuleUpdate(
                    "rule-1",
                    CorrectionRuleKindType.SOUND,
                    level=3,
                    sound_id="sound-1",
                )
            ]
            return response

    assert (
        cli._correction_update(
            args(
                rule_id="rule-1",
                kind_type="Sound",
                level=3,
                sound_id="sound-1",
                vibration_id=None,
                yes=True,
            ),
            FakeClient(),
            Output(as_json=True),
        )
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == response
    assert "configurationSyncStatus" in captured.err


def test_correction_test_passes_direct_command_safety_options(capsys) -> None:
    response = {"result": "success", "currentCommandNumber": 13}

    class FakeClient:
        def pet(self, pet_id):
            assert pet_id == "pet-1"
            return {"id": pet_id, "name": "Scout"}

        def test_correction_on_collar(self, pet_id, kind_type, **kwargs):
            assert pet_id == "pet-1"
            assert kind_type is CorrectionRuleKindType.SHOCK
            assert kwargs == {
                "sound_id": None,
                "vibration_id": None,
                "sound_intensity_level": None,
                "shock_intensity_level": 1,
                "command_number": 13,
                "expiration_seconds": 20,
                "require_online": False,
            }
            return response

    assert (
        cli._correction_test(
            args(
                pet_id="pet-1",
                kind_type="Shock",
                level=1,
                sound_id=None,
                vibration_id=None,
                command_number=13,
                expires_in=20,
                skip_online_check=True,
                yes=True,
            ),
            FakeClient(),
            Output(as_json=True),
        )
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == response
    assert "does not confirm physical execution or save a rule" in captured.err


@pytest.mark.parametrize(
    "command_args",
    [
        {
            "rule_id": "rule-1",
            "kind_type": "Sound",
            "level": 3,
            "sound_id": None,
            "vibration_id": None,
        },
        {
            "rule_id": "rule-1",
            "kind_type": "Vibration",
            "level": 1,
            "sound_id": None,
            "vibration_id": "vibration-1",
        },
        {
            "rule_id": "rule-1",
            "kind_type": "Shock",
            "level": 0,
            "sound_id": None,
            "vibration_id": None,
        },
    ],
)
def test_correction_update_rejects_invalid_modality_options(command_args) -> None:
    class FakeClient:
        def update_correction_rules(self, _):
            pytest.fail("invalid correction rule should not be sent")

    with pytest.raises(ValueError):
        cli._correction_update(
            args(**command_args, yes=True),
            FakeClient(),
            Output(),
        )


def test_correction_rule_writes_need_confirmation() -> None:
    class FakeClient:
        def pet(self, pet_id):
            return {"id": pet_id, "name": "Scout"}

        def update_correction_rules(self, _):
            pytest.fail("rule update should not be sent")

        def test_correction_on_collar(self, *_args, **_kwargs):
            pytest.fail("collar test should not be sent")

    client = FakeClient()
    with pytest.raises(ValueError, match="--yes"):
        cli._correction_update(
            args(
                rule_id="rule-1",
                kind_type="Shock",
                level=1,
                sound_id=None,
                vibration_id=None,
                yes=False,
                no_input=True,
            ),
            client,
            Output(),
        )
    with pytest.raises(ValueError, match="--yes"):
        cli._correction_test(
            args(
                pet_id="pet-1",
                kind_type="Shock",
                level=1,
                sound_id=None,
                vibration_id=None,
                expires_in=30,
                command_number=13,
                skip_online_check=False,
                yes=False,
                no_input=True,
            ),
            client,
            Output(),
        )


def test_walk_summary_emits_the_complete_result(capsys) -> None:
    response = {"id": "walk-1", "startTrigger": "mobile", "endedAt": "2026-07-30T18:41:12Z"}

    class FakeClient:
        def walk_summary(self, walk_id):
            assert walk_id == "walk-1"
            return response

    assert (
        cli._walk_summary(
            args(walk_id="walk-1"),
            FakeClient(),
            Output(as_json=True),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == response


@pytest.mark.parametrize(
    ("handler", "paused", "action"),
    [
        (cli._walk_pause, True, "pause"),
        (cli._walk_resume, False, "resume"),
    ],
)
def test_walk_pause_and_resume_accept_yes(capsys, handler, paused, action) -> None:
    class FakeClient:
        def set_walk_paused(self, walk_id, collar_id, value):
            assert (walk_id, collar_id, value) == ("walk-1", "collar-1", paused)
            return {"result": "success"}

    assert (
        handler(
            args(walk_id="walk-1", collar_id="collar-1", yes=True),
            FakeClient(),
            Output(as_json=True),
        )
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"result": "success"}
    assert f"{action} request" in captured.err
    assert "telemetry.walk.isPaused" in captured.err


def test_walk_stop_passes_the_selected_option(capsys) -> None:
    class FakeClient:
        def stop_walk(self, walk_id, collar_id, *, stop_option):
            assert (walk_id, collar_id) == ("walk-1", "collar-1")
            assert stop_option == "ForceKeepFencesMode"
            return {"result": "success"}

    assert (
        cli._walk_stop(
            args(
                walk_id="walk-1",
                collar_id="collar-1",
                stop_option="ForceKeepFencesMode",
                yes=True,
            ),
            FakeClient(),
            Output(as_json=True),
        )
        == 0
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"result": "success"}
    assert "walk=null" in captured.err


@pytest.mark.parametrize("handler", [cli._walk_pause, cli._walk_resume, cli._walk_stop])
def test_walk_collar_commands_need_confirmation(handler) -> None:
    class FakeClient:
        def __getattr__(self, _):
            return lambda *_args, **_kwargs: pytest.fail("walk command should not be sent")

    command_args = {
        "walk_id": "walk-1",
        "collar_id": "collar-1",
        "yes": False,
        "no_input": True,
        "stop_option": "Default",
    }
    with pytest.raises(ValueError, match="--yes"):
        handler(args(**command_args), FakeClient(), Output())


def test_walk_mark_ended_reads_the_pascal_case_summary_file(tmp_path, capsys) -> None:
    summary = {
        "StartedAt": "2026-07-30T18:10:00Z",
        "EndedAt": "2026-07-30T18:41:12Z",
        "Pets": [{"Id": "pet-1", "CollarId": "collar-1"}],
        "User": {"TotalDuration": "00:31:12"},
        "LocationName": "Rochester, Minnesota",
    }
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(json.dumps(summary))

    class FakeClient:
        def mark_walk_ended(self, walk_id, **kwargs):
            assert walk_id == "walk-1"
            assert kwargs == {
                "started_at": summary["StartedAt"],
                "ended_at": summary["EndedAt"],
                "pets": summary["Pets"],
                "user": summary["User"],
                "location_name": summary["LocationName"],
            }

    assert (
        cli._walk_mark_ended(
            args(walk_id="walk-1", summary_file=str(summary_path)),
            FakeClient(),
            Output(),
        )
        == 0
    )
    assert "Submitted the completed walk summary" in capsys.readouterr().err


def test_walk_image_handlers_read_files_and_preserve_names(tmp_path, capsys) -> None:
    thumbnail = tmp_path / "overview.jpg"
    pet_image = tmp_path / "pet-trail.png"
    thumbnail.write_bytes(b"overview")
    pet_image.write_bytes(b"pet-trail")
    calls = []

    class FakeClient:
        def upload_walk_trail_thumbnail(self, walk_id, image, **kwargs):
            calls.append(("thumbnail", walk_id, image, kwargs))

        def upload_walk_pet_trail_image(self, walk_id, pet_id, image, **kwargs):
            calls.append(("pet", walk_id, pet_id, image, kwargs))

    client = FakeClient()
    assert (
        cli._walk_upload_thumbnail(
            args(
                walk_id="walk-1",
                image_file=str(thumbnail),
                content_type="image/jpeg",
            ),
            client,
            Output(),
        )
        == 0
    )
    assert (
        cli._walk_upload_pet_image(
            args(
                walk_id="walk-1",
                pet_id="pet-1",
                image_file=str(pet_image),
                content_type="image/png",
            ),
            client,
            Output(),
        )
        == 0
    )

    assert calls == [
        (
            "thumbnail",
            "walk-1",
            b"overview",
            {"filename": "overview.jpg", "content_type": "image/jpeg"},
        ),
        (
            "pet",
            "walk-1",
            "pet-1",
            b"pet-trail",
            {"filename": "pet-trail.png", "content_type": "image/png"},
        ),
    ]
    assert "processing may finish later" in capsys.readouterr().err


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
