"""Owner-only, atomic local storage for credentials and command counters."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from .errors import CommandCounterUnknownError, LoginRequiredError
from .models import TokenSet

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]


def _command_counters(state: dict[str, Any]) -> dict[str, Any]:
    """Return the per-pet counter mapping, repairing a corrupted entry in place."""

    counters = state.setdefault("command_counters", {})
    if not isinstance(counters, dict):
        counters = {}
        state["command_counters"] = counters
    return counters


def default_state_dir() -> Path:
    """Return a platform-appropriate per-user state directory."""

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "halo-collar"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / "halo-collar" if base else Path.home() / ".halo-collar"
    base = os.environ.get("XDG_STATE_HOME")
    return Path(base) / "halo-collar" if base else Path.home() / ".local/state/halo-collar"


class StateStore:
    """Persist OAuth state and correction counters outside the project tree.

    The JSON file and its parent directory are restricted to the current OS user.
    This prevents accidental commits and access by other local users, but it is not
    a substitute for full-disk encryption.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_state_dir() / "state.json"
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(OSError):
            self.path.parent.chmod(0o700)

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        """Lock, load, and atomically save the state after the context exits."""

        self._ensure_parent()
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            with suppress(OSError):
                os.chmod(self.lock_path, 0o600)
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            data = self._read_unlocked()
            try:
                yield data
                self._write_unlocked(data)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, Any]:
        self._ensure_parent()
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
            try:
                return self._read_unlocked()
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LoginRequiredError(
                f"Cannot read local Halo state at {self.path}. Move it aside and log in again."
            ) from exc
        if not isinstance(value, dict):
            raise LoginRequiredError(
                f"Local Halo state at {self.path} is invalid. Move it aside and log in again."
            )
        return value

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        self._ensure_parent()
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            dir=self.path.parent,
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, self.path)
            with suppress(OSError):
                self.path.chmod(0o600)
        finally:
            temporary_path.unlink(missing_ok=True)

    def load_tokens(self) -> TokenSet:
        value = self.read().get("tokens")
        if not isinstance(value, dict):
            raise LoginRequiredError("No Halo login is stored. Run `halo login`.")
        try:
            return TokenSet.from_dict(value)
        except (KeyError, TypeError, ValueError) as exc:
            raise LoginRequiredError("The stored Halo login is invalid. Run `halo login`.") from exc

    def save_tokens(self, tokens: TokenSet) -> None:
        with self.locked() as state:
            state["tokens"] = tokens.to_dict()

    def clear_tokens(self) -> bool:
        """Remove only OAuth tokens while preserving client metadata and settings."""

        with self.locked() as state:
            return state.pop("tokens", None) is not None

    def auth_profile(self) -> dict[str, str]:
        """Return the stored OAuth client metadata, or an empty mapping."""

        value = self.read().get("auth_profile")
        if isinstance(value, dict):
            required = ("client_id", "app_version")
            if all(isinstance(value.get(key), str) and value[key] for key in required):
                return {key: value[key] for key in required}
        return {}

    def save_session(
        self,
        tokens: TokenSet,
        *,
        client_id: str,
        app_version: str,
    ) -> None:
        """Atomically bind tokens to the OAuth client that issued them.

        The application credential is deliberately not persisted. Keeping a
        copy on disk would pin users to a stale value if Halo rotates it.
        """

        if not client_id or not app_version:
            raise ValueError("Complete OAuth client metadata is required.")
        with self.locked() as state:
            state["auth_profile"] = {"client_id": client_id, "app_version": app_version}
            state["tokens"] = tokens.to_dict()

    def settings(self) -> dict[str, str]:
        value = self.read().get("settings", {})
        return value.copy() if isinstance(value, dict) else {}

    def update_settings(self, **settings: str) -> None:
        with self.locked() as state:
            current = state.setdefault("settings", {})
            if not isinstance(current, dict):
                current = {}
                state["settings"] = current
            current.update(settings)

    def reserve_command_number(self, pet_id: str, explicit: int | None = None) -> int:
        """Reserve and persist a number before dispatch to avoid duplicate effects."""

        with self.locked() as state:
            counters = _command_counters(state)
            if explicit is None:
                previous = counters.get(pet_id)
                if not isinstance(previous, int):
                    raise CommandCounterUnknownError(
                        "No command counter is stored for this pet. Supply --command-number "
                        "with the next known number for the first correction."
                    )
                number = previous + 1
            else:
                if explicit < 0:
                    raise ValueError("Command number must be zero or greater.")
                number = explicit
            counters[pet_id] = number
            return number

    def reconcile_command_number(self, pet_id: str, current: int) -> None:
        with self.locked() as state:
            counters = _command_counters(state)
            previous = counters.get(pet_id)
            if not isinstance(previous, int) or current > previous:
                counters[pet_id] = current

    def clear(self) -> bool:
        """Delete all locally stored Halo credentials and counters."""

        existed = self.path.exists()
        self.path.unlink(missing_ok=True)
        self.lock_path.unlink(missing_ok=True)
        return existed
