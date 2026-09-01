from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telethon import errors, functions, types, utils

from telegram_poll_cleanup import (
    EMPTY_EXCLUSIONS,
    CandidateDecision,
    CleanupError,
    Exclusions,
    append_private_jsonl,
    create_client,
    eligibility_reason,
    fetch_all_participants,
    fetch_voter_ids,
    friendly_rpc_error,
    load_config,
    load_exclusions,
    load_export_document,
    load_poll_context,
    participant_to_record,
    partition_exported_candidates,
    print_candidate_table,
    tighten_session_permissions,
    utc_now_iso,
    validate_export_context,
)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_EXCLUSIONS_PATH = BASE_DIR / "exclusions.txt"
TEMPORARY_BAN_DURATION = timedelta(minutes=10)
SAFE_RETRY_MARGIN = timedelta(minutes=1)
MAX_SERVER_TIME_ROUND_TRIP = timedelta(seconds=30)
MAX_ELAPSED_CLOCK_DIVERGENCE = timedelta(seconds=5)
MINIMUM_FINITE_BAN_REMAINING = timedelta(minutes=5)
PENDING_KICK_STATUSES = {
    "kick_started",
}


@dataclass(frozen=True)
class ServerClock:
    server_time: datetime
    elapsed_at_sync: float
    local_time_at_sync: datetime

    def now(self) -> datetime:
        elapsed = suspend_aware_seconds() - self.elapsed_at_sync
        local_elapsed = (
            datetime.now(timezone.utc) - self.local_time_at_sync
        ).total_seconds()
        divergence = abs(local_elapsed - elapsed)
        if (
            elapsed < 0
            or local_elapsed < 0
            or divergence > MAX_ELAPSED_CLOCK_DIVERGENCE.total_seconds()
        ):
            raise CleanupError(
                "The system clock or sleep state changed after the time check. "
                "Removal was cancelled; restart the command."
            )
        return self.server_time + timedelta(seconds=elapsed)


def suspend_aware_seconds() -> float:
    clock_id = getattr(time, "CLOCK_BOOTTIME", None)
    if clock_id is not None:
        return time.clock_gettime(clock_id)
    return time.monotonic()


async def fetch_server_clock(client: Any) -> ServerClock:
    elapsed_before = suspend_aware_seconds()
    config = await client(functions.help.GetConfigRequest())
    elapsed_after = suspend_aware_seconds()
    round_trip = elapsed_after - elapsed_before
    if round_trip > MAX_SERVER_TIME_ROUND_TRIP.total_seconds():
        raise CleanupError(
            "Telegram took more than 30 seconds to respond to the time check. "
            "Removal was cancelled; try again later."
        )

    server_time = getattr(config, "date", None)
    if not isinstance(server_time, datetime):
        raise CleanupError("Telegram did not return a valid server time.")
    if server_time.tzinfo is None:
        server_time = server_time.replace(tzinfo=timezone.utc)
    else:
        server_time = server_time.astimezone(timezone.utc)
    return ServerClock(
        server_time=server_time,
        elapsed_at_sync=elapsed_after,
        local_time_at_sync=datetime.now(timezone.utc),
    )


def ensure_fresh_finite_ban_window(
    clock: ServerClock, temporary_ban_until: datetime
) -> None:
    remaining = temporary_ban_until - clock.now()
    if remaining < MINIMUM_FINITE_BAN_REMAINING:
        raise CleanupError(
            "The temporary ban deadline expired before the request was sent. "
            "Removal was cancelled; restart the command."
        )


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError(
            "value must be a finite, non-negative number"
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recheck a JSON candidate list and remove members from a Telegram "
            "group without a permanent ban. Without --execute, only a dry run is performed."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("non_voters.json"),
        help="JSON produced by list_non_voters.py (default: non_voters.json)",
    )
    parser.add_argument(
        "--exclusions",
        type=Path,
        default=DEFAULT_EXCLUSIONS_PATH,
        help="Protected username/ID file (default: exclusions.txt in the project)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform removals after interactive confirmation",
    )
    parser.add_argument(
        "--batch-size",
        type=positive_int,
        default=10,
        help="Maximum removals per run (default: 10)",
    )
    parser.add_argument(
        "--min-delay",
        type=non_negative_float,
        default=15.0,
        help="Minimum delay between removals in seconds (default: 15)",
    )
    parser.add_argument(
        "--max-delay",
        type=non_negative_float,
        default=30.0,
        help="Maximum delay between removals in seconds (default: 30)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=Path("removal_results.jsonl"),
        help="Execution log (default: removal_results.jsonl)",
    )
    return parser


