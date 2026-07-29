"""Rendering for the command line: tables for people, JSON for programs.

Halo's payloads are deeply nested, so only collections get a table. Anything
whose shape a table would misrepresent — one pet, the map, the configuration —
prints as JSON whatever the format, because a flattened half-view of it would be
a worse lie than the braces.

Redaction lives here too. The summaries keep coordinates, signed URLs, Wi-Fi
details, and email addresses out of terminal output nobody asked for; every
command that uses one also takes ``--full`` to print what Halo actually sent.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

MISSING = "-"


@dataclass(frozen=True)
class Column:
    """One table column: a heading and the key it reads."""

    heading: str
    key: str


@dataclass
class Output:
    """Where a command writes, and in which format.

    Data goes to stdout so it can be piped; notices go to stderr so they do not
    contaminate that pipe. ``--quiet`` silences notices only, never data.
    """

    as_json: bool = False
    plain: bool = False
    quiet: bool = False
    stdout: Any = None
    stderr: Any = None

    def __post_init__(self) -> None:
        self.stdout = self.stdout or sys.stdout
        self.stderr = self.stderr or sys.stderr

    def note(self, message: str) -> None:
        """Report a state change or a caveat. Never part of the piped output."""

        if not self.quiet:
            print(message, file=self.stderr)

    def json(self, value: Any) -> None:
        print(json.dumps(value, indent=2, sort_keys=True), file=self.stdout)

    def text(self, value: str) -> None:
        print(value, file=self.stdout)

    def emit(
        self,
        data: Any,
        *,
        rows: list[dict[str, Any]] | None = None,
        columns: list[Column] | None = None,
        pairs: dict[str, Any] | None = None,
    ) -> None:
        """Render one command's result.

        ``data`` is what `--json` prints and is always the whole truth. ``rows``
        and ``pairs`` are the human view of that same data; a command that has
        no sensible flat view passes neither and prints JSON to everyone.
        """

        if self.as_json or (rows is None and pairs is None):
            self.json(data)
        elif pairs is not None:
            self._pairs(pairs)
        else:
            self._table(rows or [], columns or [])

    def _table(self, rows: list[dict[str, Any]], columns: list[Column]) -> None:
        cells = [[_cell(row.get(column.key)) for column in columns] for row in rows]
        if self.plain:
            for line in cells:
                print("\t".join(line), file=self.stdout)
            return
        if not rows:
            self.note("No results.")
            return
        headings = [column.heading for column in columns]
        widths = [
            max(len(headings[index]), *(len(line[index]) for line in cells))
            if cells
            else len(headings[index])
            for index in range(len(columns))
        ]
        header = "  ".join(h.ljust(w) for h, w in zip(headings, widths, strict=True))
        print(header.rstrip(), file=self.stdout)
        for line in cells:
            row = "  ".join(c.ljust(w) for c, w in zip(line, widths, strict=True))
            print(row.rstrip(), file=self.stdout)

    def _pairs(self, value: dict[str, Any]) -> None:
        if self.plain:
            for key, item in value.items():
                print(f"{key}\t{_cell(item)}", file=self.stdout)
            return
        width = max((len(key) for key in value), default=0)
        for key, item in value.items():
            print(f"{key.ljust(width)}  {_cell(item)}", file=self.stdout)


def _cell(value: Any) -> str:
    if value is None or value == "":
        return MISSING
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def safe_collar_summary(collars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Avoid dumping Wi-Fi SSIDs, hardware UUIDs, and full telemetry by default."""

    from .client import HaloClient

    result = []
    for collar in collars:
        telemetry = collar.get("telemetry")
        pet = collar.get("petInfo")
        result.append(
            {
                "id": collar.get("id"),
                "serialNumber": collar.get("serialNumber"),
                "type": collar.get("type"),
                "configurationSyncStatus": collar.get("configurationSyncStatus"),
                "pet": (
                    {"id": pet.get("id"), "name": pet.get("name")}
                    if isinstance(pet, dict)
                    else None
                ),
                "batteryChargePercent": (
                    telemetry.get("batteryChargePercent") if isinstance(telemetry, dict) else None
                ),
                "online": HaloClient.collar_is_online(collar),
            }
        )
    return result


def safe_pet_summary(pets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Avoid dumping live coordinates and signed report URLs by default."""

    result = []
    for pet in pets:
        collar = pet.get("collarInfo")
        result.append(
            {
                "id": pet.get("id"),
                "name": pet.get("name"),
                "breed": pet.get("breed"),
                "collar": (
                    {"id": collar.get("id"), "serialNumber": collar.get("serialNumber")}
                    if isinstance(collar, dict)
                    else None
                ),
                "isCollarEverAssigned": pet.get("isCollarEverAssigned"),
                "fencesState": pet.get("fencesState"),
                "beaconsState": pet.get("beaconsState"),
            }
        )
    return result


def safe_fence_summary(fences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Avoid dumping zone coordinates, the fence address, and signed thumbnails."""

    result = []
    for fence in fences:
        zones = fence.get("zones")
        pets_sync = fence.get("petsSync")
        result.append(
            {
                "id": fence.get("id"),
                "name": fence.get("name"),
                "description": fence.get("description"),
                "activityType": fence.get("activityType"),
                "isEnabled": fence.get("isEnabled"),
                "publicVisibilityType": fence.get("publicVisibilityType"),
                "zones": (
                    [
                        {
                            "type": zone.get("type"),
                            "pointCount": len(zone.get("locationPoints") or []),
                        }
                        for zone in zones
                        if isinstance(zone, dict)
                    ]
                    if isinstance(zones, list)
                    else None
                ),
                "petsSync": (
                    [
                        {
                            "petId": entry.get("petId"),
                            "isAssigned": entry.get("isAssigned"),
                            "status": entry.get("status"),
                        }
                        for entry in pets_sync
                        if isinstance(entry, dict)
                    ]
                    if isinstance(pets_sync, list)
                    else None
                ),
            }
        )
    return result


def safe_profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    """Avoid dumping email addresses, the avatar URL, and the referral link."""

    coupon = profile.get("referralCoupon")
    return {
        "id": profile.get("id"),
        "userId": profile.get("userId"),
        "firstName": profile.get("firstName"),
        "lastName": profile.get("lastName"),
        "hasChangeEmailRequest": profile.get("hasChangeEmailRequest"),
        "hasCompletedQuestionnaire": profile.get("hasCompletedQuestionnaire"),
        "hasFinishedUserGuide": profile.get("hasFinishedUserGuide"),
        "onboardingProgressState": profile.get("onboardingProgressState"),
        "referralCoupon": (
            {"amount": coupon.get("amount"), "canShare": coupon.get("canShare")}
            if isinstance(coupon, dict)
            else None
        ),
    }


def safe_map_summary(account_map: dict[str, Any]) -> dict[str, Any]:
    """Summarize the map payload with the same redactions the other commands use."""

    pets = account_map.get("pets")
    fences_info = account_map.get("geoFencesInfo")
    fences = fences_info.get("geoFencesToDisplay") if isinstance(fences_info, dict) else None
    corrections = account_map.get("corrections")
    return {
        "pets": safe_pet_summary(pets) if isinstance(pets, list) else None,
        "fences": safe_fence_summary(fences) if isinstance(fences, list) else None,
        "geoFencesTotalCount": (
            fences_info.get("geoFencesTotalCount") if isinstance(fences_info, dict) else None
        ),
        # The correction records have never been observed populated, so there is
        # no verified shape to redact; --full is the honest way to read them.
        "correctionCount": len(corrections) if isinstance(corrections, list) else None,
    }
