from __future__ import annotations

import json

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
