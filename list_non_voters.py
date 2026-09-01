from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from telethon import errors

from telegram_poll_cleanup import (
    CleanupError,
    build_export_document,
    create_client,
    fetch_all_participants,
    fetch_voter_ids,
    friendly_rpc_error,
    load_config,
    load_exclusions,
    load_poll_context,
    participant_to_record,
    print_candidate_table,
    resolve_poll_location,
    select_candidates,
    tighten_session_permissions,
    write_private_json,
)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_EXCLUSIONS_PATH = BASE_DIR / "exclusions.txt"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a list of current group members who did not vote in a "
            "closed, non-anonymous Telegram poll."
        )
    )
    parser.add_argument("--poll-link", help="t.me link to the poll message")
    parser.add_argument(
        "--chat",
        help="Group username (@group) or numeric ID; use with --message-id",
    )
    parser.add_argument(
        "--message-id",
        type=int,
        help="Poll message ID; use with --chat",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("non_voters.json"),
        help="Output file (default: non_voters.json)",
    )
    parser.add_argument(
        "--exclusions",
        type=Path,
        default=DEFAULT_EXCLUSIONS_PATH,
        help="Protected username/ID file (default: exclusions.txt in the project)",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    location = resolve_poll_location(
        poll_link=args.poll_link,
        chat=args.chat,
        message_id=args.message_id,
    )
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

        context = await load_poll_context(
            client,
            location.chat_ref,
            location.message_id,
            require_closed=True,
        )
        voter_ids = await fetch_voter_ids(client, context.chat, context.message.id)
        users = await fetch_all_participants(client, context.chat)
        participants = [participant_to_record(user) for user in users]
        candidates, reasons = select_candidates(
            participants,
            voter_ids=voter_ids,
            own_user_id=int(me.id),
            poll_published_at=context.message.date,
            exclusions=exclusions,
        )

        document = build_export_document(
            own_user_id=int(me.id),
            context=context,
            candidates=candidates,
        )
        write_private_json(args.output, document)

        print_candidate_table(document["candidates"])
        print()
        print(f"Current members retrieved: {len(participants)}")
        print(f"Unique voters found: {len(voter_ids)}")
        print(f"Removal candidates: {len(candidates)}")
        print(
            "Excluded: "
            f"exclusions file={reasons['excluded']}, "
            f"voted={reasons['voted']}, "
            f"owner/administrators={reasons['admin']}, "
            f"current account={reasons['self']}, "
            f"joined later={reasons['joined_after_poll']}, "
            f"unknown join date={reasons['unknown_join_date']}"
        )
        print(f"List saved to: {args.output.expanduser().resolve()}")
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
            "Telegram requested a delay. Do not retry for at least "
            f"{error.seconds} seconds. The list was not generated.",
            file=sys.stderr,
        )
        return 3
    except errors.PeerFloodError:
        print(
            "Telegram restricted the account (PeerFlood). Stop and check @SpamBot.",
            file=sys.stderr,
        )
        return 3
    except errors.RPCError as error:
        print(f"Error: {friendly_rpc_error(error)}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"System or network error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Operation cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
