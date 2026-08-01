"""Command-line interface for the Halo Collar client.

Commands are `noun verb`: `halo pet list`, `halo fence delete FENCE_ID`. Every
noun and every verb answers `--help`, and `halo help pet add` reaches the same
text for people who type it that way.

Data goes to stdout so it can be piped. Notices, confirmations, and errors go to
stderr so they never contaminate that pipe, which is why `halo pet delete` can
be silent on stdout and still tell you what it did.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import getpass
import json
import sys
import webbrowser
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auth import (
    ANDROID_PROFILE,
    CLIENT_PROFILES,
    IOS_PROFILE,
    HaloOAuth,
    OAuthClientProfile,
    client_profile,
    resolve_client_secret,
)
from .client import HaloClient
from .errors import (
    CorrectionOutcomeUnknownError,
    HaloError,
    StaleCommandNumberError,
    UnsafeCorrectionError,
)
from .models import (
    BeaconActionType,
    BeaconCorrectionEscalationType,
    BeaconModelType,
    CorrectionRuleKindType,
    CorrectionRuleUpdate,
    CorrectionType,
    WalkStopOption,
)
from .output import (
    Column,
    Output,
    safe_collar_summary,
    safe_fence_summary,
    safe_map_summary,
    safe_pet_summary,
    safe_profile_summary,
)
from .signalr import HaloSignalRClient, SignalREvent, SignalRHub
from .storage import StateStore

SUPPORT_URL = "https://github.com/sevenlayercookie/halo-collar-api/issues"

# Exit codes. Anything unmapped is a usage error from argparse, which is 2.
EXIT_OK = 0
EXIT_NO_LOGIN = 1
EXIT_ERROR = 2
EXIT_STALE_COMMAND = 3
EXIT_UNKNOWN_OUTCOME = 4
EXIT_UNSAFE = 5
EXIT_INTERRUPTED = 130

# The flat command names this CLI used before it moved to `noun verb`. They are
# gone rather than aliased, so the least the tool can do is say where they went.
RETIRED_COMMANDS = {
    "collars": "halo collar list",
    "configuration": "halo system config",
    "correct": "halo correction send",
    "correction-config": "halo correction config",
    "correction-rules": "halo correction rules",
    "beacons": "halo beacon list",
    "fences": "halo fence list",
    "fence-add": "halo fence add",
    "fence-delete": "halo fence delete",
    "fence-move": "halo fence move",
    "fence-rename": "halo fence rename",
    "find-collar": "halo collar locate",
    "inbox": "halo notification inbox",
    "login": "halo auth login",
    "logout": "halo auth logout",
    "map": "halo account map",
    "notifications": "halo notification list",
    "notifications-read": "halo notification read",
    "parcels": "halo parcel lookup",
    "pets": "halo pet list",
    "pet-add": "halo pet add",
    "pet-colors": "halo pet colors",
    "pet-delete": "halo pet delete",
    "pet-update": "halo pet update",
    "profile": "halo account profile",
    "register-device": "halo device register",
    "server-time": "halo system time",
    "status": "halo auth status",
    "subscription": "halo account subscription",
    "videos": "halo video list",
    "walks": "halo walk list",
}

CONCISE_HELP = f"""\
halo — unofficial client for the Halo Collar API

USAGE
  halo <noun> <verb> [flags]

EXAMPLES
  halo auth login --password        Log in and store the session
  halo pet list                     Every pet, including collarless ones
  halo collar list                  Collars, battery, and connectivity
  halo fence list                   Geofences on the account
  halo live telemetry               Stream unredacted events as JSON Lines
  halo correction send PET_ID Warning
                                    Send one correction, with safety checks

NOUNS
  auth          Log in, log out, inspect the stored session
  account       Profile, subscription, and the combined map view
  pet           List, inspect, create, edit, and delete pets
  collar        List collars and play a collar's locate tone
  firmware      Read installed firmware and server-managed update progress
  fence         List and change containment fences
  beacon        List beacons and their ranges
  walk          List recorded walks
  live          Stream live telemetry and notification events
  notification  Notification history and the in-app inbox
  correction    Send, test, and configure correction feedback
  training      Training course progress
  device        Register this installation with Halo
  parcel        Look up land records the fence editor uses
  video         Onboarding and training video streams
  system        Public configuration and the server clock

Live streams are JSON Lines. Other output is JSON with --json, and a table
otherwise. Data goes to stdout; notices and errors go to stderr.

Run `halo help` for every command, or `halo <noun> --help`.
Report problems at {SUPPORT_URL}
"""


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("halo-collar")
    except Exception:  # pragma: no cover - only when running from a bare tree
        return "unknown"


class _HelpfulParser(argparse.ArgumentParser):
    """An argparse parser that suggests a command instead of only refusing one."""

    def error(self, message: str) -> Any:
        if "invalid choice" in message:
            bad = message.split("'")[1] if "'" in message else ""
            print(f"halo: unknown command {bad!r}.\n", file=sys.stderr)
            for line in _suggestions(bad):
                print(line, file=sys.stderr)
            print(f"\nRun `{self.prog} --help` to see the available commands.", file=sys.stderr)
            raise SystemExit(EXIT_ERROR)
        super().error(message)


def _suggestions(bad: str) -> list[str]:
    """Point at the new spelling of a retired command, or the nearest command."""

    retired = RETIRED_COMMANDS.get(bad)
    if retired:
        return [f"`{bad}` was replaced by `{retired}`.", "", "Did you mean?", f"    {retired}"]
    close = difflib.get_close_matches(bad, sorted(RETIRED_COMMANDS), n=1, cutoff=0.7)
    if close:
        return [
            "Did you mean?",
            f"    {RETIRED_COMMANDS[close[0]]}",
        ]
    return []


def _common_flags() -> argparse.ArgumentParser:
    """Flags every command accepts, before or after the verb.

    They default to SUPPRESS so that a value given at the top level survives the
    subparser, which would otherwise overwrite it with its own default.
    """

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print Halo's data as JSON instead of a table.",
    )
    common.add_argument(
        "--plain",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print tab-separated rows with no alignment, for grep and awk.",
    )
    common.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Suppress notices on stderr. Data and errors still print.",
    )
    common.add_argument(
        "--no-input",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Never prompt. Commands needing confirmation fail unless --yes is passed.",
    )
    common.add_argument(
        "--state-file",
        default=argparse.SUPPRESS,
        help="Override the owner-only credential/counter state path.",
    )
    common.add_argument(
        "--timezone",
        default=argparse.SUPPRESS,
        help="IANA timezone sent in Halo-Client (for example America/Chicago).",
    )
    return common


def _leaf(
    group: Any,
    name: str,
    *,
    help_text: str,
    description: str,
    examples: str,
    handler: Any,
    needs_client: bool = True,
) -> argparse.ArgumentParser:
    """Add one verb, with the help every verb is expected to have."""

    parser = group.add_parser(
        name,
        help=help_text,
        description=description,
        epilog=f"EXAMPLES\n{examples}\n\nReport problems at {SUPPORT_URL}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_flags()],
    )
    parser.set_defaults(handler=handler, needs_client=needs_client)
    return parser


def _group(subparsers: Any, name: str, *, help_text: str, description: str) -> Any:
    """Add one noun and return its verb subparsers."""

    parser = subparsers.add_parser(
        name,
        help=help_text,
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(handler=None, needs_client=False, group_parser=parser)
    return parser.add_subparsers(dest="verb", metavar="<verb>")


def _with_full(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Offer the unredacted payload on a command that summarizes by default.

    Everything Halo returns stays reachable; the flag only keeps coordinates,
    signed URLs, and Wi-Fi details out of terminal output nobody asked for.
    Because the whole payload has no flat shape, --full always prints JSON.
    """

    parser.add_argument(
        "--full",
        action="store_true",
        help="Print Halo's complete response as JSON, including GPS and signed URLs.",
    )
    return parser


def _with_confirmation(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation. Required when stdin is not a terminal.",
    )
    return parser


def _pet_profile_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    """Halo replaces a pet wholesale, so both commands take the same five fields."""

    parser.add_argument("--name", required=required, help="The pet's display name.")
    parser.add_argument(
        "--color-hex", required=required, help="Collar color; one of `halo pet colors`."
    )
    parser.add_argument("--breed", required=required, help="Breed slug, e.g. goldenretriever.")
    parser.add_argument("--birthday", required=required, help="ISO date, for example 2021-04-17.")
    parser.add_argument("--weight-kg", required=required, type=float, help="Weight in kilograms.")


def _correction_feedback_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "kind_type",
        choices=[item.value for item in CorrectionRuleKindType],
        help="Feedback modality: Sound, Vibration, or Shock (static feedback).",
    )
    parser.add_argument(
        "--level",
        type=int,
        help="Sound or shock intensity level from `halo correction config`.",
    )
    parser.add_argument(
        "--sound-id",
        help="Sound UUID from `halo correction config`; required for Sound.",
    )
    parser.add_argument(
        "--vibration-id",
        help="Vibration UUID from `halo correction config`; required for Vibration.",
    )


def _fence_point_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--point",
        action="append",
        required=True,
        metavar="LAT,LON",
        help="A boundary corner; repeat at least three times, in order.",
    )
    _with_confirmation(parser)


def _points(values: Sequence[str]) -> list[tuple[float, float]]:
    points = []
    for value in values:
        parts = value.split(",")
        if len(parts) != 2:
            raise ValueError(f"Expected a LAT,LON pair but got {value!r}.")
        try:
            points.append((float(parts[0]), float(parts[1])))
        except ValueError as exc:
            raise ValueError(f"Expected a LAT,LON pair but got {value!r}.") from exc
    if len(points) < 3:
        raise ValueError("A fence needs at least three boundary points.")
    return points


def build_parser() -> argparse.ArgumentParser:
    parser = _HelpfulParser(
        prog="halo",
        description="Unofficial client for the Halo Collar API.",
        epilog=f"Run `halo help` for a tour.\nReport problems at {SUPPORT_URL}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_common_flags()],
        add_help=True,
    )
    parser.add_argument("--version", action="version", version=f"halo {_version()}")
    parser.set_defaults(handler=None, needs_client=False, group_parser=None)
    subparsers = parser.add_subparsers(dest="noun", metavar="<noun>")

    _build_auth(subparsers)
    _build_account(subparsers)
    _build_pet(subparsers)
    _build_collar(subparsers)
    _build_firmware(subparsers)
    _build_fence(subparsers)
    _build_beacon(subparsers)
    _build_walk(subparsers)
    _build_live(subparsers)
    _build_notification(subparsers)
    _build_correction(subparsers)
    _build_training(subparsers)
    _build_device(subparsers)
    _build_parcel(subparsers)
    _build_video(subparsers)
    _build_system(subparsers)

    help_parser = subparsers.add_parser(
        "help",
        help="Show help for any noun or verb.",
        description="Show help for any noun or verb, the long way round.",
        epilog="EXAMPLES\n  halo help\n  halo help pet\n  halo help pet add",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    help_parser.add_argument("topic", nargs="*", metavar="<noun> [verb]")
    help_parser.set_defaults(handler=None, needs_client=False, group_parser=None)
    return parser


def _build_auth(subparsers: Any) -> None:
    auth = _group(
        subparsers,
        "auth",
        help_text="Log in, log out, inspect the stored session.",
        description="Manage the stored Halo session. Tokens live in the state file.",
    )
    login = _leaf(
        auth,
        "login",
        help_text="Log in with a password or the hosted browser page.",
        description=(
            "Log in and store the session. Without a mode flag this opens Halo's "
            "hosted login page; your password is never seen by this tool."
        ),
        examples=(
            "  halo auth login --password\n"
            "  halo auth login --platform ios\n"
            "  halo auth login --from-refresh-token"
        ),
        handler=_auth_login,
        needs_client=False,
    )
    mode = login.add_mutually_exclusive_group()
    mode.add_argument(
        "--password",
        dest="password_grant",
        action="store_true",
        help="Prompt for Halo email/password and use the Android password grant.",
    )
    mode.add_argument(
        "--from-refresh-token",
        action="store_true",
        help="Securely prompt for an existing refresh token instead of opening a browser.",
    )
    mode.add_argument(
        "--browser-capture",
        action="store_true",
        help="Read the callback from a saved raw HTTP or HAR file instead of pasting the URL.",
    )
    login.add_argument(
        "--platform",
        choices=sorted(CLIENT_PROFILES),
        help="Mobile OAuth profile for browser or imported refresh-token login.",
    )
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the login URL without attempting to open a browser.",
    )

    _leaf(
        auth,
        "logout",
        help_text="Delete stored tokens and command counters.",
        description="Delete the local session. Halo is not told; the refresh token simply goes.",
        examples="  halo auth logout",
        handler=_auth_logout,
        needs_client=False,
    )
    _leaf(
        auth,
        "status",
        help_text="Show login status without revealing tokens.",
        description="Report whether a usable session is stored, and where it lives.",
        examples="  halo auth status\n  halo auth status --json",
        handler=_auth_status,
        needs_client=False,
    )


