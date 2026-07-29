"""User-friendly command-line interface for the Halo Collar client."""

from __future__ import annotations

import argparse
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
from .models import CorrectionType
from .storage import StateStore


def _with_full(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Offer the unredacted payload on a command that summarizes by default.

    Everything Halo returns stays reachable; the flag only keeps coordinates,
    signed URLs, and Wi-Fi details out of terminal output nobody asked for.
    """

    parser.add_argument(
        "--full",
        action="store_true",
        help="Print Halo's complete response, including GPS coordinates and signed URLs.",
    )
    return parser


def _pet_profile_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    """Halo replaces a pet wholesale, so both commands take the same five fields."""

    parser.add_argument("--name", required=required)
    parser.add_argument("--color-hex", required=required, help="One of `halo pet-colors`.")
    parser.add_argument("--breed", required=required)
    parser.add_argument("--birthday", required=required, help="ISO date, for example 2021-04-17.")
    parser.add_argument("--weight-kg", required=required, type=float)


def _fence_point_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--point",
        action="append",
        required=True,
        metavar="LAT,LON",
        help="A boundary corner; repeat at least three times, in order.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation.",
    )


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
    parser = argparse.ArgumentParser(
        prog="halo",
        description="Unofficial client for supported Halo Collar REST endpoints.",
    )
    parser.add_argument(
        "--state-file",
        help="Override the owner-only credential/counter state path.",
    )
    parser.add_argument(
        "--timezone",
        help="IANA timezone sent in Halo-Client (for example America/Chicago).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="Log in with a password or hosted browser.")
    mode = login.add_mutually_exclusive_group()
    mode.add_argument(
        "--password",
        dest="password_grant",
        action="store_true",
        help="Prompt for Halo email/password and use the supported Android password grant.",
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

    subparsers.add_parser("logout", help="Delete locally stored tokens and command counters.")
    subparsers.add_parser("status", help="Show local login status without revealing tokens.")
    subparsers.add_parser("configuration", help="Fetch public Halo configuration.")
    _with_full(subparsers.add_parser("collars", help="List collars on the account."))

    _with_full(subparsers.add_parser("pets", help="List every pet, including pets with no collar."))

    _with_full(subparsers.add_parser("fences", help="List the geofences on the account."))
    _with_full(
        subparsers.add_parser(
            "videos",
            help="List the onboarding, training, and subscription video streams.",
        )
    )

    pet = subparsers.add_parser("pet", help="Fetch one pet.")
    pet.add_argument("pet_id")
    pet.add_argument("--refresh-telemetry", action="store_true")

    account_map = _with_full(
        subparsers.add_parser(
            "map",
            help="Fetch pets, fences, and recent corrections in one call.",
        )
    )
    account_map.add_argument("latitude", type=float, nargs="?")
    account_map.add_argument("longitude", type=float, nargs="?")
    account_map.add_argument("--refresh-telemetry", action="store_true")
    account_map.add_argument("--max-corrections", type=int, default=20)

    _with_full(subparsers.add_parser("profile", help="Show the account profile."))
    subparsers.add_parser("beacons", help="List beacons and their available ranges.")
    subparsers.add_parser("subscription", help="Show plan, limits, and enabled features.")
    subparsers.add_parser("inbox", help="List in-app portal notifications.")
    subparsers.add_parser(
        "correction-config",
        help="Show Halo's global sound, vibration, and intensity catalog.",
    )
    subparsers.add_parser("server-time", help="Show Halo's UTC server clock.")
    subparsers.add_parser(
        "register-device",
        help="Register this installation and store the MobileId corrections carry.",
    )

    parcels = subparsers.add_parser(
        "parcels",
        help="Look up land-parcel records at a point (returns third-party names).",
    )
    parcels.add_argument("latitude", type=float)
    parcels.add_argument("longitude", type=float)
    parcels.add_argument("--page", type=int, default=1)
    parcels.add_argument("--results-per-page", type=int, default=1)

    pet_add = subparsers.add_parser("pet-add", help="Create a pet.")
    _pet_profile_arguments(pet_add, required=True)

    pet_update = subparsers.add_parser(
        "pet-update",
        help="Update a pet; unspecified fields keep their current values.",
    )
    pet_update.add_argument("pet_id")
    _pet_profile_arguments(pet_update, required=False)

    pet_delete = subparsers.add_parser("pet-delete", help="Delete a pet (destructive).")
    pet_delete.add_argument("pet_id")
    pet_delete.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation.",
    )

    fence_add = _with_full(
        subparsers.add_parser(
            "fence-add",
            help="Create a containment fence (changes where the collar corrects).",
        )
    )
    fence_add.add_argument("name")
    _fence_point_arguments(fence_add)

    fence_move = subparsers.add_parser(
        "fence-move",
        help="Replace a fence's boundary (changes where the collar corrects).",
    )
    fence_move.add_argument("fence_id")
    _fence_point_arguments(fence_move)

    walks = subparsers.add_parser("walks", help="List recorded walks.")
    walks.add_argument("--page", type=int, default=1)
    walks.add_argument("--page-size", type=int, default=30)

    notifications = subparsers.add_parser("notifications", help="List notification history.")
    notifications.add_argument("--page", type=int, default=1)
    notifications.add_argument("--page-size", type=int, default=30)

    subparsers.add_parser("training", help="Show training course progress.")
    subparsers.add_parser("pet-colors", help="List assignable collar colors.")

    rules = subparsers.add_parser("correction-rules", help="Show a pet's correction rules.")
    rules.add_argument("pet_id")

    mark_read = subparsers.add_parser("notifications-read", help="Mark notifications read.")
    mark_read.add_argument("notification_id", nargs="+")

    rename_fence = subparsers.add_parser("fence-rename", help="Rename a fence.")
    rename_fence.add_argument("fence_id")
    rename_fence.add_argument("name")

    delete_fence = subparsers.add_parser(
        "fence-delete",
        help="Delete a containment fence (destructive).",
    )
    delete_fence.add_argument("fence_id")
    delete_fence.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation.",
    )

    find = subparsers.add_parser(
        "find-collar",
        help="Play the collar's locate tone (audible only, not a correction).",
    )
    find.add_argument("collar_id")

    correction = subparsers.add_parser(
        "correct",
        help="Send one instant correction with safety checks.",
    )
    correction.add_argument("pet_id")
    correction.add_argument(
        "correction_type",
        choices=[item.value for item in CorrectionType],
    )
    correction.add_argument(
        "--command-number",
        type=int,
        help="Required on first use: the next known Halo command number.",
    )
    correction.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive physical-action confirmation.",
    )
    correction.add_argument(
        "--skip-online-check",
        action="store_true",
        help="Send even when Halo does not report a socket-connected collar.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = StateStore(args.state_file)
    try:
        if args.command == "login":
            return _login(args, store)
        if args.command == "logout":
            removed = store.clear()
            print("Local Halo state deleted." if removed else "No local Halo state was present.")
            return 0
        if args.command == "status":
            return _status(store)

        with HaloClient(store=store, timezone_name=args.timezone) as client:
            if args.command == "configuration":
                _print_json(client.configuration())
            elif args.command == "collars":
                collars = client.collars()
                _print_json(collars if args.full else _safe_collar_summary(collars))
            elif args.command == "pets":
                pets = client.pets()
                _print_json(pets if args.full else _safe_pet_summary(pets))
            elif args.command == "fences":
                fences = client.geofences()
                _print_json(fences if args.full else _safe_fence_summary(fences))
            elif args.command == "videos":
                videos = client.videos()
                _print_json(videos if args.full else _video_index(videos))
            elif args.command == "pet":
                _print_json(client.pet(args.pet_id, refresh_telemetry=args.refresh_telemetry))
            elif args.command == "map":
                account_map = client.account_map(
                    args.latitude,
                    args.longitude,
                    refresh_telemetry=args.refresh_telemetry,
                    max_corrections_count=args.max_corrections,
                )
                _print_json(account_map if args.full else _safe_map_summary(account_map))
            elif args.command == "profile":
                profile = client.user_profile()
                _print_json(profile if args.full else _safe_profile_summary(profile))
            elif args.command == "beacons":
                _print_json(client.beacons())
            elif args.command == "subscription":
                _print_json(client.subscription())
            elif args.command == "inbox":
                _print_json(client.portal_notifications())
            elif args.command == "correction-config":
                _print_json(client.correction_rule_configuration())
            elif args.command == "server-time":
                print(client.server_time().isoformat())
            elif args.command == "register-device":
                mobile_id = client.register_mobile_device()
                print(
                    f"Registered this installation as MobileId {mobile_id}. "
                    "Corrections now send it instead of the fallback constant."
                )
            elif args.command == "parcels":
                print(
                    "This returns public land records: real owner names and mailing "
                    "addresses for whoever owns the land, including neighbors.",
                    file=sys.stderr,
                )
                _print_json(
                    client.lookup_parcels(
                        args.latitude,
                        args.longitude,
                        page=args.page,
                        results_per_page=args.results_per_page,
                    )
                )
            elif args.command == "pet-add":
                _print_json(
                    client.add_pet(
                        name=args.name,
                        color_hex=args.color_hex,
                        breed=args.breed,
                        birthday=args.birthday,
                        weight_kg=args.weight_kg,
                    )
                )
            elif args.command == "pet-update":
                return _update_pet(args, client)
            elif args.command == "pet-delete":
                return _delete_pet(args, client)
            elif args.command == "fence-add":
                return _add_fence(args, client)
            elif args.command == "fence-move":
                return _move_fence(args, client)
            elif args.command == "walks":
                _print_json(client.walks(page=args.page, page_size=args.page_size))
            elif args.command == "notifications":
                _print_json(client.notifications(page=args.page, page_size=args.page_size))
            elif args.command == "training":
                _print_json(client.training())
            elif args.command == "pet-colors":
                _print_json(client.pet_colors())
            elif args.command == "correction-rules":
                _print_json(client.pet_correction_rules(args.pet_id))
            elif args.command == "notifications-read":
                client.set_notification_status(args.notification_id)
                print(f"Marked {len(args.notification_id)} notification(s) read.")
            elif args.command == "fence-rename":
                client.rename_geo_fence(args.fence_id, args.name)
                print(f"Fence renamed to {args.name}.")
            elif args.command == "fence-delete":
                return _delete_fence(args, client)
            elif args.command == "find-collar":
                client.find_collar(args.collar_id)
                print("Halo accepted the locate request. The collar plays its tone if reachable.")
            elif args.command == "correct":
                return _correct(args, client)
        return 0
    except StaleCommandNumberError as exc:
        print(f"Correction not sent: {exc}", file=sys.stderr)
        return 3
    except CorrectionOutcomeUnknownError as exc:
        print(f"WARNING: {exc}", file=sys.stderr)
        return 4
    except UnsafeCorrectionError as exc:
        print(f"Safety check stopped the correction: {exc}", file=sys.stderr)
        return 5
    except (HaloError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except EOFError:
        # A confirmation prompt that cannot be answered must never be treated as
        # consent, so closed stdin cancels rather than proceeding.
        print("Cancelled: confirmation could not be read from input.", file=sys.stderr)
        return 130


def _client_secret(profile: OAuthClientProfile) -> str:
    secret = resolve_client_secret(profile)
    if not secret:
        raise ValueError(
            f"No {profile.name} client secret is available. Set "
            f"HALO_{profile.name.upper()}_CLIENT_SECRET."
        )
    return secret


def _login(args: argparse.Namespace, store: StateStore) -> int:
    if args.no_browser and (args.password_grant or args.from_refresh_token):
        raise ValueError("--no-browser applies only to the hosted browser login.")
    if args.password_grant and args.platform not in (None, "android"):
        raise ValueError("The supported password grant belongs to the Android client.")
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
            username = input("Halo account email: ").strip()
            password = getpass.getpass("Halo account password (input hidden; never stored): ")
            try:
                tokens = oauth.password_login(username, password)
            finally:
                password = ""
        elif args.from_refresh_token:
            refresh_token = getpass.getpass("Halo refresh token (input hidden): ").strip()
            tokens = oauth.refresh(refresh_token)
        else:
            flow = oauth.begin_login()
            print(
                "\nSign in only on auth.halocollar.com. This tool never receives your password.\n"
            )
            opened = False if args.no_browser else webbrowser.open(flow.url)
            if not opened:
                print(f"Open this URL in a browser:\n\n{flow.url}\n")
            print(
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
                print(
                    "Browser capture accepted. It still contains private session data; "
                    "protect or delete it when no longer needed."
                )
            else:
                callback = getpass.getpass("Callback URL (input hidden): ")
                tokens = oauth.complete_login(callback, flow)
    store.save_session(
        tokens,
        client_id=profile.client_id,
        app_version=profile.app_version,
    )
    if args.timezone:
        store.update_settings(timezone=args.timezone)
    expires = datetime.fromtimestamp(tokens.expires_at, timezone.utc).isoformat()
    print(
        f"Login successful with the Halo {profile.name} profile. "
        f"Access token expires at {expires}; refresh is automatic."
    )
    return 0


def _status(store: StateStore) -> int:
    try:
        tokens = store.load_tokens()
    except HaloError as exc:
        print(str(exc))
        return 1
    expiry = datetime.fromtimestamp(tokens.expires_at, timezone.utc).isoformat()
    state = "expired (will refresh on next request)" if tokens.is_expired else "usable"
    profile = store.auth_profile()
    client_id = profile.get("client_id", "unknown")
    print(
        f"Stored login: {state}\nOAuth client: {client_id}\n"
        f"Access-token expiry: {expiry}\nState file: {store.path}"
    )
    return 0


def _correct(args: argparse.Namespace, client: HaloClient) -> int:
    pet = client.pet(args.pet_id)
    pet_name = str(pet.get("name") or args.pet_id)
    kind = CorrectionType.parse(args.correction_type)
    if not args.yes:
        print(
            f"\nThis will send {kind.value} to {pet_name}. Halo accepted this enum in the "
            "API, but its physical effect depends on the collar configuration.\n"
            "For first tests, remove the collar from the dog and use the lowest safe "
            "feedback level."
        )
        answer = input(f"Type the pet name ({pet_name}) to send exactly once: ").strip()
        if answer != pet_name:
            print("Cancelled; no correction was sent.")
            return 1
    result = client.send_instant_correction(
        args.pet_id,
        kind,
        command_number=args.command_number,
        require_online=not args.skip_online_check,
    )
    print(
        f"Halo accepted {kind.value} for {pet_name}. "
        "Cloud acceptance does not confirm physical execution."
    )
    if result.get("currentCommandNumber") is not None:
        print(f"Halo current command number: {result['currentCommandNumber']}")
    return 0


def _delete_fence(args: argparse.Namespace, client: HaloClient) -> int:
    if not args.yes:
        print(
            f"\nThis permanently deletes fence {args.fence_id}. Halo does not return the "
            "deleted boundary, so re-drawing it is manual, and any dog relying on it for "
            "containment loses that boundary once the collar syncs."
        )
        if input("Type the fence id to delete it: ").strip() != args.fence_id:
            print("Cancelled; the fence was not deleted.")
            return 1
    client.delete_geo_fence(args.fence_id)
    print("Fence deleted.")
    return 0


def _update_pet(args: argparse.Namespace, client: HaloClient) -> int:
    """Fill unspecified fields from the stored pet.

    Halo replaces the whole profile rather than patching it, so sending only the
    flag you meant to change would silently blank the rest.
    """

    current = client.pet(args.pet_id)
    birthday = args.birthday if args.birthday is not None else current.get("birthday")
    weight = args.weight_kg if args.weight_kg is not None else current.get("weightKg")
    fields = {
        "name": args.name if args.name is not None else current.get("name"),
        "color_hex": args.color_hex if args.color_hex is not None else current.get("colorHex"),
        "breed": args.breed if args.breed is not None else current.get("breed"),
        "birthday": birthday,
        "weight_kg": weight,
    }
    missing = sorted(key for key, value in fields.items() if value in (None, ""))
    if missing:
        raise ValueError(
            f"Halo requires the full pet profile and the stored pet has no "
            f"{', '.join(missing)}; pass the matching flag."
        )
    _print_json(client.update_pet(args.pet_id, **fields))
    return 0


def _delete_pet(args: argparse.Namespace, client: HaloClient) -> int:
    pet = client.pet(args.pet_id)
    pet_name = str(pet.get("name") or args.pet_id)
    if not args.yes:
        print(
            f"\nThis permanently deletes {pet_name} and the history Halo keeps under it. "
            "Halo does not return the deleted pet, so nothing here can undo it."
        )
        if input(f"Type the pet name ({pet_name}) to delete it: ").strip() != pet_name:
            print("Cancelled; the pet was not deleted.")
            return 1
    client.delete_pet(args.pet_id)
    print(f"Deleted {pet_name}.")
    return 0


def _add_fence(args: argparse.Namespace, client: HaloClient) -> int:
    points = _points(args.point)
    if not args.yes:
        print(
            f"\nThis creates the fence {args.name} from {len(points)} points. It changes "
            "where the collar corrects the dog once the collar syncs. Preview the boundary "
            "in the official app if you have not verified these coordinates."
        )
        if input(f"Type the fence name ({args.name}) to create it: ").strip() != args.name:
            print("Cancelled; no fence was created.")
            return 1
    created = client.add_geo_fence(args.name, points)
    # Halo nests the new fence under `geoFence`, and echoing it whole would
    # print the signed thumbnail URL that every other command hides.
    fence = created.get("geoFence") if isinstance(created, dict) else None
    if args.full or not isinstance(fence, dict):
        _print_json(created)
    else:
        _print_json(_safe_fence_summary([fence])[0])
    return 0


def _move_fence(args: argparse.Namespace, client: HaloClient) -> int:
    points = _points(args.point)
    if not args.yes:
        print(
            f"\nThis replaces fence {args.fence_id} with a new {len(points)}-point boundary. "
            "The old boundary is not returned, and a dog relying on this fence for "
            "containment follows the new one once the collar syncs."
        )
        if input("Type the fence id to move it: ").strip() != args.fence_id:
            print("Cancelled; the fence was not moved.")
            return 1
    _print_json(client.update_geo_fence_location(args.fence_id, points))
    return 0


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


def _safe_profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
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


def _safe_collar_summary(collars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Avoid dumping Wi-Fi SSIDs, hardware UUIDs, and full telemetry by default."""

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


def _safe_pet_summary(pets: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def _safe_fence_summary(fences: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def _safe_map_summary(account_map: dict[str, Any]) -> dict[str, Any]:
    """Summarize the map payload with the same redactions the other commands use."""

    pets = account_map.get("pets")
    fences_info = account_map.get("geoFencesInfo")
    fences = fences_info.get("geoFencesToDisplay") if isinstance(fences_info, dict) else None
    corrections = account_map.get("corrections")
    return {
        "pets": _safe_pet_summary(pets) if isinstance(pets, list) else None,
        "fences": _safe_fence_summary(fences) if isinstance(fences, list) else None,
        "geoFencesTotalCount": (
            fences_info.get("geoFencesTotalCount") if isinstance(fences_info, dict) else None
        ),
        # The correction records have has no stable populated shape, so there is
        # no verified shape to redact; --full is the honest way to read them.
        "correctionCount": len(corrections) if isinstance(corrections, list) else None,
    }


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
