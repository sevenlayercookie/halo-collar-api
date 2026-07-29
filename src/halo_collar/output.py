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
NULL = "null"

# A table stops earning its keep once it wraps or runs to hundreds of rows, so
# past these limits a payload is better read as JSON than as a mangled grid.
MAX_TABLE_WIDTH = 120
MAX_TABLE_ROWS = 200
MAX_COLUMNS = 10


@dataclass(frozen=True)
class Column:
    """One table column: a heading and the key it reads."""

    heading: str
    key: str


@dataclass(frozen=True)
class Fields:
    """A run of scalar keys, rendered as FIELD/VALUE."""

    heading: str | None
    mapping: dict[str, Any]


@dataclass(frozen=True)
class Rows:
    """A list of records, rendered as a table."""

    heading: str | None
    rows: list[dict[str, Any]]
    columns: list[Column]


@dataclass(frozen=True)
class Document:
    """A value no table can hold, kept as JSON beside ones that can."""

    heading: str | None
    value: Any


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _row_columns(items: list[Any]) -> list[Column] | None:
    """Columns for a list of records, or None if it is not one worth tabulating."""

    if not items or len(items) > MAX_TABLE_ROWS:
        return None
    if not all(isinstance(item, dict) for item in items):
        return None
    keys = sorted({key for item in items for key in item})
    if not keys or len(keys) > MAX_COLUMNS:
        return None
    return [Column(key.upper(), key) for key in keys]


def _fits(rows: list[dict[str, Any]], columns: list[Column]) -> bool:
    widths = [
        max(
            len(column.heading),
            *(len(_cell(row.get(column.key), missing=NULL)) for row in rows),
        )
        for column in columns
    ]
    return sum(widths) + 2 * (len(columns) - 1) <= MAX_TABLE_WIDTH


Section = Fields | Rows | Document


def _fields_fit(mapping: dict[str, Any]) -> bool:
    """A field table with a 1500-character cell in it is not a table."""

    return all(len(_cell(item, missing=NULL)) <= MAX_TABLE_WIDTH for item in mapping.values())


def auto_sections(value: Any) -> list[Section] | None:
    """Lay out a payload as tables, or return None to leave it as JSON.

    Shallow things read better as a grid: a run of scalar fields, a list of flat
    records, a small nested object. Anything deeper or wider than that — one
    pet, the configuration, a notification row carrying twenty-one columns — is
    left alone, because a table that wraps is harder to read than the JSON it
    came from.

    One exception earns its keep: a payload that is mostly metadata around a
    single oversized collection, like a page of walks, shows the metadata as a
    table and leaves that one collection as JSON. Two such collections and the
    result is a JSON document with headings sprinkled in it, so it bails.
    """

    if isinstance(value, list):
        columns = _row_columns(value)
        if columns is None or not _fits(value, columns):
            return None
        return [Rows(None, value, columns)]
    if not isinstance(value, dict) or not value:
        return None

    sections: list[Section] = []
    documents = 0
    run: dict[str, Any] = {}

    def flush() -> None:
        nonlocal run
        if run:
            sections.append(Fields(None, run))
            run = {}

    # Sorted so the table and `--json`, which also sorts, agree on order.
    for key, item in sorted(value.items()):
        if _is_scalar(item) or item == [] or item == {}:
            run[key] = item
        elif isinstance(item, dict) and all(_is_scalar(inner) for inner in item.values()):
            if not _fields_fit(item):
                return None
            flush()
            sections.append(Fields(key.upper(), item))
        elif isinstance(item, list):
            columns = _row_columns(item)
            if columns is not None and _fits(item, columns):
                flush()
                sections.append(Rows(key.upper(), item, columns))
                continue
            documents += 1
            if documents > 1:
                return None
            flush()
            sections.append(Document(key.upper(), item))
        else:
            return None
    if not _fields_fit(run):
        return None
    flush()
    if not sections or all(isinstance(section, Document) for section in sections):
        return None
    return sections


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
            # stdout is block-buffered when redirected while stderr is not, so
            # without this a footer note overtakes the table it belongs under.
            self.stdout.flush()
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

        if self.as_json:
            self.json(data)
        elif _is_scalar(data):
            # A bare timestamp or count reads better without JSON's quotes.
            self.text(_cell(data, missing=NULL))
        elif pairs is not None:
            self._sections([Fields(None, pairs)])
        elif rows is not None:
            self._table(rows, columns or [])
        else:
            # Nothing curated: work out whether the payload is shallow enough to
            # tabulate on its own, and leave it as JSON when it is not.
            sections = auto_sections(data)
            if sections is None:
                self.json(data)
            else:
                self._sections(sections)

    def _sections(self, sections: list[Section]) -> None:
        for index, section in enumerate(sections):
            if index:
                print("", file=self.stdout)
            if section.heading and not self.plain:
                print(section.heading, file=self.stdout)
            if isinstance(section, Document):
                self.json(section.value)
            elif isinstance(section, Rows):
                self._table(section.rows, section.columns, missing=NULL)
            else:
                self._table(
                    [{"field": key, "value": item} for key, item in section.mapping.items()],
                    [Column("FIELD", "field"), Column("VALUE", "value")],
                    missing=NULL,
                )

    def _table(
        self,
        rows: list[dict[str, Any]],
        columns: list[Column],
        *,
        missing: str = MISSING,
    ) -> None:
        cells = [
            [_cell(row.get(column.key), missing=missing) for column in columns] for row in rows
        ]
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


def _cell(value: Any, *, missing: str = MISSING) -> str:
    """Render one value.

    Booleans and nulls print as Halo sent them rather than as English, because
    this is a view of an API and `true` is what the field actually says. The
    ``missing`` placeholder differs by caller: a curated column showing a dash
    means "nothing to show here", while `null` in a field table means Halo
    returned null.
    """

    if value is None:
        return missing
    if value == "":
        return MISSING
    if isinstance(value, bool):
        return "true" if value else "false"
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
        # The correction records have has no stable populated shape, so there is
        # no verified shape to redact; --full is the honest way to read them.
        "correctionCount": len(corrections) if isinstance(corrections, list) else None,
    }