def _build_account(subparsers: Any) -> None:
    account = _group(
        subparsers,
        "account",
        help_text="Profile, subscription, and the combined map view.",
        description="Read and manage account-level profile data.",
    )
    _with_full(
        _leaf(
            account,
            "profile",
            help_text="Show the account profile.",
            description=(
                "Show the account profile. Summarized by default because the payload "
                "carries your email addresses, avatar URL, and referral link."
            ),
            examples="  halo account profile\n  halo account profile --full",
            handler=_account_profile,
        )
    )
    _leaf(
        account,
        "subscription",
        help_text="Show plan, limits, and enabled features.",
        description="Show the subscription: access level, collar and fence limits, features.",
        examples="  halo account subscription\n  halo account subscription --json",
        handler=_account_subscription,
    )
    account_map = _with_full(
        _leaf(
            account,
            "map",
            help_text="Fetch pets, fences, and corrections in one call.",
            description=(
                "Fetch the aggregate view the app polls on its home screen: pets with "
                "collars embedded, geofences, and recent corrections. Prefer this over "
                "several calls when polling. The viewport coordinates are optional."
            ),
            examples=(
                "  halo account map\n"
                "  halo account map --latitude 37.4219983 --longitude -122.084\n"
                "  halo account map --full"
            ),
            handler=_account_map,
        )
    )
    account_map.add_argument("--latitude", type=float, help="Viewport centre latitude.")
    account_map.add_argument("--longitude", type=float, help="Viewport centre longitude.")
    account_map.add_argument(
        "--refresh-telemetry",
        action="store_true",
        help="Ask the collars for fresher telemetry before answering.",
    )
    account_map.add_argument(
        "--max-corrections",
        type=int,
        default=20,
        help="How many recent corrections to include (default: 20).",
    )

    update_name = _leaf(
        account,
        "update-name",
        help_text="Update the profile's first and last name.",
        description="Replace the two editable name fields; this is not a generic profile patch.",
        examples="  halo account update-name Taylor Quinn",
        handler=_account_update_name,
    )
    update_name.add_argument("first_name", help="New first name.")
    update_name.add_argument("last_name", help="New last name.")

    avatar_upload = _leaf(
        account,
        "avatar-upload",
        help_text="Upload a profile avatar image.",
        description="Upload an image through Halo's profile icon multipart field.",
        examples="  halo account avatar-upload avatar.jpg --content-type image/jpeg",
        handler=_account_avatar_upload,
    )
    avatar_upload.add_argument("image_file", help="Path to the avatar image.")
    avatar_upload.add_argument(
        "--content-type",
        default="image/png",
        help="Image MIME type (default: image/png).",
    )

    _with_confirmation(
        _leaf(
            account,
            "avatar-delete",
            help_text="Remove the profile avatar.",
            description="Remove the current avatar image from the account profile.",
            examples="  halo account avatar-delete\n  halo account avatar-delete --yes",
            handler=_account_avatar_delete,
        )
    )

    _leaf(
        account,
        "onboarding",
        help_text="Show versioned onboarding progress.",
        description="Show the versioned onboarding progress DTO returned by Halo.",
        examples="  halo account onboarding",
        handler=_account_onboarding,
    )
    onboarding_update = _leaf(
        account,
        "onboarding-update",
        help_text="Save versioned onboarding progress from JSON.",
        description=(
            "Save a PascalCase OnboardingProgressDto file; stale versions must be reloaded."
        ),
        examples="  halo account onboarding-update onboarding.json",
        handler=_account_onboarding_update,
    )
    onboarding_update.add_argument(
        "progress_file",
        help="Path to an OnboardingProgressDto JSON file.",
    )

    _leaf(
        account,
        "questionnaire",
        help_text="Show the saved account questionnaire.",
        description="A successful read is Halo's effective questionnaire completion check.",
        examples="  halo account questionnaire",
        handler=_account_questionnaire,
    )
    questionnaire_save = _leaf(
        account,
        "questionnaire-save",
        help_text="Save a questionnaire DTO from JSON.",
        description="Save a complete PascalCase UserQuestionnaireDto file.",
        examples="  halo account questionnaire-save questionnaire.json",
        handler=_account_questionnaire_save,
    )
    questionnaire_save.add_argument(
        "questionnaire_file",
        help="Path to a UserQuestionnaireDto JSON file.",
    )

    _leaf(
        account,
        "email-check",
        help_text="Check whether an email can be changed to.",
        description="Check target-email eligibility without starting a change.",
        examples="  halo account email-check new@example.com",
        handler=_account_email_check,
    ).add_argument("email", help="Proposed new email address.")

    email_request = _with_confirmation(
        _leaf(
            account,
            "email-request",
            help_text="Start an email-change request.",
            description="Send a confirmation code to a new email address.",
            examples="  halo account email-request new@example.com --yes",
            handler=_account_email_request,
        )
    )
    email_request.add_argument("email", help="New email address that will receive the code.")

    email_confirm = _with_confirmation(
        _leaf(
            account,
            "email-confirm",
            help_text="Confirm a pending email change.",
            description="Complete the pending email change with the emailed code.",
            examples="  halo account email-confirm 123456 --yes",
            handler=_account_email_confirm,
        )
    )
    email_confirm.add_argument("code", help="Confirmation code received at the new address.")

    _leaf(
        account,
        "email-resend",
        help_text="Resend a pending email-change confirmation code.",
        description="Resend the confirmation email for the pending email change.",
        examples="  halo account email-resend",
        handler=_account_email_resend,
    )

    _with_confirmation(
        _leaf(
            account,
            "email-cancel",
            help_text="Cancel or restore a pending email change.",
            description="Cancel or restore the account's pending email-change request.",
            examples="  halo account email-cancel --yes",
            handler=_account_email_cancel,
        )
    )

    _with_confirmation(
        _leaf(
            account,
            "delete",
            help_text="Permanently delete the account (destructive).",
            description="Permanently delete the authenticated Halo account.",
            examples="  halo account delete --yes",
            handler=_account_delete,
        )
    )


def _build_pet(subparsers: Any) -> None:
    pet = _group(
        subparsers,
        "pet",
        help_text="Manage pets and their collar modes.",
        description="Work with pets on the account, with or without collars.",
    )
    _with_full(
        _leaf(
            pet,
            "list",
            help_text="List every pet, including pets with no collar.",
            description=(
                "List every pet on the account. Pets that have never had a collar "
                "assigned appear here but never in `halo collar list`."
            ),
            examples="  halo pet list\n  halo pet list --json\n  halo pet list --plain",
            handler=_pet_list,
        )
    )
    show = _leaf(
        pet,
        "show",
        help_text="Fetch one pet in full.",
        description=(
            "Fetch one pet. This prints the complete object, including live "
            "coordinates, so it is JSON whatever the format flags say."
        ),
        examples="  halo pet show PET_ID\n  halo pet show PET_ID --refresh-telemetry",
        handler=_pet_show,
    )
    show.add_argument("pet_id", help="The pet's UUID, from `halo pet list`.")
    show.add_argument(
        "--refresh-telemetry",
        action="store_true",
        help="Ask the collar for fresher telemetry before answering.",
    )

    add = _leaf(
        pet,
        "add",
        help_text="Create a pet.",
        description="Create a pet. The new pet has no collar until one is bound to it.",
        examples=(
            "  halo pet add --name Scout --color-hex '#FF7A00' \\\n"
            "      --breed goldenretriever --birthday 2021-04-17 --weight-kg 28.5"
        ),
        handler=_pet_add,
    )
    _pet_profile_arguments(add, required=True)

    update = _leaf(
        pet,
        "update",
        help_text="Update a pet; unspecified fields keep their values.",
        description=(
            "Update a pet. Halo replaces the whole profile rather than patching it, "
            "so this reads the pet first and sends back whatever you did not pass."
        ),
        examples="  halo pet update PET_ID --weight-kg 29.2\n  halo pet update PET_ID --name Scout",
        handler=_pet_update,
    )
    update.add_argument("pet_id", help="The pet's UUID, from `halo pet list`.")
    _pet_profile_arguments(update, required=False)

    delete = _with_confirmation(
        _leaf(
            pet,
            "delete",
            help_text="Delete a pet and its history (destructive).",
            description=(
                "Delete a pet and everything Halo keeps under it. Halo returns nothing, "
                "so this cannot be undone from here. You are asked to type the pet's name."
            ),
            examples="  halo pet delete PET_ID\n  halo pet delete PET_ID --yes",
            handler=_pet_delete,
        )
    )
    delete.add_argument("pet_id", help="The pet's UUID, from `halo pet list`.")

    bind_collar = _with_confirmation(
        _leaf(
            pet,
            "bind-collar",
            help_text="Attach an account-bound collar to a pet.",
            description=(
                "Attach a collar already bound to the account. COLLAR_ID is Halo's "
                "server UUID from `halo collar list`, not a serial number or ESN."
            ),
            examples=(
                "  halo pet bind-collar PET_ID COLLAR_ID\n"
                "  halo pet bind-collar PET_ID COLLAR_ID --yes"
            ),
            handler=_pet_bind_collar,
        )
    )
    bind_collar.add_argument("pet_id", help="The pet's UUID, from `halo pet list`.")
    bind_collar.add_argument(
        "collar_id",
        help="The server-issued collar UUID, from `halo collar list`.",
    )

    unbind_collar = _with_confirmation(
        _leaf(
            pet,
            "unbind-collar",
            help_text="Detach a collar from a pet but keep it on the account.",
            description=(
                "Remove the pet-to-collar relationship only. The collar remains "
                "registered to the authenticated Halo account."
            ),
            examples=(
                "  halo pet unbind-collar PET_ID\n"
                "  halo pet unbind-collar PET_ID --yes"
            ),
            handler=_pet_unbind_collar,
        )
    )
    unbind_collar.add_argument("pet_id", help="The pet's UUID, from `halo pet list`.")

    _leaf(
        pet,
        "colors",
        help_text="List assignable collar colors.",
        description="List the collar colors Halo accepts as --color-hex.",
        examples="  halo pet colors\n  halo pet colors --plain",
        handler=_pet_colors,
    )

    fences = _with_confirmation(
        _leaf(
            pet,
            "fences",
            help_text="Turn a pet's containment fences on or off.",
            description=(
                "Set the cloud's desired containment mode. The response also carries "
                "the collar's last reported mode, which may lag behind the desired mode."
            ),
            examples=(
                "  halo pet fences PET_ID off\n"
                "  halo pet fences PET_ID on --yes"
            ),
            handler=_pet_fences,
        )
    )
    fences.add_argument("pet_id", help="The pet's UUID, from `halo pet list`.")
    fences.add_argument("state", choices=("on", "off"), help="Desired containment state.")

    beacons = _with_confirmation(
        _leaf(
            pet,
            "beacons",
            help_text="Turn a pet's beacon assignment on or off.",
            description=(
                "Enable or disable beacon assignment for a pet. Halo handles this "
                "separately from the containment-fence mode."
            ),
            examples=(
                "  halo pet beacons PET_ID off\n"
                "  halo pet beacons PET_ID on --yes"
            ),
            handler=_pet_beacons,
        )
    )
    beacons.add_argument("pet_id", help="The pet's UUID, from `halo pet list`.")
    beacons.add_argument("state", choices=("on", "off"), help="Desired beacon assignment.")