def ensure_removal_permission(chat: Any, permissions: Any) -> None:
    if bool(getattr(permissions, "is_creator", False)):
        return
    if isinstance(chat, types.Channel):
        if bool(getattr(permissions, "ban_users", False)):
            return
    elif isinstance(chat, types.Chat) and bool(
        getattr(permissions, "is_admin", False)
    ):
        return
    raise CleanupError(
        "The account is not the owner or an administrator with permission to remove members."
    )


def make_log_record(
    *,
    document: Mapping[str, Any],
    candidate: Mapping[str, Any],
    status: str,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "timestamp": utc_now_iso(),
        "account_id": document["exported_by_user_id"],
        "chat_id": document["chat"]["id"],
        "poll_message_id": document["poll"]["message_id"],
        "user": {
            "id": candidate["id"],
            "first_name": candidate.get("first_name"),
            "last_name": candidate.get("last_name"),
            "username": candidate.get("username"),
        },
        "status": status,
        "reason": reason,
    }
    if details:
        record["details"] = dict(details)
    return record


def append_decision_log(
    path: Path, document: Mapping[str, Any], decision: CandidateDecision
) -> None:
    append_private_jsonl(
        path,
        make_log_record(
            document=document,
            candidate=decision.candidate,
            status=decision.status,
            reason=decision.reason,
        ),
    )


