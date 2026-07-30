from __future__ import annotations

import os

import pytest

from halo_collar import (
    CommandCounterUnknownError,
    LoginRequiredError,
    StateStore,
    TokenSet,
)


def test_state_is_owner_only_and_atomic(tmp_path) -> None:
    store = StateStore(tmp_path / "nested" / "state.json")
    store.save_tokens(TokenSet("access", "refresh", 123.0))
    assert store.load_tokens().refresh_token == "refresh"
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o777 == 0o600
        assert store.path.parent.stat().st_mode & 0o777 == 0o700


def test_counter_is_reserved_before_dispatch(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    assert store.reserve_command_number("pet", 10) == 10
    assert store.reserve_command_number("pet") == 11
    assert store.reserve_command_number("pet") == 12


def test_session_is_bound_to_client_profile_and_tokens_can_be_cleared(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save_session(
        TokenSet("access", "refresh", 123.0),
        client_id="halo.app.android",
        app_version="2.12.0.590",
    )

    assert store.auth_profile() == {
        "client_id": "halo.app.android",
        "app_version": "2.12.0.590",
    }
    # The application credential is never persisted.
    assert "client_secret" not in store.path.read_text()
    assert store.clear_tokens()
    with pytest.raises(LoginRequiredError):
        store.load_tokens()
    assert store.auth_profile()["client_id"] == "halo.app.android"


def test_first_correction_requires_an_explicit_command_number(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    with pytest.raises(CommandCounterUnknownError, match="--command-number"):
        store.reserve_command_number("pet")