def _build_collar(subparsers: Any) -> None:
    collar = _group(
        subparsers,
        "collar",
        help_text="List, bind, and locate collars.",
        description="Work with collars bound to, or being added to, the account.",
    )
    _with_full(
        _leaf(
            collar,
            "list",
            help_text="List collars, battery, and connectivity.",
            description=(
                "List collars. Summarized by default; the full payload carries Wi-Fi "
                "SSIDs, hardware UUIDs, and telemetry."
            ),
            examples="  halo collar list\n  halo collar list --full",
            handler=_collar_list,
        )
    )
    show = _leaf(
        collar,
        "show",
        help_text="Fetch one collar and its current pet assignment.",
        description=(
            "Fetch one account collar in full. Inspect petInfo to confirm its "
            "current reciprocal pet relationship."
        ),
        examples="  halo collar show COLLAR_ID",
        handler=_collar_show,
    )
    show.add_argument("collar_id", help="The collar's UUID, from `halo collar list`.")

    locate = _leaf(
        collar,
        "locate",
        help_text="Play the collar's locate tone.",
        description=(
            "Play the collar's locate tone. This is audible only — it is not a "
            "correction and carries no feedback."
        ),
        examples="  halo collar locate COLLAR_ID",
        handler=_collar_locate,
    )
    locate.add_argument("collar_id", help="The collar's UUID, from `halo collar list`.")

    check_binding = _leaf(
        collar,
        "check-binding",
        help_text="Check whether a collar can be bound to this account.",
        description=(
            "Ask Halo whether the printed collar serial can be bound to this account. "
            "A successful request may still return result=false with the reason."
        ),
        examples="  halo collar check-binding PRINTED_SERIAL",
        handler=_collar_check_binding,
    )
    check_binding.add_argument(
        "serial_number",
        help="The serial number printed on the physical collar.",
    )

    bind = _with_confirmation(
        _leaf(
            collar,
            "bind",
            help_text="Bind a physical collar to this account.",
            description=(
                "Bind a collar using its printed serial and the encrypted serial read "
                "from the physical collar over Bluetooth. A hardware uuId from an "
                "account response is not known to be a substitute."
            ),
            examples=(
                "  halo collar bind PRINTED_SERIAL ENCRYPTED_SERIAL\n"
                "  halo collar bind PRINTED_SERIAL ENCRYPTED_SERIAL --yes"
            ),
            handler=_collar_bind,
        )
    )
    bind.add_argument(
        "serial_number",
        help="The serial number printed on the physical collar.",
    )
    bind.add_argument(
        "encrypted_serial_number",
        help="The encrypted serial returned by the physical collar over Bluetooth.",
    )

    remove = _with_confirmation(
        _leaf(
            collar,
            "remove",
            help_text="Remove a collar from the authenticated account.",
            description=(
                "Remove the collar-to-account relationship. This is distinct from "
                "`halo pet unbind-collar`; the server's behavior for an assigned "
                "collar has not been directly observed."
            ),
            examples=(
                "  halo collar remove COLLAR_ID\n"
                "  halo collar remove COLLAR_ID --yes"
            ),
            handler=_collar_remove,
        )
    )
    remove.add_argument("collar_id", help="The collar's UUID, from `halo collar list`.")


def _build_firmware(subparsers: Any) -> None:
    firmware = _group(
        subparsers,
        "firmware",
        help_text="Read installed firmware and server-managed update progress.",
        description=(
            "Read firmware snapshots from Halo's collar endpoints. Firmware rollout "
            "is server-managed; no customer-facing start, cancel, or channel-selection "
            "HTTP operation is known."
        ),
    )
    listing = _leaf(
        firmware,
        "list",
        help_text="Show firmware status for every account collar.",
        description=(
            "List installed versions, target versions, and asynchronous update "
            "states. --full preserves the complete firmware metadata."
        ),
        examples="  halo firmware list\n  halo firmware list --full",
        handler=_firmware_list,
    )
    listing.add_argument(
        "--full",
        action="store_true",
        help="Print complete nested firmware and target metadata as JSON.",
    )
    show = _leaf(
        firmware,
        "show",
        help_text="Show one collar's complete firmware status.",
        description=(
            "Read one collar's installed firmware and pending update snapshot. "
            "Failures and paused states appear inside a successful response."
        ),
        examples="  halo firmware show COLLAR_ID",
        handler=_firmware_show,
    )
    show.add_argument("collar_id", help="The collar's UUID, from `halo collar list`.")


def _build_fence(subparsers: Any) -> None:
    fence = _group(
        subparsers,
        "fence",
        help_text="List and change containment fences.",
        description=(
            "Work with geofences. Adding, moving, or deleting one changes where the "
            "collar corrects the dog, once the collar syncs."
        ),
    )
    _with_full(
        _leaf(
            fence,
            "list",
            help_text="List the geofences on the account.",
            description=(
                "List geofences. Halo has no fence-list endpoint, so this reads them "
                "out of the map payload. Summarized by default: the full response "
                "carries the zone polygons, the address, and a signed thumbnail URL."
            ),
            examples="  halo fence list\n  halo fence list --full",
            handler=_fence_list,
        )
    )
    add = _with_full(
        _leaf(
            fence,
            "add",
            help_text="Create a containment fence.",
            description=(
                "Create a containment fence from at least three boundary corners, in "
                "order. You are asked to type the fence name before it is created."
            ),
            examples=(
                "  halo fence add 'Back yard' --point 40.0001,-75.0001 \\\n"
                "      --point 40.0002,-75.00015 --point 40.0003,-75.00005"
            ),
            handler=_fence_add,
        )
    )
    add.add_argument("name", help="A name for the fence.")
    _fence_point_arguments(add)

    rename = _leaf(
        fence,
        "rename",
        help_text="Rename a fence.",
        description="Rename a fence. The boundary is untouched.",
        examples="  halo fence rename FENCE_ID 'Front yard'",
        handler=_fence_rename,
    )
    rename.add_argument("fence_id", help="The fence's UUID, from `halo fence list`.")
    rename.add_argument("name", help="The new name.")

    move = _leaf(
        fence,
        "move",
        help_text="Replace a fence's boundary (destructive).",
        description=(
            "Replace a fence's boundary outright. The old boundary is not returned, "
            "and a dog relying on this fence follows the new one once the collar syncs."
        ),
        examples=(
            "  halo fence move FENCE_ID --point 40.0001,-75.0001 \\\n"
            "      --point 40.0002,-75.00015 --point 40.0004,-75.00005"
        ),
        handler=_fence_move,
    )
    move.add_argument("fence_id", help="The fence's UUID, from `halo fence list`.")
    _fence_point_arguments(move)

    delete = _with_confirmation(
        _leaf(
            fence,
            "delete",
            help_text="Delete a fence (destructive).",
            description=(
                "Delete a containment fence. Halo does not return the deleted boundary, "
                "so re-drawing it is manual, and any dog relying on it loses it."
            ),
            examples="  halo fence delete FENCE_ID\n  halo fence delete FENCE_ID --yes",
            handler=_fence_delete,
        )
    )
    delete.add_argument("fence_id", help="The fence's UUID, from `halo fence list`.")


def _build_beacon(subparsers: Any) -> None:
    beacon = _group(
        subparsers,
        "beacon",
        help_text="List, bind, configure, and remove beacons.",
        description=(
            "Manage account beacons. Per-pet assignment is controlled by "
            "`halo pet beacons`, not by an individual beacon id."
        ),
    )
    _leaf(
        beacon,
        "list",
        help_text="List beacons and the available ranges.",
        description="List beacons on the account, with the range levels Halo offers.",
        examples="  halo beacon list\n  halo beacon list --json",
        handler=_beacon_list,
    )

    check_name = _leaf(
        beacon,
        "check-name",
        help_text="Check whether a beacon name is available.",
        description="Check a proposed name; pass --beacon-id when renaming.",
        examples=(
            "  halo beacon check-name Kitchen\n"
            "  halo beacon check-name Kitchen --beacon-id BEACON_ID"
        ),
        handler=_beacon_check_name,
    )
    check_name.add_argument("name", help="Proposed beacon name.")
    check_name.add_argument("--beacon-id", help="Existing beacon id when renaming.")

    check_binding = _leaf(
        beacon,
        "check-binding",
        help_text="Check whether a beacon serial can bind to this account.",
        description=(
            "Check binding eligibility. A successful HTTP request may return result=false."
        ),
        examples="  halo beacon check-binding BEACON_SERIAL",
        handler=_beacon_check_binding,
    )
    check_binding.add_argument("serial_number", help="Serial printed on the physical beacon.")

    sync = _leaf(
        beacon,
        "sync",
        help_text="Show one beacon's per-pet synchronization state.",
        description=(
            "Read petsSync for one account beacon. completed confirms collar "
            "distribution; pending and skipped are preserved."
        ),
        examples="  halo beacon sync BEACON_ID",
        handler=_beacon_sync,
    )
    sync.add_argument("beacon_id", help="Beacon server id, from `halo beacon list`.")

    add = _with_confirmation(
        _leaf(
            beacon,
            "add",
            help_text="Add or bind a physical beacon.",
            description=(
                "Create the account beacon record using its physical serial and "
                "configuration. Available range pairs come from `halo beacon list`."
            ),
            examples=(
                "  halo beacon add --name Kitchen --serial-number SERIAL "
                "--model-type Usb --action-type KeepAway --should-notify "
                "--range-level 3 --radius-in-decibel -50\n"
                "  halo beacon add --name Kitchen --serial-number SERIAL "
                "--model-type Usb --action-type KeepAway --should-notify --yes"
            ),
            handler=_beacon_add,
        )
    )
    add.add_argument("--name", required=True, help="Beacon display name.")
    add.add_argument("--serial-number", required=True, help="Physical beacon serial.")
    add.add_argument(
        "--model-type",
        required=True,
        choices=tuple(item.value for item in BeaconModelType),
    )
    add.add_argument(
        "--action-type",
        required=True,
        choices=tuple(item.value for item in BeaconActionType),
    )
    add.add_argument(
        "--should-notify",
        action=argparse.BooleanOptionalAction,
        required=True,
        help="Whether Halo should emit beacon notifications.",
    )
    add.add_argument(
        "--is-enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Initial enabled state; omit to send null.",
    )
    add.add_argument("--range-level", type=int, help="Range level from availableRanges.")
    add.add_argument("--radius-in-decibel", type=int, help="Matching range radius.")
    add.add_argument("--transmission-rate-ms", type=int, help="Transmission interval.")
    add.add_argument(
        "--correction-escalation-type",
        choices=tuple(item.value for item in BeaconCorrectionEscalationType),
    )
    add.add_argument("--pet-id", help="Optional initial pet id.")

    update = _with_confirmation(
        _leaf(
            beacon,
            "update",
            help_text="Update supplied settings on one beacon.",
            description=(
                "Send only the supplied fields to the beacon's PUT route. The beacon "
                "server id is required; its serial number is not a substitute."
            ),
            examples=(
                "  halo beacon update BEACON_ID --name 'Back Door'\n"
                "  halo beacon update BEACON_ID --action-type IgnoreFences "
                "--range-level 5 --radius-in-decibel -57 --yes"
            ),
            handler=_beacon_update,
        )
    )
    update.add_argument("beacon_id", help="Beacon server id, from `halo beacon list`.")
    update.add_argument("--name", default=argparse.SUPPRESS)
    update.add_argument(
        "--is-enabled",
        action=argparse.BooleanOptionalAction,
        default=argparse.SUPPRESS,
    )
    update.add_argument(
        "--action-type",
        choices=tuple(item.value for item in BeaconActionType),
        default=argparse.SUPPRESS,
    )
    update.add_argument(
        "--should-notify",
        action=argparse.BooleanOptionalAction,
        default=argparse.SUPPRESS,
    )
    update.add_argument("--range-level", type=int, default=argparse.SUPPRESS)
    update.add_argument("--radius-in-decibel", type=int, default=argparse.SUPPRESS)
    update.add_argument(
        "--model-type",
        choices=tuple(item.value for item in BeaconModelType),
        default=argparse.SUPPRESS,
    )
    update.add_argument("--transmission-rate-ms", type=int, default=argparse.SUPPRESS)
    update.add_argument(
        "--correction-escalation-type",
        choices=tuple(item.value for item in BeaconCorrectionEscalationType),
        default=argparse.SUPPRESS,
    )
    update.add_argument("--pet-id", default=argparse.SUPPRESS)

    delete = _with_confirmation(
        _leaf(
            beacon,
            "delete",
            help_text="Delete or unbind a beacon.",
            description=(
                "Delete the account beacon record. Refresh `halo beacon list` to "
                "confirm removal."
            ),
            examples="  halo beacon delete BEACON_ID\n  halo beacon delete BEACON_ID --yes",
            handler=_beacon_delete,
        )
    )
    delete.add_argument("beacon_id", help="Beacon server id, from `halo beacon list`.")

    telemetry = _leaf(
        beacon,
        "telemetry",
        help_text="Upload locally observed beacon battery telemetry.",
        description=(
            "Read a JSON array of PascalCase SerialNumber and BatteryChargePercent "
            "objects and upload it. This updates cloud telemetry, not beacon settings."
        ),
        examples="  halo beacon telemetry readings.json",
        handler=_beacon_telemetry,
    )
    telemetry.add_argument("readings_file", help="Path to the telemetry JSON array.")


