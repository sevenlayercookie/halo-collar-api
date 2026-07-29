"""User-friendly command-line interface for the Halo Collar client."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import webbrowser
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="halo",
        description="Unofficial client for observed Halo Collar REST endpoints.",
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
        help="Prompt for Halo email/password and use the observed Android password grant.",
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
    subparsers.add_parser("collars", help="List collars on the account.")

    subparsers.add_parser("pets", help="List every pet, including pets with no collar.")

    pet = subparsers.add_parser("pet", help="Fetch one pet.")
    pet.add_argument("pet_id")
    pet.add_argument("--refresh-telemetry", action="store_true")

    account_map = subparsers.add_parser(
        "map",
        help="Fetch pets, fences, and recent corrections in one call.",
    )
    account_map.add_argument("latitude", type=float)
    account_map.add_argument("longitude", type=float)
    account_map.add_argument("--refresh-telemetry", action="store_true")
    account_map.add_argument("--max-corrections", type=int, default=20)

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
                _print_json(_safe_collar_summary(client.collars()))
            elif args.command == "pets":
                _print_json(_safe_pet_summary(client.pets()))
            elif args.command == "pet":
                _print_json(client.pet(args.pet_id, refresh_telemetry=args.refresh_telemetry))
            elif args.command == "map":
                _print_json(
                    client.account_map(
                        args.latitude,
                        args.longitude,
                        refresh_telemetry=args.refresh_telemetry,
                        max_corrections_count=args.max_corrections,
                    )
                )
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
        raise ValueError("The observed password grant belongs to the Android client.")
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
            "captured API, but its physical effect depends on the collar configuration.\n"
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


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