def load_pending_kicks(
    path: Path,
    document: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    path = path.expanduser()
    if not path.exists():
        return [], []

    latest_by_user_id: dict[int, dict[str, Any]] = {}
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise CleanupError(
                        f"Malformed log {path}, line {line_number}: {error}"
                    ) from error
                if not isinstance(record, dict):
                    raise CleanupError(
                        f"Invalid log entry in {path}, line {line_number}."
                    )
                if (
                    record.get("chat_id") != document["chat"]["id"]
                    or record.get("poll_message_id")
                    != document["poll"]["message_id"]
                ):
                    continue
                if record.get("account_id") != document["exported_by_user_id"]:
                    raise CleanupError(
                        "The log for this group and poll was created by another account."
                    )
                user = record.get("user")
                user_id = user.get("id") if isinstance(user, dict) else None
                if isinstance(user_id, bool) or not isinstance(user_id, int):
                    raise CleanupError(
                        f"Invalid user.id in the log, line {line_number}."
                    )
                latest_by_user_id[user_id] = record
    except OSError as error:
        raise CleanupError(f"Could not read log {path}: {error}") from error

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    pending: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    for record in latest_by_user_id.values():
        if record.get("status") not in PENDING_KICK_STATUSES:
            continue
        details = record.get("details")
        safe_retry_value = (
            details.get("safe_retry_after") if isinstance(details, dict) else None
        )
        try:
            safe_retry_at = datetime.fromisoformat(safe_retry_value)
        except (TypeError, ValueError):
            pending.append(record)
            continue
        if safe_retry_at.tzinfo is None:
            safe_retry_at = safe_retry_at.replace(tzinfo=timezone.utc)
        if safe_retry_at <= current_time:
            expired.append(record)
        else:
            pending.append(record)
    return pending, expired


async def live_eligibility_reason(
    client: Any,
    chat: Any,
    user: Any,
    *,
    voter_ids: set[int],
    own_user_id: int,
    poll_published_at: datetime,
    exclusions: Exclusions = EMPTY_EXCLUSIONS,
) -> str:
    permissions = await client.get_permissions(chat, user)
    live_participant = permissions.participant
    is_kicked = isinstance(
        live_participant, types.ChannelParticipantBanned
    ) and bool(getattr(live_participant, "left", False))
    if bool(getattr(permissions, "has_left", False)) or is_kicked:
        return "not_a_current_participant"
    if bool(getattr(permissions, "is_admin", False)) or bool(
        getattr(permissions, "is_creator", False)
    ):
        return "admin"
    if isinstance(live_participant, types.ChannelParticipantBanned):
        return "restricted"
    return eligibility_reason(
        participant_to_record(user, participant=live_participant),
        voter_ids=voter_ids,
        own_user_id=own_user_id,
        poll_published_at=poll_published_at,
        exclusions=exclusions,
    )


async def pause_before_next(
    *, index: int, total: int, min_delay: float, max_delay: float
) -> None:
    if index + 1 >= total:
        return
    delay = random.uniform(min_delay, max_delay)
    print(f"Waiting {delay:.1f} seconds.")
    await asyncio.sleep(delay)


async def run(args: argparse.Namespace) -> int:
    if args.batch_size > 1_000:
        raise CleanupError("--batch-size cannot exceed 1000.")
    if args.max_delay < args.min_delay:
        raise CleanupError("--max-delay cannot be less than --min-delay.")

    document = load_export_document(args.input)
    exclusions = load_exclusions(args.exclusions)
    config = load_config(BASE_DIR)
    client = create_client(config)

    try:
        await client.start(
            phone=lambda: input(
                "Please enter your phone number (bot tokens are not supported): "
            )
        )
        tighten_session_permissions(config.session_path)
        me = await client.get_me()
        if me is None:
            raise CleanupError("Could not identify the authorized account.")
        if bool(getattr(me, "bot", False)):
            raise CleanupError("A user account is required; bot tokens are not supported.")

        chat_id = int(document["chat"]["id"])
        message_id = int(document["poll"]["message_id"])
        context = await load_poll_context(
            client,
            chat_id,
            message_id,
            require_closed=True,
        )
        validate_export_context(
            document,
            own_user_id=int(me.id),
            chat_id=utils.get_peer_id(context.chat),
            message_id=int(context.message.id),
            poll_id=int(context.poll.id),
        )

        permissions = await client.get_permissions(context.chat, me)
        ensure_removal_permission(context.chat, permissions)

        server_clock = None
        if args.execute and isinstance(context.chat, types.Channel):
            server_clock = await fetch_server_clock(client)

        pending_kicks, expired_kicks = load_pending_kicks(
            args.log,
            document,
            now=server_clock.now() if server_clock is not None else None,
        )
        if expired_kicks:
            print(
                "The temporary-ban safety period expired for pending operations: "
                f"{len(expired_kicks)}."
            )
            if args.execute:
                for record in expired_kicks:
                    append_private_jsonl(
                        args.log,
                        make_log_record(
                            document=document,
                            candidate=record["user"],
                            status="uncertain_resolved",
                            reason="temporary_ban_window_expired",
                        ),
                    )

        if pending_kicks:
            print_candidate_table([record["user"] for record in pending_kicks])
            print()
            print(
                "A previous run ended with an uncertain result for "
                f"{len(pending_kicks)} operation(s). New removals are temporarily blocked."
            )
            deadlines = [
                record.get("details", {}).get("safe_retry_after")
                for record in pending_kicks
                if isinstance(record.get("details"), dict)
            ]
            deadlines = [value for value in deadlines if isinstance(value, str)]
            if deadlines:
                print(f"Repeat the dry run after: {max(deadlines)}")
            print(
                "The script will not remove the ban with a separate request: "
                "Telegram will automatically expire the restriction at its deadline."
            )
            return 0

        voter_ids = await fetch_voter_ids(client, context.chat, message_id)
        users = await fetch_all_participants(client, context.chat)
        user_by_id = {int(user.id): user for user in users}
        participant_by_id = {
            int(user.id): participant_to_record(user) for user in users
        }
        ready_ids, decisions = partition_exported_candidates(
            document["candidates"],
            current_participants=participant_by_id,
            voter_ids=voter_ids,
            own_user_id=int(me.id),
            poll_published_at=context.message.date,
            exclusions=exclusions,
        )

        candidate_by_id = {
            int(candidate["id"]): candidate for candidate in document["candidates"]
        }
        batch_ids = ready_ids[: args.batch_size]
        batch_candidates = [
            participant_by_id[user_id].as_candidate() for user_id in batch_ids
        ]

        print_candidate_table(batch_candidates)
        print()
        print(f"Group: {getattr(context.chat, 'title', '')} ({chat_id})")
        print(f"Candidates in the original list: {len(document['candidates'])}")
        print(f"Eligible after recheck: {len(ready_ids)}")
        print(f"To be processed in this run: {len(batch_ids)}")
        print(f"Already absent or protected: {len(decisions)}")

        if not args.execute:
            print("Dry run complete: the Telegram group was not changed.")
            print("Run the command again with --execute to remove members.")
            return 0
        if not batch_ids:
            print("There are no eligible members to remove after the recheck.")
            return 0
        if not sys.stdin.isatty():
            raise CleanupError("--execute requires an interactive terminal.")

        expected_confirmation = f"REMOVE {len(batch_ids)}"
        confirmation = input(
            f"Enter {expected_confirmation} to confirm removal: "
        ).strip()
        if confirmation != expected_confirmation:
            raise CleanupError("Confirmation did not match. Removal was cancelled.")

        for decision in decisions:
            append_decision_log(args.log, document, decision)

        removed_count = 0
        for index, user_id in enumerate(batch_ids):
            user = user_by_id[user_id]
            candidate = candidate_by_id[user_id]
            try:
                fresh_reason = await live_eligibility_reason(
                    client,
                    context.chat,
                    user,
                    voter_ids=voter_ids,
                    own_user_id=int(me.id),
                    poll_published_at=context.message.date,
                    exclusions=exclusions,
                )
            except errors.UserNotParticipantError:
                fresh_reason = "not_a_current_participant"

            if fresh_reason != "eligible":
                status = (
                    "already_absent"
                    if fresh_reason == "not_a_current_participant"
                    else "skipped_protected"
                )
                append_private_jsonl(
                    args.log,
                    make_log_record(
                        document=document,
                        candidate=candidate,
                        status=status,
                        reason=f"live_revalidation:{fresh_reason}",
                    ),
                )
                print(f"Skipped {user_id} after recheck: {fresh_reason}.")
                await pause_before_next(
                    index=index,
                    total=len(batch_ids),
                    min_delay=args.min_delay,
                    max_delay=args.max_delay,
                )
                continue

            operation_details: dict[str, Any] = {}
            if isinstance(context.chat, types.Channel):
                if server_clock is None:
                    raise CleanupError("Could not retrieve Telegram server time.")
                operation_clock = await fetch_server_clock(client)
                temporary_ban_until = operation_clock.now() + TEMPORARY_BAN_DURATION
                safe_retry_after = temporary_ban_until + SAFE_RETRY_MARGIN
                operation_details = {
                    "temporary_ban_until": temporary_ban_until.isoformat(),
                    "safe_retry_after": safe_retry_after.isoformat(),
                }
                append_private_jsonl(
                    args.log,
                    make_log_record(
                        document=document,
                        candidate=candidate,
                        status="kick_started",
                        reason="finite_temporary_ban",
                        details=operation_details,
                    ),
                )
                try:
                    ensure_fresh_finite_ban_window(
                        operation_clock, temporary_ban_until
                    )
                except CleanupError as error:
                    append_private_jsonl(
                        args.log,
                        make_log_record(
                            document=document,
                            candidate=candidate,
                            status="failed",
                            reason="stale_temporary_ban_window",
                            details={"message": str(error)},
                        ),
                    )
                    raise
            try:
                if isinstance(context.chat, types.Channel):
                    await client(
                        functions.channels.EditBannedRequest(
                            channel=context.chat,
                            participant=user,
                            banned_rights=types.ChatBannedRights(
                                until_date=temporary_ban_until,
                                view_messages=True,
                            ),
                        )
                    )
                else:
                    await client.kick_participant(context.chat, user)
            except errors.RPCError as error:
                if isinstance(error, errors.FloodWaitError):
                    details = {"wait_seconds": error.seconds}
                    status = "failed"
                    reason = "flood_wait"
                elif isinstance(error, errors.PeerFloodError):
                    details = {}
                    status = "failed"
                    reason = "peer_flood"
                elif isinstance(error, errors.UserNotParticipantError):
                    details = {}
                    status = "already_absent"
                    reason = "not_a_current_participant"
                elif isinstance(error, errors.UserAdminInvalidError):
                    current_permissions = await client.get_permissions(
                        context.chat, me
                    )
                    try:
                        ensure_removal_permission(context.chat, current_permissions)
                    except CleanupError:
                        details = {"message": str(error)}
                        status = "failed"
                        reason = "account_lost_removal_permission"
                    else:
                        details = {}
                        status = "skipped_protected"
                        reason = "telegram_reports_admin"
                elif isinstance(
                    error,
                    (
                        errors.InputUserDeactivatedError,
                        errors.ParticipantIdInvalidError,
                        errors.UserIdInvalidError,
                    ),
                ):
                    details = {"message": str(error)}
                    status = "failed"
                    reason = type(error).__name__
                else:
                    details = {"message": str(error)}
                    status = "failed"
                    reason = type(error).__name__

                append_private_jsonl(
                    args.log,
                    make_log_record(
                        document=document,
                        candidate=candidate,
                        status=status,
                        reason=reason,
                        details=details,
                    ),
                )
                if isinstance(error, (errors.FloodWaitError, errors.PeerFloodError)):
                    raise
                if reason == "account_lost_removal_permission":
                    raise CleanupError(
                        "The account lost permission to remove members; the batch was stopped."
                    ) from error
                if status == "already_absent":
                    print(f"Skipped {user_id}: no longer a group member.")
                elif status == "skipped_protected":
                    print(
                        f"Skipped {user_id}: Telegram identifies this user as an administrator."
                    )
                elif reason in {
                    "InputUserDeactivatedError",
                    "ParticipantIdInvalidError",
                    "UserIdInvalidError",
                }:
                    print(f"Could not remove {user_id}: {reason}.")
                else:
                    raise
            else:
                removed_count += 1
                is_supergroup = isinstance(context.chat, types.Channel)
                append_private_jsonl(
                    args.log,
                    make_log_record(
                        document=document,
                        candidate=candidate,
                        status="removed",
                        reason=(
                            "kick_with_finite_temporary_ban"
                            if is_supergroup
                            else "kick_without_permanent_ban"
                        ),
                        details=operation_details,
                    ),
                )
                message = f"Removed {user_id} ({index + 1}/{len(batch_ids)})."
                if is_supergroup:
                    message += (
                        " The user will be able to rejoin after "
                        f"{operation_details['temporary_ban_until']}."
                    )
                print(message)

            await pause_before_next(
                index=index,
                total=len(batch_ids),
                min_delay=args.min_delay,
                max_delay=args.max_delay,
            )

        print(f"Removed in this run: {removed_count}")
        print(f"Log: {args.log.expanduser().resolve()}")
        if len(ready_ids) > len(batch_ids):
            print(
                f"Remaining candidates: {len(ready_ids) - len(batch_ids)}. "
                "Run another dry run before the next batch."
            )
        return 0
    finally:
        await client.disconnect()
        tighten_session_permissions(config.session_path)


def main() -> int:
    os.umask(0o077)
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except CleanupError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except errors.FloodWaitError as error:
        print(
            "Telegram requested a delay. Execution stopped; do not retry for at least "
            f"{error.seconds} seconds.",
            file=sys.stderr,
        )
        return 3
    except errors.PeerFloodError:
        print(
            "Telegram restricted the account (PeerFlood). Execution stopped; "
            "do not bypass the restriction, and check @SpamBot.",
            file=sys.stderr,
        )
        return 3
    except errors.RPCError as error:
        print(f"Error: {friendly_rpc_error(error)}", file=sys.stderr)
        return 2
    except OSError as error:
        print(
            "System or network error: "
            f"{error}. Before retrying, always run a dry run; the log may contain "
            "a pending operation.",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        print("Operation cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