def _build_walk(subparsers: Any) -> None:
    walk = _group(
        subparsers,
        "walk",
        help_text="Read and complete existing walks.",
        description=(
            "Read walk history and operate on a walk already started by a collar or "
            "the Bluetooth mobile flow. HTTP cannot start an ordinary walk."
        ),
    )
    listing = _leaf(
        walk,
        "list",
        help_text="List recorded walks, one page at a time.",
        description="List recorded walks. Halo pages these; ask for a page at a time.",
        examples="  halo walk list\n  halo walk list --page 2 --page-size 10",
        handler=_walk_list,
    )
    listing.add_argument("--page", type=int, default=1, help="Page number (default: 1).")
    listing.add_argument("--page-size", type=int, default=30, help="Rows per page (default: 30).")

    summary = _leaf(
        walk,
        "summary",
        help_text="Fetch one completed walk.",
        description=(
            "Fetch one completed walk summary, including trail-image URLs when their "
            "separate uploads have finished processing."
        ),
        examples="  halo walk summary WALK_ID",
        handler=_walk_summary,
    )
    summary.add_argument("walk_id", help="The walk UUID, from `halo walk list` or telemetry.")

    for verb, paused in (("pause", True), ("resume", False)):
        command = _with_confirmation(
            _leaf(
                walk,
                verb,
                help_text=f"{verb.title()} one collar's existing walk.",
                description=(
                    f"{verb.title()} one collar through Halo's remote walk command. "
                    "A success result is acknowledgement; confirm with fresh telemetry."
                ),
                examples=(
                    f"  halo walk {verb} WALK_ID COLLAR_ID\n"
                    f"  halo walk {verb} WALK_ID COLLAR_ID --yes"
                ),
                handler=_walk_pause if paused else _walk_resume,
            )
        )
        command.add_argument("walk_id", help="The existing walk UUID.")
        command.add_argument("collar_id", help="The participating collar UUID.")

    stop = _with_confirmation(
        _leaf(
            walk,
            "stop",
            help_text="Stop one collar's existing walk.",
            description=(
                "Stop one participating collar. This does not finalize a multi-pet "
                "walk; confirm application when fresh telemetry reports walk=null."
            ),
            examples=(
                "  halo walk stop WALK_ID COLLAR_ID\n"
                "  halo walk stop WALK_ID COLLAR_ID --stop-option ForceSetFencesOn --yes"
            ),
            handler=_walk_stop,
        )
    )
    stop.add_argument("walk_id", help="The existing walk UUID.")
    stop.add_argument("collar_id", help="The participating collar UUID.")
    stop.add_argument(
        "--stop-option",
        choices=tuple(option.value for option in WalkStopOption),
        default=WalkStopOption.DEFAULT.value,
        help="How leash mode should leave fences configured (default: Default).",
    )

    mark_ended = _leaf(
        walk,
        "mark-ended",
        help_text="Submit a completed walk summary from a JSON file.",
        description=(
            "Submit the final aggregate summary. The JSON file uses PascalCase "
            "StartedAt, EndedAt, Pets, User, and LocationName fields and contains "
            "no raw trail points. Image uploads are separate commands."
        ),
        examples="  halo walk mark-ended WALK_ID summary.json",
        handler=_walk_mark_ended,
    )
    mark_ended.add_argument("walk_id", help="The completed walk UUID.")
    mark_ended.add_argument("summary_file", help="Path to the PascalCase summary JSON file.")

    thumbnail = _leaf(
        walk,
        "upload-thumbnail",
        help_text="Upload the rendered overall trail thumbnail.",
        description="Upload image bytes as the multipart trail-thumbnail field.",
        examples=(
            "  halo walk upload-thumbnail WALK_ID overview.png\n"
            "  halo walk upload-thumbnail WALK_ID overview.jpg --content-type image/jpeg"
        ),
        handler=_walk_upload_thumbnail,
    )
    thumbnail.add_argument("walk_id", help="The completed walk UUID.")
    thumbnail.add_argument("image_file", help="Path to the rendered image.")
    thumbnail.add_argument(
        "--content-type",
        default="image/png",
        help="Image MIME type (default: image/png).",
    )

    pet_image = _leaf(
        walk,
        "upload-pet-image",
        help_text="Upload one pet's rendered trail image.",
        description="Upload image bytes as the multipart trail-image field.",
        examples=(
            "  halo walk upload-pet-image WALK_ID PET_ID pet-trail.png\n"
            "  halo walk upload-pet-image WALK_ID PET_ID pet-trail.jpg "
            "--content-type image/jpeg"
        ),
        handler=_walk_upload_pet_image,
    )
    pet_image.add_argument("walk_id", help="The completed walk UUID.")
    pet_image.add_argument("pet_id", help="The pet UUID represented by the image.")
    pet_image.add_argument("image_file", help="Path to the rendered image.")
    pet_image.add_argument(
        "--content-type",
        default="image/png",
        help="Image MIME type (default: image/png).",
    )


def _build_live(subparsers: Any) -> None:
    live = _group(
        subparsers,
        "live",
        help_text="Stream live telemetry and notification events.",
        description=(
            "Open Halo's SignalR connection and print one unredacted JSON event per "
            "line until interrupted. Live telemetry may contain precise location data."
        ),
    )
    telemetry = _leaf(
        live,
        "telemetry",
        help_text="Stream live collar and pet telemetry.",
        description=(
            "Stream TelemetryHub as compact JSON Lines until Ctrl-C. Events are "
            "unredacted and may contain precise pet locations."
        ),
        examples=(
            "  halo live telemetry\n"
            "  halo live telemetry --pet-id PET_ID\n"
            "  halo live telemetry --target HandleIoTTelemetry"
        ),
        handler=_live_telemetry,
    )
    _live_filter_arguments(telemetry)

    notifications = _leaf(
        live,
        "notifications",
        help_text="Stream live notification-hub events.",
        description=(
            "Stream NotificationHub as compact JSON Lines until Ctrl-C. This is "
            "the live socket, not the stored notification history."
        ),
        examples=(
            "  halo live notifications\n"
            "  halo live notifications --target TARGET"
        ),
        handler=_live_notifications,
    )
    _live_filter_arguments(notifications)


def _live_filter_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--pet-id",
        help="Only print events whose common petId field matches this value.",
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Only print this SignalR target; repeat to allow several targets.",
    )


def _build_notification(subparsers: Any) -> None:
    notification = _group(
        subparsers,
        "notification",
        help_text="Notification history and the in-app inbox.",
        description=(
            "Halo keeps two separate feeds: the notification history behind `list`, "
            "and the in-app portal messages behind `inbox`."
        ),
    )
    listing = _with_full(
        _leaf(
            notification,
            "list",
            help_text="List notification history.",
            description=(
                "List the notification history, one page at a time. Halo sends every "
                "column for every notification type and nulls the ones that do not "
                "apply, so the table shows the field each type actually populates; "
                "--full has all twenty-one, plus the paging envelope."
            ),
            examples=(
                "  halo notification list\n"
                "  halo notification list --page 2 --page-size 10\n"
                "  halo notification list --full"
            ),
            handler=_notification_list,
        )
    )
    listing.add_argument("--page", type=int, default=1, help="Page number (default: 1).")
    listing.add_argument("--page-size", type=int, default=30, help="Rows per page (default: 30).")

    read = _leaf(
        notification,
        "read",
        help_text="Mark notifications read.",
        description="Mark one or more notifications read.",
        examples="  halo notification read NOTIFICATION_ID\n  halo notification read ID_1 ID_2",
        handler=_notification_read,
    )
    read.add_argument("notification_id", nargs="+", help="One or more notification UUIDs.")

    _leaf(
        notification,
        "inbox",
        help_text="List in-app portal notifications.",
        description="List the in-app portal messages, a different feed from `list`.",
        examples="  halo notification inbox",
        handler=_notification_inbox,
    )


def _build_correction(subparsers: Any) -> None:
    correction = _group(
        subparsers,
        "correction",
        help_text="Send, test, and configure correction feedback.",
        description=(
            "Direct corrections and collar tests are physical actions. Cloud acceptance "
            "does not prove execution, and this client never retries either one."
        ),
    )
    send = _with_confirmation(
        _leaf(
            correction,
            "send",
            help_text="Send one instant correction, with safety checks.",
            description=(
                "Send exactly one instant correction. The collar must report as "
                "socket-connected unless you skip the check, the command number is "
                "reserved before dispatch, and you are asked to type the pet's name.\n\n"
                "Remove the collar from the dog and configure the lowest safe feedback "
                "level in the official app before testing."
            ),
            examples=(
                "  halo correction send PET_ID GoodBehavior --command-number 13\n"
                "  halo correction send PET_ID ReturnWhistle\n"
                "  halo correction send PET_ID Warning --yes"
            ),
            handler=_correction_send,
        )
    )
    send.add_argument("pet_id", help="The pet's UUID, from `halo pet list`.")
    send.add_argument(
        "correction_type",
        choices=[item.value for item in CorrectionType],
        help="Which correction enum to send.",
    )
    send.add_argument(
        "--command-number",
        type=int,
        help="Required on first use: the next known Halo command number.",
    )
    send.add_argument(
        "--skip-online-check",
        action="store_true",
        help="Send even when Halo does not report a socket-connected collar.",
    )

    rules = _leaf(
        correction,
        "rules",
        help_text="Show a pet's correction rules.",
        description="Show the correction rules Halo has configured for one pet.",
        examples="  halo correction rules PET_ID",
        handler=_correction_rules,
    )
    rules.add_argument("pet_id", help="The pet's UUID, from `halo pet list`.")

    _leaf(
        correction,
        "config",
        help_text="Show the global sound and intensity catalog.",
        description="Show Halo's global catalog of sounds, vibrations, and intensity levels.",
        examples="  halo correction config",
        handler=_correction_config,
    )

    update = _with_confirmation(
        _leaf(
            correction,
            "update",
            help_text="Update one identified persistent correction rule.",
            description=(
                "Update one existing rule by its rule UUID. The rule ID implicitly "
                "selects the pet and escalation slot; omitted rules are left alone. "
                "Use IDs and levels from `halo correction config`."
            ),
            examples=(
                "  halo correction update RULE_ID Sound --level 3 --sound-id SOUND_ID\n"
                "  halo correction update RULE_ID Vibration --vibration-id VIBRATION_ID\n"
                "  halo correction update RULE_ID Shock --level 1"
            ),
            handler=_correction_update,
        )
    )
    update.add_argument("rule_id", help="The existing rule UUID from `halo correction rules`.")
    _correction_feedback_arguments(update)

    test = _with_confirmation(
        _leaf(
            correction,
            "test",
            help_text="Test proposed feedback directly on a collar.",
            description=(
                "Send exactly one proposed sound, vibration, or static-feedback setting "
                "to a pet's collar without saving it as a persistent rule. The collar "
                "must report online unless you explicitly skip that check."
            ),
            examples=(
                "  halo correction test PET_ID Sound --level 1 --sound-id SOUND_ID "
                "--command-number 13\n"
                "  halo correction test PET_ID Vibration --vibration-id VIBRATION_ID\n"
                "  halo correction test PET_ID Shock --level 1"
            ),
            handler=_correction_test,
        )
    )
    test.add_argument("pet_id", help="The pet's UUID, from `halo pet list`.")
    _correction_feedback_arguments(test)
    test.add_argument(
        "--command-number",
        type=int,
        help="Required on first use: the next known Halo command number.",
    )
    test.add_argument(
        "--expires-in",
        type=int,
        default=30,
        metavar="SECONDS",
        help="Expire the direct command after this many seconds (default: 30).",
    )
    test.add_argument(
        "--skip-online-check",
        action="store_true",
        help="Send even when Halo does not report a socket-connected collar.",
    )


def _build_training(subparsers: Any) -> None:
    training = _group(
        subparsers,
        "training",
        help_text="Training course progress.",
        description="Read the training courses and their progress.",
    )
    _leaf(
        training,
        "show",
        help_text="Show training course progress.",
        description="Show the training curriculum and how far through it the account is.",
        examples="  halo training show",
        handler=_training_show,
    )


def _build_device(subparsers: Any) -> None:
    device = _group(
        subparsers,
        "device",
        help_text="Register this installation with Halo.",
        description="Manage how Halo identifies this installation.",
    )
    _leaf(
        device,
        "register",
        help_text="Register this installation and store its MobileId.",
        description=(
            "Register this installation and store the MobileId Halo assigns. Every "
            "correction carries that id; until you register, corrections fall back to "
            "a constant that is almost certainly not yours."
        ),
        examples="  halo device register",
        handler=_device_register,
    )


def _build_parcel(subparsers: Any) -> None:
    parcel = _group(
        subparsers,
        "parcel",
        help_text="Look up land records the fence editor uses.",
        description=(
            "Halo proxies a third-party property database here. Responses contain real "
            "owner names and mailing addresses, including neighbors'."
        ),
    )
    lookup = _leaf(
        parcel,
        "lookup",
        help_text="Look up land-parcel records at a point.",
        description=(
            "Look up public land records at a point, as the fence editor does. The "
            "response is about whoever owns the land, not about you."
        ),
        examples="  halo parcel lookup --latitude 37.4219983 --longitude -122.084",
        handler=_parcel_lookup,
    )
    lookup.add_argument("--latitude", type=float, required=True, help="Latitude of the point.")
    lookup.add_argument("--longitude", type=float, required=True, help="Longitude of the point.")
    lookup.add_argument("--page", type=int, default=1, help="Page number (default: 1).")
    lookup.add_argument(
        "--results-per-page", type=int, default=1, help="Records per page (default: 1)."
    )


def _build_system(subparsers: Any) -> None:
    system = _group(
        subparsers,
        "system",
        help_text="Public configuration and the server clock.",
        description="Read what Halo publishes about itself rather than about your account.",
    )
    _leaf(
        system,
        "config",
        help_text="Fetch the public application configuration.",
        description="Fetch Halo's public application configuration. Needs no login.",
        examples="  halo system config",
        handler=_system_config,
    )
    _leaf(
        system,
        "time",
        help_text="Show Halo's UTC server clock.",
        description="Show Halo's server clock, which corrections use for expiry.",
        examples="  halo system time",
        handler=_system_time,
    )


def _build_video(subparsers: Any) -> None:
    video = _group(
        subparsers,
        "video",
        help_text="List the app's onboarding and training video streams.",
        description=(
            "The videos the apps play. They come out of the public configuration, so "
            "unlike everything else here they are not account data."
        ),
    )
    _with_full(
        _leaf(
            video,
            "list",
            help_text="List the app's video streams.",
            description=(
                "List the onboarding, training, and subscription videos as name and HLS "
                "URL. The streams are unsigned and the configuration needs no login, so "
                "this works logged out."
            ),
            examples="  halo video list\n  halo video list --full\n  halo video list --plain",
            handler=_video_list,
        )
    )


# --- Handlers -------------------------------------------------------------


def _live_telemetry(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    return asyncio.run(_stream_live_events(args, client, out, SignalRHub.TELEMETRY))


def _live_notifications(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    return asyncio.run(_stream_live_events(args, client, out, SignalRHub.NOTIFICATIONS))


async def _stream_live_events(
    args: argparse.Namespace,
    client: HaloClient,
    out: Output,
    hub: SignalRHub,
) -> int:
    targets = set(args.target)
    async with HaloSignalRClient(client, hub=hub) as stream:
        await stream.wait_connected()
        out.note(
            f"Listening to {hub.value}; press Ctrl-C to stop. "
            "Events are unredacted and may contain precise location data."
        )
        async for event in stream:
            if not _live_event_matches(event, pet_id=args.pet_id, targets=targets):
                continue
            payload = {**event.raw, "hub": event.hub.value}
            print(
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                file=out.stdout,
                flush=True,
            )
    return EXIT_OK


def _live_event_matches(
    event: SignalREvent,
    *,
    pet_id: str | None,
    targets: set[str],
) -> bool:
    if pet_id is not None and event.pet_id != pet_id:
        return False
    return not targets or event.target in targets


def _auth_login(args: argparse.Namespace, _: HaloClient | None, out: Output) -> int:
    store = _store(args)
    if args.no_browser and (args.password_grant or args.from_refresh_token):
        raise ValueError("--no-browser applies only to the hosted browser login.")
    if args.password_grant and args.platform not in (None, "android"):
        raise ValueError("The password grant belongs to the Android client.")
    if args.password_grant:
        profile = ANDROID_PROFILE
    elif args.platform:
        profile = CLIENT_PROFILES[args.platform]
    elif args.from_refresh_token:
        stored_client_id = store.auth_profile().get("client_id")
        profile = client_profile(stored_client_id) if stored_client_id else IOS_PROFILE
    else:
        profile = IOS_PROFILE

    secret = _client_secret(profile)
    with HaloOAuth(secret, profile=profile) as oauth:
        if args.password_grant:
            _require_input(args, "Logging in with a password needs an interactive terminal.")
            username = input("Halo account email: ").strip()
            password = getpass.getpass("Halo account password (input hidden; never stored): ")
            try:
                tokens = oauth.password_login(username, password)
            finally:
                password = ""
        elif args.from_refresh_token:
            _require_input(args, "Importing a refresh token needs an interactive terminal.")
            refresh_token = getpass.getpass("Halo refresh token (input hidden): ").strip()
            tokens = oauth.refresh(refresh_token)
        else:
            _require_input(args, "The hosted browser login needs an interactive terminal.")
            flow = oauth.begin_login()
            out.note(
                "\nSign in only on auth.halocollar.com. This tool never receives your password.\n"
            )
            opened = False if args.no_browser else webbrowser.open(flow.url)
            if not opened:
                out.note(f"Open this URL in a browser:\n\n{flow.url}\n")
            out.note(
                "After sign-in, copy the full URL beginning with haloapp://callback.\n"
                "If the official app opens, copy the callback URL from the browser/proxy history."
            )
            if args.browser_capture:
                capture_path = Path(
                    input(
                        "Save/transfer the raw HTTP or HAR capture, then enter its file path: "
                    ).strip()
                )
                try:
                    capture = capture_path.read_text(encoding="utf-8")
                except OSError as exc:
                    raise ValueError(f"Cannot read browser capture at {capture_path}.") from exc
                tokens = oauth.complete_login_from_browser_capture(capture, flow)
                out.note(
                    "Browser capture accepted. It still contains private session data; "
                    "protect or delete it when no longer needed."
                )
            else:
                callback = getpass.getpass("Callback URL (input hidden): ")
                tokens = oauth.complete_login(callback, flow)
    store.save_session(tokens, client_id=profile.client_id, app_version=profile.app_version)
    if _flag(args, "timezone", None):
        store.update_settings(timezone=args.timezone)
    expires = datetime.fromtimestamp(tokens.expires_at, timezone.utc).isoformat()
    out.note(
        f"Login successful with the Halo {profile.name} profile. "
        f"Access token expires at {expires}; refresh is automatic."
    )
    out.note("Next: `halo device register`, then `halo pet list`.")
    return EXIT_OK


def _auth_logout(args: argparse.Namespace, _: HaloClient | None, out: Output) -> int:
    removed = _store(args).clear()
    out.note("Local Halo state deleted." if removed else "No local Halo state was present.")
    return EXIT_OK


def _auth_status(args: argparse.Namespace, _: HaloClient | None, out: Output) -> int:
    store = _store(args)
    try:
        tokens = store.load_tokens()
    except HaloError as exc:
        out.note(str(exc))
        out.note("Run `halo auth login` to sign in.")
        return EXIT_NO_LOGIN
    profile = store.auth_profile()
    status = {
        "state": "expired (will refresh on next request)" if tokens.is_expired else "usable",
        "oauthClient": profile.get("client_id", "unknown"),
        "accessTokenExpiry": datetime.fromtimestamp(tokens.expires_at, timezone.utc).isoformat(),
        "mobileId": store.settings().get("mobile_id") or "unregistered",
        "stateFile": str(store.path),
    }
    out.emit(status, pairs=status)
    return EXIT_OK


def _account_profile(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    profile = client.user_profile()
    if args.full:
        out.emit(profile)
        return EXIT_OK
    summary = safe_profile_summary(profile)
    out.emit(summary, pairs=summary)
    return EXIT_OK


def _account_subscription(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.subscription())
    return EXIT_OK


def _account_map(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    account_map = client.account_map(
        args.latitude,
        args.longitude,
        refresh_telemetry=args.refresh_telemetry,
        max_corrections_count=args.max_corrections,
    )
    out.emit(account_map if args.full else safe_map_summary(account_map))
    return EXIT_OK


def _account_update_name(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    profile = client.update_profile_name(args.first_name, args.last_name)
    out.note("Updated the profile name.")
    if profile is not None:
        out.emit(profile)
    return EXIT_OK


def _account_avatar_upload(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    path, image = _read_image(args.image_file, label="avatar image")
    client.upload_profile_avatar(
        image,
        filename=path.name,
        content_type=args.content_type,
    )
    out.note("Uploaded the profile avatar.")
    return EXIT_OK


def _account_avatar_delete(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if not _confirmed(
        args,
        out,
        warning="\nThis will remove the current profile avatar.",
        prompt="Type DELETE to remove the avatar: ",
        expected="DELETE",
        cancelled="Cancelled; the profile avatar was not removed.",
    ):
        return EXIT_NO_LOGIN
    client.delete_profile_avatar()
    out.note("Removed the profile avatar.")
    return EXIT_OK


def _account_onboarding(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.onboarding_progress())
    return EXIT_OK


def _account_onboarding_update(
    args: argparse.Namespace,
    client: HaloClient,
    out: Output,
) -> int:
    progress = _read_json_object(args.progress_file, "onboarding progress")
    required = ("Version", "Steps", "ProgressState")
    missing = [key for key in required if key not in progress]
    if missing:
        raise ValueError(f"Onboarding progress is missing: {', '.join(missing)}.")
    updated = client.update_onboarding_progress(
        version=progress["Version"],
        steps=progress["Steps"],
        progress_state=progress["ProgressState"],
    )
    out.note("Saved onboarding progress. Reload it before retrying an out-of-date version.")
    out.emit(updated)
    return EXIT_OK


def _account_questionnaire(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.questionnaire())
    return EXIT_OK


def _account_questionnaire_save(
    args: argparse.Namespace,
    client: HaloClient,
    out: Output,
) -> int:
    questionnaire = _read_json_object(args.questionnaire_file, "questionnaire")
    saved = client.save_questionnaire(questionnaire)
    out.note("Saved the questionnaire. A successful questionnaire read confirms completion.")
    if saved is not None:
        out.emit(saved)
    return EXIT_OK


def _account_email_check(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    client.check_user_can_change_email(args.email)
    out.note(f"Halo accepted {args.email} as eligible for an email-change request.")
    return EXIT_OK


def _account_email_request(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if not _confirmed(
        args,
        out,
        warning=f"\nThis will send an email-change code to {args.email}.",
        prompt=f"Type the new email ({args.email}) to request the change: ",
        expected=args.email,
        cancelled="Cancelled; no email-change request was started.",
    ):
        return EXIT_NO_LOGIN
    client.request_email_change(args.email)
    out.note("Requested the email change. Confirm it with the code sent to the new address.")
    return EXIT_OK


def _account_email_confirm(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if not _confirmed(
        args,
        out,
        warning="\nThis will confirm the pending account-email change.",
        prompt="Type CONFIRM to complete the email change: ",
        expected="CONFIRM",
        cancelled="Cancelled; the email change was not confirmed.",
    ):
        return EXIT_NO_LOGIN
    client.confirm_email_change(args.code)
    out.note("Confirmed the email change. Run `halo account profile --full` to verify it.")
    return EXIT_OK


def _account_email_resend(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    client.resend_email_change_confirmation()
    out.note("Resent the pending email-change confirmation message.")
    return EXIT_OK


def _account_email_cancel(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if not _confirmed(
        args,
        out,
        warning="\nThis will cancel or restore the pending account-email change.",
        prompt="Type CANCEL to cancel the pending email change: ",
        expected="CANCEL",
        cancelled="Cancelled; the pending email change was left alone.",
    ):
        return EXIT_NO_LOGIN
    out.emit(client.cancel_email_change())
    return EXIT_OK


def _account_delete(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    profile = client.user_profile()
    email = profile.get("currentEmail") or profile.get("email")
    expected = str(email) if isinstance(email, str) and email else "DELETE"
    if not _confirmed(
        args,
        out,
        warning="\nThis permanently deletes the authenticated Halo account.",
        prompt=f"Type {expected} to permanently delete the account: ",
        expected=expected,
        cancelled="Cancelled; the account was not deleted.",
    ):
        return EXIT_NO_LOGIN
    client.delete_account()
    out.note("Deleted the Halo account. Remove any remaining local credentials manually.")
    return EXIT_OK


PET_COLUMNS = [
    Column("NAME", "name"),
    Column("BREED", "breed"),
    Column("COLLAR", "collar"),
    Column("FENCES", "fencesState"),
    Column("BEACONS", "beaconsState"),
    Column("ID", "id"),
]


def _pet_list(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    pets = client.pets()
    if args.full:
        out.emit(pets)
        return EXIT_OK
    summary = safe_pet_summary(pets)
    rows = [
        {
            **pet,
            "collar": (pet.get("collar") or {}).get("serialNumber") if pet.get("collar") else None,
        }
        for pet in summary
    ]
    out.emit(summary, rows=rows, columns=PET_COLUMNS)
    return EXIT_OK


def _pet_show(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.pet(args.pet_id, refresh_telemetry=args.refresh_telemetry))
    return EXIT_OK


def _pet_add(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    created = client.add_pet(
        name=args.name,
        color_hex=args.color_hex,
        breed=args.breed,
        birthday=args.birthday,
        weight_kg=args.weight_kg,
    )
    out.note(f"Created {args.name}. It has no collar until one is bound to it.")
    out.emit(created)
    return EXIT_OK


def _pet_update(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    """Fill unspecified fields from the stored pet.

    Halo replaces the whole profile rather than patching it, so sending only the
    flag you meant to change would silently blank the rest.
    """

    current = client.pet(args.pet_id)
    fields = {
        "name": args.name if args.name is not None else current.get("name"),
        "color_hex": args.color_hex if args.color_hex is not None else current.get("colorHex"),
        "breed": args.breed if args.breed is not None else current.get("breed"),
        "birthday": args.birthday if args.birthday is not None else current.get("birthday"),
        "weight_kg": args.weight_kg if args.weight_kg is not None else current.get("weightKg"),
    }
    missing = sorted(key for key, value in fields.items() if value in (None, ""))
    if missing:
        raise ValueError(
            f"Halo requires the full pet profile and the stored pet has no "
            f"{', '.join(missing)}; pass the matching flag."
        )
    updated = client.update_pet(args.pet_id, **fields)
    out.note("Updated. The collar's configuration is outdated until it next syncs.")
    out.emit(updated)
    return EXIT_OK


def _pet_delete(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    pet = client.pet(args.pet_id)
    pet_name = str(pet.get("name") or args.pet_id)
    if not _confirmed(
        args,
        out,
        warning=(
            f"\nThis permanently deletes {pet_name} and the history Halo keeps under it. "
            "Halo does not return the deleted pet, so nothing here can undo it."
        ),
        prompt=f"Type the pet name ({pet_name}) to delete it: ",
        expected=pet_name,
        cancelled="Cancelled; the pet was not deleted.",
    ):
        return EXIT_NO_LOGIN
    client.delete_pet(args.pet_id)
    out.note(f"Deleted {pet_name}.")
    return EXIT_OK


def _pet_bind_collar(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if not _confirmed(
        args,
        out,
        warning=(
            f"\nThis will attach account collar {args.collar_id} to pet {args.pet_id}. "
            "The HTTP response does not prove that collar synchronization completed."
        ),
        prompt=f"Type the pet id ({args.pet_id}) to attach the collar: ",
        expected=args.pet_id,
        cancelled="Cancelled; the collar was not attached to the pet.",
    ):
        return EXIT_NO_LOGIN
    client.bind_collar_to_pet(args.pet_id, args.collar_id)
    out.note(
        f"Requested attachment of collar {args.collar_id} to pet {args.pet_id}. "
        f"Confirm with `halo pet show {args.pet_id} --refresh-telemetry` and "
        f"`halo collar show {args.collar_id}`."
    )
    return EXIT_OK


def _pet_unbind_collar(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if not _confirmed(
        args,
        out,
        warning=(
            f"\nThis will detach the current collar from pet {args.pet_id}. "
            "The collar will remain registered to the account."
        ),
        prompt=f"Type the pet id ({args.pet_id}) to detach its collar: ",
        expected=args.pet_id,
        cancelled="Cancelled; the collar was not detached from the pet.",
    ):
        return EXIT_NO_LOGIN
    client.unbind_collar_from_pet(args.pet_id)
    out.note(
        f"Requested collar detachment from pet {args.pet_id}. Confirm that a refreshed "
        "pet has collarInfo=null and that the collar has petInfo=null."
    )
    return EXIT_OK


def _pet_colors(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    colors = client.pet_colors()
    out.emit(
        colors,
        rows=colors,
        columns=[
            Column("NAME", "fallbackColorName"),
            Column("HEX", "colorHex"),
            Column("SLUG", "collarColor"),
            Column("AVAILABLE", "isAvailable"),
        ],
    )
    return EXIT_OK


def _pet_fences(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if not _confirmed(
        args,
        out,
        warning=(
            f"\nThis will turn containment fences {args.state} for pet {args.pet_id}. "
            "The cloud accepting the request does not prove the collar applied it."
        ),
        prompt=f"Type the pet id ({args.pet_id}) to change containment: ",
        expected=args.pet_id,
        cancelled="Cancelled; containment mode was not changed.",
    ):
        return EXIT_NO_LOGIN
    mode = client.set_pet_fences_enabled(args.pet_id, args.state == "on")
    out.note(
        f"Requested containment {args.state} for {args.pet_id}. "
        "Compare desiredMode with telemetry.mode before relying on the change."
    )
    out.emit(mode)
    return EXIT_OK


def _pet_beacons(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if not _confirmed(
        args,
        out,
        warning=f"\nThis will turn beacon assignment {args.state} for pet {args.pet_id}.",
        prompt=f"Type the pet id ({args.pet_id}) to change beacon assignment: ",
        expected=args.pet_id,
        cancelled="Cancelled; beacon assignment was not changed.",
    ):
        return EXIT_NO_LOGIN
    response = client.set_pet_beacons_assigned(args.pet_id, args.state == "on")
    out.note(f"Requested beacon assignment {args.state} for {args.pet_id}.")
    if response is not None:
        out.emit(response)
    return EXIT_OK


def _collar_list(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    collars = client.collars()
    if args.full:
        out.emit(collars)
        return EXIT_OK
    summary = safe_collar_summary(collars)
    rows = [
        {**collar, "pet": (collar.get("pet") or {}).get("name") if collar.get("pet") else None}
        for collar in summary
    ]
    out.emit(
        summary,
        rows=rows,
        columns=[
            Column("SERIAL", "serialNumber"),
            Column("PET", "pet"),
            Column("BATTERY", "batteryChargePercent"),
            Column("ONLINE", "online"),
            Column("SYNC", "configurationSyncStatus"),
            Column("ID", "id"),
        ],
    )
    return EXIT_OK


def _collar_show(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.collar(args.collar_id))
    return EXIT_OK


def _collar_locate(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    client.find_collar(args.collar_id)
    out.note("Halo accepted the locate request. The collar plays its tone if reachable.")
    return EXIT_OK


def _collar_check_binding(
    args: argparse.Namespace,
    client: HaloClient,
    out: Output,
) -> int:
    out.emit(client.check_collar_binding(args.serial_number))
    return EXIT_OK


def _collar_bind(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if not _confirmed(
        args,
        out,
        warning=(
            f"\nThis will bind collar {args.serial_number} to the authenticated Halo account."
        ),
        prompt=f"Type the printed serial ({args.serial_number}) to bind it: ",
        expected=args.serial_number,
        cancelled="Cancelled; the collar was not bound.",
    ):
        return EXIT_NO_LOGIN
    bound = client.bind_collar(args.serial_number, args.encrypted_serial_number)
    out.note(f"Bound collar {args.serial_number}.")
    out.emit(bound)
    return EXIT_OK


def _collar_remove(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if not _confirmed(
        args,
        out,
        warning=(
            f"\nThis will remove collar {args.collar_id} from the authenticated account. "
            "If it is assigned to a pet, the server's cascade behavior is not confirmed; "
            "detach it from the pet first for an explicit two-stage removal."
        ),
        prompt=f"Type the collar id ({args.collar_id}) to remove it: ",
        expected=args.collar_id,
        cancelled="Cancelled; the collar was not removed from the account.",
    ):
        return EXIT_NO_LOGIN
    client.unbind_collar_from_user(args.collar_id)
    out.note(
        f"Requested removal of collar {args.collar_id}. Confirm it is absent from "
        "`halo collar list` and that its former pet has collarInfo=null."
    )
    return EXIT_OK


FIRMWARE_COLUMNS = [
    Column("INSTALLED", "installedVersion"),
    Column("TARGET", "targetVersion"),
    Column("STATUS", "updateStatus"),
    Column("AVAILABLE", "hasFirmwareUpdatesAvailable"),
    Column("LATEST PROD", "latestProduction"),
    Column("LATEST BETA", "latestBeta"),
    Column("SERIAL", "serialNumber"),
    Column("COLLAR ID", "collarId"),
]


def _firmware_rows(statuses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for status in statuses:
        installed = status.get("firmware")
        update = status.get("firmwareUpdate")
        target = update.get("firmware") if isinstance(update, dict) else None
        rows.append(
            {
                "collarId": status.get("collarId"),
                "serialNumber": status.get("serialNumber"),
                "installedVersion": (
                    installed.get("formattedVersion") or installed.get("version")
                    if isinstance(installed, dict)
                    else None
                ),
                "targetVersion": (
                    target.get("formattedVersion") or target.get("version")
                    if isinstance(target, dict)
                    else None
                ),
                "updateStatus": status.get("updateStatus") or "idle",
                "hasFirmwareUpdatesAvailable": status.get(
                    "hasFirmwareUpdatesAvailable"
                ),
                "latestProduction": (
                    installed.get("firmwareLatestProduction")
                    if isinstance(installed, dict)
                    else None
                ),
                "latestBeta": (
                    installed.get("firmwareLatestBeta")
                    if isinstance(installed, dict)
                    else None
                ),
            }
        )
    return rows


def _firmware_list(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    statuses = client.firmware_statuses()
    if args.full:
        out.emit(statuses)
        return EXIT_OK
    out.emit(statuses, rows=_firmware_rows(statuses), columns=FIRMWARE_COLUMNS)
    return EXIT_OK


def _firmware_show(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.firmware_status(args.collar_id))
    return EXIT_OK


FENCE_COLUMNS = [
    Column("NAME", "name"),
    Column("ENABLED", "isEnabled"),
    Column("ZONES", "zones"),
    Column("PETS", "petsSync"),
    Column("ID", "id"),
]


def _fence_rows(fences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **fence,
            "zones": len(fence.get("zones") or []),
            "petsSync": sum(1 for entry in fence.get("petsSync") or [] if entry.get("isAssigned")),
        }
        for fence in fences
    ]


def _fence_list(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    fences = client.geofences()
    if args.full:
        out.emit(fences)
        return EXIT_OK
    summary = safe_fence_summary(fences)
    out.emit(summary, rows=_fence_rows(summary), columns=FENCE_COLUMNS)
    return EXIT_OK


def _fence_add(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    points = _points(args.point)
    if not _confirmed(
        args,
        out,
        warning=(
            f"\nThis creates the fence {args.name} from {len(points)} points. It changes "
            "where the collar corrects the dog once the collar syncs. Preview the boundary "
            "in the official app if you have not verified these coordinates."
        ),
        prompt=f"Type the fence name ({args.name}) to create it: ",
        expected=args.name,
        cancelled="Cancelled; no fence was created.",
    ):
        return EXIT_NO_LOGIN
    created = client.add_geo_fence(args.name, points)
    # Halo nests the new fence under `geoFence`, and echoing it whole would
    # print the signed thumbnail URL that every other command hides.
    fence = created.get("geoFence") if isinstance(created, dict) else None
    out.note(f"Created {args.name}.")
    if args.full or not isinstance(fence, dict):
        out.emit(created)
    else:
        summary = safe_fence_summary([fence])
        out.emit(summary[0], rows=_fence_rows(summary), columns=FENCE_COLUMNS)
    return EXIT_OK


def _fence_rename(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    client.rename_geo_fence(args.fence_id, args.name)
    out.note(f"Fence renamed to {args.name}.")
    return EXIT_OK


def _fence_move(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    points = _points(args.point)
    if not _confirmed(
        args,
        out,
        warning=(
            f"\nThis replaces fence {args.fence_id} with a new {len(points)}-point boundary. "
            "The old boundary is not returned, and a dog relying on this fence for "
            "containment follows the new one once the collar syncs."
        ),
        prompt="Type the fence id to move it: ",
        expected=args.fence_id,
        cancelled="Cancelled; the fence was not moved.",
    ):
        return EXIT_NO_LOGIN
    result = client.update_geo_fence_location(args.fence_id, points)
    out.note("Boundary replaced. It takes effect once the collar syncs.")
    out.emit(result)
    return EXIT_OK


def _fence_delete(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if not _confirmed(
        args,
        out,
        warning=(
            f"\nThis permanently deletes fence {args.fence_id}. Halo does not return the "
            "deleted boundary, so re-drawing it is manual, and any dog relying on it for "
            "containment loses that boundary once the collar syncs."
        ),
        prompt="Type the fence id to delete it: ",
        expected=args.fence_id,
        cancelled="Cancelled; the fence was not deleted.",
    ):
        return EXIT_NO_LOGIN
    client.delete_geo_fence(args.fence_id)
    out.note("Fence deleted.")
    return EXIT_OK


def _beacon_list(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.beacons())
    return EXIT_OK


def _beacon_check_name(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    available = client.beacon_name_is_available(args.name, beacon_id=args.beacon_id)
    result = {"available": available}
    out.emit(result, pairs=result)
    return EXIT_OK


def _beacon_check_binding(
    args: argparse.Namespace,
    client: HaloClient,
    out: Output,
) -> int:
    out.emit(client.check_beacon_binding(args.serial_number))
    return EXIT_OK


def _beacon_sync(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.beacon_pet_sync(args.beacon_id))
    return EXIT_OK


def _beacon_range_from_args(args: argparse.Namespace) -> dict[str, int] | None:
    level = getattr(args, "range_level", None)
    radius = getattr(args, "radius_in_decibel", None)
    if (level is None) != (radius is None):
        raise ValueError("Pass --range-level and --radius-in-decibel together.")
    if level is None:
        return None
    return {"Level": level, "RadiusInDecibel": radius}


def _beacon_add(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if not _confirmed(
        args,
        out,
        warning=(
            f"\nThis will bind beacon serial {args.serial_number} to the authenticated "
            "Halo account."
        ),
        prompt=f"Type the beacon serial ({args.serial_number}) to bind it: ",
        expected=args.serial_number,
        cancelled="Cancelled; the beacon was not added.",
    ):
        return EXIT_NO_LOGIN
    created = client.add_beacon(
        name=args.name,
        serial_number=args.serial_number,
        model_type=args.model_type,
        action_type=args.action_type,
        should_notify=args.should_notify,
        beacon_range=_beacon_range_from_args(args),
        is_enabled=args.is_enabled,
        transmission_rate_milliseconds=args.transmission_rate_ms,
        correction_escalation_type=args.correction_escalation_type,
        pet_id=args.pet_id,
    )
    out.note(f"Added beacon {args.name}. Inspect petsSync to follow collar distribution.")
    out.emit(created)
    return EXIT_OK


def _beacon_update(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    kwargs: dict[str, Any] = {}
    for cli_name, method_name in (
        ("name", "name"),
        ("is_enabled", "is_enabled"),
        ("action_type", "action_type"),
        ("should_notify", "should_notify"),
        ("model_type", "model_type"),
        ("transmission_rate_ms", "transmission_rate_milliseconds"),
        ("correction_escalation_type", "correction_escalation_type"),
        ("pet_id", "pet_id"),
    ):
        if hasattr(args, cli_name):
            kwargs[method_name] = getattr(args, cli_name)
    has_level = hasattr(args, "range_level")
    has_radius = hasattr(args, "radius_in_decibel")
    if has_level != has_radius:
        raise ValueError("Pass --range-level and --radius-in-decibel together.")
    if has_level:
        kwargs["beacon_range"] = {
            "Level": args.range_level,
            "RadiusInDecibel": args.radius_in_decibel,
        }
    if not kwargs:
        raise ValueError("Pass at least one beacon setting to update.")
    if not _confirmed(
        args,
        out,
        warning=f"\nThis will change settings for beacon {args.beacon_id}.",
        prompt=f"Type the beacon id ({args.beacon_id}) to update it: ",
        expected=args.beacon_id,
        cancelled="Cancelled; the beacon was not updated.",
    ):
        return EXIT_NO_LOGIN
    updated = client.update_beacon(args.beacon_id, **kwargs)
    out.note(f"Updated beacon {args.beacon_id}.")
    out.emit(updated)
    return EXIT_OK


def _beacon_delete(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if not _confirmed(
        args,
        out,
        warning=f"\nThis will delete or unbind beacon {args.beacon_id}.",
        prompt=f"Type the beacon id ({args.beacon_id}) to delete it: ",
        expected=args.beacon_id,
        cancelled="Cancelled; the beacon was not deleted.",
    ):
        return EXIT_NO_LOGIN
    client.delete_beacon(args.beacon_id)
    out.note(f"Deleted beacon {args.beacon_id}. Refresh the list to confirm removal.")
    return EXIT_OK


def _beacon_telemetry(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    value = _read_json_value(args.readings_file, "beacon telemetry")
    if isinstance(value, dict):
        value = value.get("BeaconsTelemetry")
    if not isinstance(value, list):
        raise ValueError(
            "Beacon telemetry must be a JSON array or an object containing BeaconsTelemetry."
        )
    client.upload_beacon_telemetry(value)
    out.note("Uploaded beacon battery telemetry to Halo.")
    return EXIT_OK


def _walk_list(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.walks(page=args.page, page_size=args.page_size))
    return EXIT_OK


def _walk_summary(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.walk_summary(args.walk_id))
    return EXIT_OK


def _walk_collar_command_confirmed(
    args: argparse.Namespace,
    out: Output,
    action: str,
) -> bool:
    past_tense = {"pause": "paused", "resume": "resumed", "stop": "stopped"}[action]
    return _confirmed(
        args,
        out,
        warning=(
            f"\nThis will {action} collar {args.collar_id} for walk {args.walk_id}. "
            "Cloud acknowledgement does not prove the collar applied it."
        ),
        prompt=f"Type the collar id ({args.collar_id}) to send the command: ",
        expected=args.collar_id,
        cancelled=f"Cancelled; the walk was not {past_tense}.",
    )


def _walk_set_paused(
    args: argparse.Namespace,
    client: HaloClient,
    out: Output,
    *,
    paused: bool,
) -> int:
    action = "pause" if paused else "resume"
    if not _walk_collar_command_confirmed(args, out, action):
        return EXIT_NO_LOGIN
    response = client.set_walk_paused(args.walk_id, args.collar_id, paused)
    out.note(
        f"Halo acknowledged the {action} request. Confirm telemetry.walk.isPaused "
        "before treating it as applied."
    )
    out.emit(response)
    return EXIT_OK


def _walk_pause(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    return _walk_set_paused(args, client, out, paused=True)


def _walk_resume(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    return _walk_set_paused(args, client, out, paused=False)


def _walk_stop(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if not _walk_collar_command_confirmed(args, out, "stop"):
        return EXIT_NO_LOGIN
    response = client.stop_walk(
        args.walk_id,
        args.collar_id,
        stop_option=args.stop_option,
    )
    out.note(
        "Halo acknowledged the stop request. Confirm fresh collar telemetry reports "
        "walk=null; this does not finalize the overall walk."
    )
    out.emit(response)
    return EXIT_OK


def _read_json_value(path_value: str, description: str) -> Any:
    path = Path(path_value)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Cannot read {description} at {path}.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{description.title()} at {path} is not valid JSON.") from exc


def _read_json_object(path_value: str, description: str) -> dict[str, Any]:
    value = _read_json_value(path_value, description)
    if not isinstance(value, dict):
        raise ValueError(f"{description.title()} must contain one JSON object.")
    return value


def _walk_mark_ended(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    summary = _read_json_object(args.summary_file, "walk summary")
    required = ("StartedAt", "EndedAt", "Pets", "User", "LocationName")
    missing = [key for key in required if key not in summary]
    if missing:
        raise ValueError(f"Walk summary is missing: {', '.join(missing)}.")
    client.mark_walk_ended(
        args.walk_id,
        started_at=summary["StartedAt"],
        ended_at=summary["EndedAt"],
        pets=summary["Pets"],
        user=summary["User"],
        location_name=summary["LocationName"],
    )
    out.note(
        "Submitted the completed walk summary. Trail images are separate uploads "
        "and may become visible later."
    )
    return EXIT_OK


def _read_image(path_value: str, *, label: str = "image") -> tuple[Path, bytes]:
    path = Path(path_value)
    try:
        image = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Cannot read {label} at {path}.") from exc
    if not image:
        raise ValueError(f"{label.capitalize()} at {path} is empty.")
    return path, image


def _walk_upload_thumbnail(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    path, image = _read_image(args.image_file)
    client.upload_walk_trail_thumbnail(
        args.walk_id,
        image,
        filename=path.name,
        content_type=args.content_type,
    )
    out.note("Uploaded the overall trail thumbnail; processing may finish later.")
    return EXIT_OK


def _walk_upload_pet_image(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    path, image = _read_image(args.image_file)
    client.upload_walk_pet_trail_image(
        args.walk_id,
        args.pet_id,
        image,
        filename=path.name,
        content_type=args.content_type,
    )
    out.note("Uploaded the pet trail image; processing may finish later.")
    return EXIT_OK


NOTIFICATION_COLUMNS = [
    Column("WHEN", "when"),
    Column("PET", "pet"),
    Column("TYPE", "type"),
    Column("DETAIL", "detail"),
    Column("STATUS", "status"),
    Column("ID", "id"),
]


def _short_time(value: Any) -> Any:
    """Trim Halo's ISO timestamp to the minute, without pretending to parse it."""

    if isinstance(value, str) and len(value) >= 16 and value[10] == "T":
        return f"{value[:10]} {value[11:16]}"
    return value


def _notification_detail(row: dict[str, Any]) -> Any:
    """The one field this notification's type actually populates.

    Halo sends every column for every type and leaves the irrelevant ones null,
    so a useful table has to pick. Anything unrecognized falls back to the
    title, and `--full` still has all twenty-one fields.
    """

    if row.get("batteryChargePercent") is not None:
        return f"{row['batteryChargePercent']}% battery"
    if row.get("correctionsCount"):
        return f"{row['correctionsCount']} corrections"
    if row.get("notificationZone"):
        return row["notificationZone"]
    if row.get("duration"):
        return row["duration"]
    beacon = row.get("beacon")
    if isinstance(beacon, dict) and beacon.get("name"):
        return beacon["name"]
    return row.get("title") or row.get("body")


def _notification_row(row: dict[str, Any]) -> dict[str, Any]:
    pet = row.get("pet")
    return {
        "when": _short_time(row.get("date")),
        "pet": pet.get("name") if isinstance(pet, dict) else None,
        "type": row.get("type"),
        "detail": _notification_detail(row),
        "status": row.get("status"),
        "id": row.get("id"),
    }


def _page_note(page: dict[str, Any], noun: str) -> str:
    number = page.get("pageNumber")
    total_pages = page.get("totalNumberOfPages")
    total_items = page.get("totalNumberOfItems")
    return f"Page {number} of {total_pages} ({total_items} {noun})."


def _notification_list(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    page = client.notifications(page=args.page, page_size=args.page_size)
    results = page.get("results") if isinstance(page, dict) else None
    if args.full or not isinstance(results, list):
        out.emit(page)
        return EXIT_OK
    out.emit(
        results,
        rows=[_notification_row(row) for row in results],
        columns=NOTIFICATION_COLUMNS,
    )
    # Paging is metadata about the request, not part of the data being piped.
    out.note(_page_note(page, "notifications"))
    return EXIT_OK


def _notification_read(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    client.set_notification_status(args.notification_id)
    out.note(f"Marked {len(args.notification_id)} notification(s) read.")
    return EXIT_OK


def _notification_inbox(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.portal_notifications())
    return EXIT_OK


def _correction_send(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    pet = client.pet(args.pet_id)
    pet_name = str(pet.get("name") or args.pet_id)
    kind = CorrectionType.parse(args.correction_type)
    if not _confirmed(
        args,
        out,
        warning=(
            f"\nThis will send {kind.value} to {pet_name}. Halo accepts this enum, "
            "but its physical effect depends on the collar configuration.\n"
            "For first tests, remove the collar from the dog and use the lowest safe "
            "feedback level."
        ),
        prompt=f"Type the pet name ({pet_name}) to send exactly once: ",
        expected=pet_name,
        cancelled="Cancelled; no correction was sent.",
    ):
        return EXIT_NO_LOGIN
    result = client.send_instant_correction(
        args.pet_id,
        kind,
        command_number=args.command_number,
        require_online=not args.skip_online_check,
    )
    out.note(
        f"Halo accepted {kind.value} for {pet_name}. "
        "Cloud acceptance does not confirm physical execution."
    )
    if result.get("currentCommandNumber") is not None:
        out.note(f"Halo current command number: {result['currentCommandNumber']}")
    out.emit(result)
    return EXIT_OK


def _correction_rules(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.pet_correction_rules(args.pet_id))
    return EXIT_OK


def _correction_config(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.correction_rule_configuration())
    return EXIT_OK


def _correction_update(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    item = _correction_update_item(args)
    if not _confirmed(
        args,
        out,
        warning=(
            f"\nThis will change persistent correction rule {args.rule_id} to "
            f"{CorrectionRuleKindType.parse(args.kind_type).value}. Future collar "
            "behavior can change after configuration synchronization."
        ),
        prompt=f"Type the rule id ({args.rule_id}) to update it: ",
        expected=args.rule_id,
        cancelled="Cancelled; the correction rule was not changed.",
    ):
        return EXIT_NO_LOGIN
    updated = client.update_correction_rules([item])
    out.note(
        f"Halo stored correction rule {args.rule_id}. Verify it with `halo correction "
        "rules PET_ID`, then wait for the collar's configurationSyncStatus to become "
        "uptodate."
    )
    out.emit(updated)
    return EXIT_OK


def _correction_test(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    if args.expires_in < 1:
        raise ValueError("--expires-in must be at least 1 second.")
    item = _correction_update_item(
        argparse.Namespace(
            rule_id="test-only",
            kind_type=args.kind_type,
            level=args.level,
            sound_id=args.sound_id,
            vibration_id=args.vibration_id,
        )
    )
    pet = client.pet(args.pet_id)
    pet_name = str(pet.get("name") or args.pet_id)
    if not _confirmed(
        args,
        out,
        warning=(
            f"\nThis will send one {CorrectionRuleKindType.parse(args.kind_type).value} "
            f"test directly to {pet_name}'s collar. It does not save the rule.\n"
            "Remove the collar from the dog and start with the lowest safe feedback level."
        ),
        prompt=f"Type the pet name ({pet_name}) to test exactly once: ",
        expected=pet_name,
        cancelled="Cancelled; no collar test was sent.",
    ):
        return EXIT_NO_LOGIN
    kind = CorrectionRuleKindType.parse(item.kind_type)
    result = client.test_correction_on_collar(
        args.pet_id,
        kind,
        sound_id=item.sound_id,
        vibration_id=item.vibration_id,
        sound_intensity_level=item.level if kind is CorrectionRuleKindType.SOUND else None,
        shock_intensity_level=item.level if kind is CorrectionRuleKindType.SHOCK else None,
        command_number=args.command_number,
        expiration_seconds=args.expires_in,
        require_online=not args.skip_online_check,
    )
    out.note(
        f"Halo accepted the {kind.value} collar test for {pet_name}. "
        "Cloud acceptance does not confirm physical execution or save a rule."
    )
    if result.get("currentCommandNumber") is not None:
        out.note(f"Halo current command number: {result['currentCommandNumber']}")
    out.emit(result)
    return EXIT_OK


def _correction_update_item(args: argparse.Namespace) -> CorrectionRuleUpdate:
    kind = CorrectionRuleKindType.parse(args.kind_type)
    if args.level is not None and args.level < 1:
        raise ValueError("--level must be at least 1.")
    if kind is CorrectionRuleKindType.SOUND:
        if not args.sound_id:
            raise ValueError("Sound requires --sound-id from `halo correction config`.")
        if args.vibration_id:
            raise ValueError("Sound cannot be combined with --vibration-id.")
    elif kind is CorrectionRuleKindType.VIBRATION:
        if not args.vibration_id:
            raise ValueError("Vibration requires --vibration-id from `halo correction config`.")
        if args.sound_id or args.level is not None:
            raise ValueError("Vibration cannot be combined with --sound-id or --level.")
    else:
        if args.level is None:
            raise ValueError("Shock requires --level from `halo correction config`.")
        if args.sound_id or args.vibration_id:
            raise ValueError("Shock cannot be combined with sound or vibration IDs.")
    return CorrectionRuleUpdate(
        correction_rule_id=args.rule_id,
        kind_type=kind,
        level=args.level,
        sound_id=args.sound_id,
        vibration_id=args.vibration_id,
    )


def _training_show(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.training())
    return EXIT_OK


def _device_register(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    mobile_id = client.register_mobile_device()
    out.note(
        f"Registered this installation as MobileId {mobile_id}. "
        "Corrections now send it instead of the fallback constant."
    )
    out.emit({"mobileId": mobile_id}, pairs={"mobileId": mobile_id})
    return EXIT_OK


def _parcel_lookup(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.note(
        "This returns public land records: real owner names and mailing "
        "addresses for whoever owns the land, including neighbors."
    )
    out.emit(
        client.lookup_parcels(
            args.latitude,
            args.longitude,
            page=args.page,
            results_per_page=args.results_per_page,
        )
    )
    return EXIT_OK


def _system_config(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.configuration())
    return EXIT_OK


def _system_time(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    out.emit(client.server_time().isoformat())
    return EXIT_OK


def _video_list(args: argparse.Namespace, client: HaloClient, out: Output) -> int:
    videos = client.videos()
    if args.full:
        out.emit(videos)
        return EXIT_OK
    index = _video_index(videos)
    out.emit(
        index,
        rows=[{"name": name, "url": url} for name, url in index.items()],
        columns=[Column("NAME", "name"), Column("STREAM", "url")],
    )
    return EXIT_OK


def _video_index(videos: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten to name -> stream URL, qualifying only the names that repeat.

    Nothing is redacted here: these URLs are unsigned and the configuration they
    come from needs no login. `--full` adds thumbnails and the section each one
    was found in.
    """

    counts = Counter(video.get("name") for video in videos)
    index: dict[str, Any] = {}
    for video in videos:
        name = video.get("name") or ""
        section = video.get("section")
        key = f"{section}.{name}" if counts[name] > 1 and section else name
        index[key] = video.get("videoStreamUrl")
    return index


# --- Plumbing -------------------------------------------------------------


def _flag(args: argparse.Namespace, name: str, default: Any) -> Any:
    """Read a common flag, which is absent unless it was actually passed."""

    return getattr(args, name, default)


def _store(args: argparse.Namespace) -> StateStore:
    return StateStore(_flag(args, "state_file", None))


def _client_secret(profile: OAuthClientProfile) -> str:
    secret = resolve_client_secret(profile)
    if not secret:
        raise ValueError(
            f"No {profile.name} client secret is available. Set "
            f"HALO_{profile.name.upper()}_CLIENT_SECRET."
        )
    return secret


def _require_input(args: argparse.Namespace, why: str) -> None:
    if _flag(args, "no_input", False) or not sys.stdin.isatty():
        raise ValueError(f"{why} Re-run it in a terminal without --no-input.")


def _confirmed(
    args: argparse.Namespace,
    out: Output,
    *,
    warning: str,
    prompt: str,
    expected: str,
    cancelled: str,
) -> bool:
    """Ask the user to type an identifier back before something irreversible.

    `--yes` is the scriptable path. Without a terminal there is no way to ask, so
    the command refuses rather than treating silence as consent.
    """

    if getattr(args, "yes", False):
        return True
    if _flag(args, "no_input", False) or not sys.stdin.isatty():
        raise ValueError(
            "This needs confirmation and stdin is not an interactive terminal. "
            "Pass --yes if you are sure."
        )
    out.note(warning)
    if input(prompt).strip() != expected:
        out.note(cancelled)
        return False
    return True


def _print_help(parser: argparse.ArgumentParser, topic: Sequence[str]) -> int:
    """Serve `halo help`, `halo help pet`, and `halo help pet add`."""

    if not topic:
        parser.print_help()
        return EXIT_OK
    actions = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    current = parser
    for name in topic:
        choices = actions[0].choices if actions else {}
        if name not in choices:
            print(f"halo: no help for {' '.join(topic)!r}.\n", file=sys.stderr)
            for line in _suggestions(name):
                print(line, file=sys.stderr)
            print("\nRun `halo help` to see every command.", file=sys.stderr)
            return EXIT_ERROR
        current = choices[name]
        actions = [
            action for action in current._actions if isinstance(action, argparse._SubParsersAction)
        ]
    current.print_help()
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        print(CONCISE_HELP, end="")
        return EXIT_OK

    args = parser.parse_args(argv)
    out = Output(
        as_json=_flag(args, "as_json", False),
        plain=_flag(args, "plain", False),
        quiet=_flag(args, "quiet", False),
    )

    if args.noun == "help":
        return _print_help(parser, args.topic)
    if args.handler is None:
        # A noun with no verb: show that noun's own help rather than an error.
        group_parser = _flag(args, "group_parser", None)
        (group_parser or parser).print_help()
        return EXIT_OK

    try:
        if not args.needs_client:
            return args.handler(args, None, out)
        with HaloClient(store=_store(args), timezone_name=_flag(args, "timezone", None)) as client:
            return args.handler(args, client, out)
    except StaleCommandNumberError as exc:
        print(f"Correction not sent: {exc}", file=sys.stderr)
        return EXIT_STALE_COMMAND
    except CorrectionOutcomeUnknownError as exc:
        print(f"WARNING: {exc}", file=sys.stderr)
        return EXIT_UNKNOWN_OUTCOME
    except UnsafeCorrectionError as exc:
        print(f"Safety check stopped the correction: {exc}", file=sys.stderr)
        return EXIT_UNSAFE
    except (HaloError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return EXIT_INTERRUPTED
    except EOFError:
        # A confirmation prompt that cannot be answered must never be treated as
        # consent, so closed stdin cancels rather than proceeding.
        print("Cancelled: confirmation could not be read from input.", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
