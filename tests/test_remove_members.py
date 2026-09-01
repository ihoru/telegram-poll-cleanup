from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import functions, types, utils
from telethon.tl.custom import ParticipantPermissions

from remove_members import (
    ServerClock,
    build_parser,
    ensure_fresh_finite_ban_window,
    ensure_removal_permission,
    fetch_server_clock,
    live_eligibility_reason,
    load_pending_kicks,
    make_log_record,
    non_negative_float,
    run,
)
from telegram_poll_cleanup import CleanupError, Exclusions

POLL_PUBLISHED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def channel() -> types.Channel:
    return types.Channel(
        id=1,
        title="Test supergroup",
        photo=types.ChatPhotoEmpty(),
        date=None,
        megagroup=True,
    )


def basic_group() -> types.Chat:
    return types.Chat(
        id=1,
        title="Test group",
        photo=types.ChatPhotoEmpty(),
        participants_count=3,
        date=None,
        version=1,
    )


class RemovalPermissionTests(unittest.TestCase):
    def test_creator_and_suitable_admin_permissions_are_accepted(self) -> None:
        allowed_cases = (
            (
                channel(),
                SimpleNamespace(is_creator=True, ban_users=False, is_admin=False),
            ),
            (
                channel(),
                SimpleNamespace(is_creator=False, ban_users=True, is_admin=True),
            ),
            (
                basic_group(),
                SimpleNamespace(is_creator=False, ban_users=False, is_admin=True),
            ),
        )

        for chat, permissions in allowed_cases:
            with self.subTest(chat_type=type(chat).__name__, permissions=permissions):
                ensure_removal_permission(chat, permissions)

    def test_insufficient_permissions_are_rejected(self) -> None:
        denied_cases = (
            (
                channel(),
                SimpleNamespace(is_creator=False, ban_users=False, is_admin=True),
            ),
            (
                basic_group(),
                SimpleNamespace(is_creator=False, ban_users=True, is_admin=False),
            ),
        )

        for chat, permissions in denied_cases:
            with self.subTest(
                chat_type=type(chat).__name__, permissions=permissions
            ), self.assertRaises(CleanupError):
                ensure_removal_permission(chat, permissions)


class ArgumentValidationTests(unittest.TestCase):
    def test_input_defaults_to_non_voters_json_and_can_be_overridden(self) -> None:
        self.assertEqual(build_parser().parse_args([]).input, Path("non_voters.json"))
        self.assertEqual(
            build_parser().parse_args(["--input", "custom.json"]).input,
            Path("custom.json"),
        )

    def test_limit_caps_the_batch_and_batch_size_remains_an_alias(self) -> None:
        self.assertEqual(build_parser().parse_args([]).batch_size, 10)
        self.assertEqual(build_parser().parse_args(["--limit", "1"]).batch_size, 1)
        self.assertEqual(
            build_parser().parse_args(["--batch-size", "2"]).batch_size,
            2,
        )

    def test_non_negative_float_accepts_only_finite_non_negative_values(self) -> None:
        self.assertEqual(non_negative_float("0"), 0.0)
        self.assertEqual(non_negative_float("1.5"), 1.5)

        for value in ("-0.1", "nan", "inf", "+inf", "-inf"):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                non_negative_float(value)


class RemovalLogTests(unittest.TestCase):
    def test_make_log_record_copies_identity_context_status_and_details(self) -> None:
        document = {
            "exported_by_user_id": 900,
            "chat": {"id": -100123},
            "poll": {"message_id": 42},
        }
        candidate = {
            "id": 7,
            "first_name": "Ada",
            "last_name": None,
            "username": "ada",
            "ignored": "value",
        }
        details = {"wait_seconds": 30}

        with patch("remove_members.utc_now_iso", return_value="2026-08-01T12:00:00+00:00"):
            record = make_log_record(
                document=document,
                candidate=candidate,
                status="failed",
                reason="flood_wait",
                details=details,
            )

        self.assertEqual(
            record,
            {
                "timestamp": "2026-08-01T12:00:00+00:00",
                "account_id": 900,
                "chat_id": -100123,
                "poll_message_id": 42,
                "user": {
                    "id": 7,
                    "first_name": "Ada",
                    "last_name": None,
                    "username": "ada",
                },
                "status": "failed",
                "reason": "flood_wait",
                "details": {"wait_seconds": 30},
            },
        )
        self.assertIsNot(record["details"], details)

    def test_make_log_record_omits_empty_details(self) -> None:
        with patch("remove_members.utc_now_iso", return_value="now"):
            record = make_log_record(
                document={
                    "exported_by_user_id": 900,
                    "chat": {"id": -100123},
                    "poll": {"message_id": 42},
                },
                candidate={"id": 7},
                status="removed",
                reason="kick_without_permanent_ban",
                details={},
            )

        self.assertNotIn("details", record)
        self.assertEqual(
            record["user"],
            {"id": 7, "first_name": None, "last_name": None, "username": None},
        )


def pending_document() -> dict[str, object]:
    return {
        "exported_by_user_id": 900,
        "chat": {"id": -100123},
        "poll": {"message_id": 42},
    }


def pending_record(
    user_id: int,
    *,
    status: str,
    safe_retry_after: datetime,
) -> dict[str, object]:
    return {
        "timestamp": "2026-08-01T12:00:00+00:00",
        "account_id": 900,
        "chat_id": -100123,
        "poll_message_id": 42,
        "user": {"id": user_id, "first_name": f"User {user_id}"},
        "status": status,
        "reason": "test",
        "details": {"safe_retry_after": safe_retry_after.isoformat()},
    }


class PendingKickTests(unittest.TestCase):
    NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    def load_records(
        self, records: list[dict[str, object]]
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "removal_results.jsonl"
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            return load_pending_kicks(path, pending_document(), now=self.NOW)

    def test_future_kick_started_record_remains_pending(self) -> None:
        record = pending_record(
            1,
            status="kick_started",
            safe_retry_after=self.NOW + timedelta(minutes=5),
        )

        pending, expired = self.load_records([record])

        self.assertEqual([item["user"]["id"] for item in pending], [1])
        self.assertEqual(expired, [])

    def test_later_terminal_record_clears_a_pending_kick(self) -> None:
        kick_started = pending_record(
            1,
            status="kick_started",
            safe_retry_after=self.NOW + timedelta(minutes=5),
        )
        terminal = pending_record(
            1,
            status="removed",
            safe_retry_after=self.NOW + timedelta(minutes=5),
        )

        pending, expired = self.load_records([kick_started, terminal])

        self.assertEqual(pending, [])
        self.assertEqual(expired, [])

    def test_elapsed_fallback_deadline_is_reported_as_expired(self) -> None:
        record = pending_record(
            1,
            status="kick_started",
            safe_retry_after=self.NOW - timedelta(seconds=1),
        )

        pending, expired = self.load_records([record])

        self.assertEqual(pending, [])
        self.assertEqual([item["user"]["id"] for item in expired], [1])


class LiveEligibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_restricted_current_member_is_protected(self) -> None:
        original_participant = types.ChannelParticipant(
            user_id=7,
            date=POLL_PUBLISHED_AT - timedelta(days=1),
        )
        user = types.User(id=7, first_name="Ada")
        user.participant = original_participant
        restricted_participant = types.ChannelParticipantBanned(
            peer=types.PeerUser(user_id=7),
            kicked_by=900,
            date=POLL_PUBLISHED_AT + timedelta(minutes=1),
            banned_rights=types.ChatBannedRights(
                until_date=POLL_PUBLISHED_AT + timedelta(days=1),
                send_messages=True,
            ),
            left=False,
        )
        permissions = ParticipantPermissions(restricted_participant, chat=False)
        client = SimpleNamespace(
            get_permissions=AsyncMock(return_value=permissions)
        )

        reason = await live_eligibility_reason(
            client,
            channel(),
            user,
            voter_ids=set(),
            own_user_id=900,
            poll_published_at=POLL_PUBLISHED_AT,
        )

        self.assertEqual(reason, "restricted")
        self.assertIs(user.participant, original_participant)

    async def test_rejoined_member_uses_live_join_date(self) -> None:
        user = types.User(id=7, first_name="Ada")
        user.participant = types.ChannelParticipant(
            user_id=7,
            date=POLL_PUBLISHED_AT - timedelta(days=1),
        )
        live_participant = types.ChannelParticipant(
            user_id=7,
            date=POLL_PUBLISHED_AT + timedelta(minutes=1),
        )
        permissions = ParticipantPermissions(live_participant, chat=False)
        client = SimpleNamespace(
            get_permissions=AsyncMock(return_value=permissions)
        )

        reason = await live_eligibility_reason(
            client,
            channel(),
            user,
            voter_ids=set(),
            own_user_id=900,
            poll_published_at=POLL_PUBLISHED_AT,
        )

        self.assertEqual(reason, "joined_after_poll")
        self.assertLess(user.participant.date, POLL_PUBLISHED_AT)

    async def test_new_username_exclusion_protects_live_member(self) -> None:
        user = types.User(id=7, first_name="Ada", username="Protected_User")
        live_participant = types.ChannelParticipant(
            user_id=7,
            date=POLL_PUBLISHED_AT - timedelta(days=1),
        )
        permissions = ParticipantPermissions(live_participant, chat=False)
        client = SimpleNamespace(get_permissions=AsyncMock(return_value=permissions))

        reason = await live_eligibility_reason(
            client,
            channel(),
            user,
            voter_ids=set(),
            own_user_id=900,
            poll_published_at=POLL_PUBLISHED_AT,
            exclusions=Exclusions(
                usernames=frozenset({"protected_user"}),
                user_ids=frozenset(),
            ),
        )

        self.assertEqual(reason, "excluded")


class ServerClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_clock_uses_telegram_time_and_suspend_aware_elapsed(
        self,
    ) -> None:
        server_time = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        client = AsyncMock(return_value=SimpleNamespace(date=server_time))

        with patch(
            "remove_members.suspend_aware_seconds",
            side_effect=[100.0, 100.2, 101.0],
        ):
            clock = await fetch_server_clock(client)
            current = clock.now()

        self.assertIsInstance(clock, ServerClock)
        self.assertEqual(current, server_time + timedelta(seconds=0.8))
        request = client.await_args.args[0]
        self.assertIsInstance(request, functions.help.GetConfigRequest)

    async def test_server_clock_rejects_a_slow_time_sync(self) -> None:
        client = AsyncMock(
            return_value=SimpleNamespace(date=POLL_PUBLISHED_AT)
        )

        with (
            patch(
                "remove_members.suspend_aware_seconds",
                side_effect=[100.0, 131.0],
            ),
            self.assertRaisesRegex(CleanupError, "more than 30 seconds"),
        ):
            await fetch_server_clock(client)

    def test_server_clock_rejects_elapsed_clock_divergence(self) -> None:
        clock = ServerClock(
            server_time=POLL_PUBLISHED_AT,
            elapsed_at_sync=100.0,
            local_time_at_sync=datetime.now(timezone.utc),
        )

        with (
            patch("remove_members.suspend_aware_seconds", return_value=106.0),
            self.assertRaisesRegex(CleanupError, "sleep state"),
        ):
            clock.now()

    def test_finite_ban_window_must_still_have_five_minutes(self) -> None:
        local_now = datetime.now(timezone.utc)
        clock = ServerClock(
            server_time=POLL_PUBLISHED_AT,
            elapsed_at_sync=100.0,
            local_time_at_sync=local_now,
        )

        with patch("remove_members.suspend_aware_seconds", return_value=100.0):
            ensure_fresh_finite_ban_window(
                clock, POLL_PUBLISHED_AT + timedelta(minutes=5)
            )
            with self.assertRaisesRegex(CleanupError, "expired"):
                ensure_fresh_finite_ban_window(
                    clock,
                    POLL_PUBLISHED_AT + timedelta(minutes=5) - timedelta(microseconds=1),
                )


class DryRunClient:
    def __init__(self, *, allow_edit_banned: bool = False) -> None:
        self.allow_edit_banned = allow_edit_banned
        self.start = AsyncMock()
        self.get_me = AsyncMock(return_value=SimpleNamespace(id=900, bot=False))
        self.get_permissions = AsyncMock(
            return_value=SimpleNamespace(is_creator=True)
        )
        self.kick_participant = AsyncMock(
            side_effect=AssertionError("dry-run attempted kick_participant")
        )
        self.disconnect = AsyncMock()
        self.raw_requests: list[object] = []

    async def __call__(self, request: object) -> object:
        self.raw_requests.append(request)
        if self.allow_edit_banned and isinstance(
            request, functions.channels.EditBannedRequest
        ):
            return SimpleNamespace()
        raise AssertionError(f"dry-run attempted raw mutation: {request!r}")


class DryRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_dry_run_never_kicks_or_sends_edit_banned(self) -> None:
        chat = channel()
        chat_id = utils.get_peer_id(chat)
        poll = SimpleNamespace(id=700)
        context = SimpleNamespace(
            chat=chat,
            message=SimpleNamespace(
                id=42,
                date=POLL_PUBLISHED_AT,
                media=SimpleNamespace(poll=poll),
            ),
            poll=poll,
        )
        user = types.User(id=7, first_name="Ada", username="ada")
        user.participant = types.ChannelParticipant(
            user_id=7,
            date=POLL_PUBLISHED_AT - timedelta(days=1),
        )
        document = {
            "exported_by_user_id": 900,
            "chat": {"id": chat_id},
            "poll": {"message_id": 42, "poll_id": 700},
            "candidates": [
                {
                    "id": 7,
                    "first_name": "Ada",
                    "last_name": None,
                    "username": "ada",
                }
            ],
        }
        args = argparse.Namespace(
            input=Path("unused.json"),
            exclusions=Path("unused-exclusions.txt"),
            execute=False,
            batch_size=10,
            min_delay=15.0,
            max_delay=30.0,
            log=Path("unused.jsonl"),
        )
        client = DryRunClient()

        with (
            patch("remove_members.load_export_document", return_value=document),
            patch(
                "remove_members.load_config",
                return_value=SimpleNamespace(session_path=Path("unused-session")),
            ),
            patch("remove_members.create_client", return_value=client),
            patch(
                "remove_members.load_poll_context",
                new=AsyncMock(return_value=context),
            ),
            patch("remove_members.load_pending_kicks", return_value=([], [])),
            patch(
                "remove_members.fetch_voter_ids",
                new=AsyncMock(return_value=set()),
            ),
            patch(
                "remove_members.fetch_all_participants",
                new=AsyncMock(return_value=[user]),
            ),
            patch("remove_members.print_candidate_table"),
            patch("remove_members.tighten_session_permissions"),
            patch("builtins.print"),
        ):
            result = await run(args)

        self.assertEqual(result, 0)
        client.kick_participant.assert_not_awaited()
        self.assertFalse(
            any(
                isinstance(request, functions.channels.EditBannedRequest)
                for request in client.raw_requests
            )
        )
        self.assertEqual(client.raw_requests, [])
        client.disconnect.assert_awaited_once_with()

    async def test_execute_supergroup_uses_one_finite_server_timed_ban(self) -> None:
        server_now = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)
        elapsed_at_sync = 100.0
        server_clock = ServerClock(
            server_time=server_now,
            elapsed_at_sync=elapsed_at_sync,
            local_time_at_sync=datetime.now(timezone.utc),
        )
        chat = channel()
        chat_id = utils.get_peer_id(chat)
        poll = SimpleNamespace(id=700)
        context = SimpleNamespace(
            chat=chat,
            message=SimpleNamespace(
                id=42,
                date=POLL_PUBLISHED_AT,
                media=SimpleNamespace(poll=poll),
            ),
            poll=poll,
        )
        user = types.User(id=7, first_name="Ada", username="ada")
        user.participant = types.ChannelParticipant(
            user_id=7,
            date=POLL_PUBLISHED_AT - timedelta(days=1),
        )
        document = {
            "exported_by_user_id": 900,
            "chat": {"id": chat_id},
            "poll": {"message_id": 42, "poll_id": 700},
            "candidates": [
                {
                    "id": 7,
                    "first_name": "Ada",
                    "last_name": None,
                    "username": "ada",
                }
            ],
        }
        args = argparse.Namespace(
            input=Path("unused.json"),
            exclusions=Path("unused-exclusions.txt"),
            execute=True,
            batch_size=10,
            min_delay=15.0,
            max_delay=30.0,
            log=Path("unused.jsonl"),
        )
        client = DryRunClient(allow_edit_banned=True)
        client.get_permissions.side_effect = [
            SimpleNamespace(is_creator=True),
            ParticipantPermissions(user.participant, chat=False),
        ]

        with (
            patch("remove_members.load_export_document", return_value=document),
            patch(
                "remove_members.load_config",
                return_value=SimpleNamespace(session_path=Path("unused-session")),
            ),
            patch("remove_members.create_client", return_value=client),
            patch(
                "remove_members.load_poll_context",
                new=AsyncMock(return_value=context),
            ),
            patch(
                "remove_members.fetch_server_clock",
                new=AsyncMock(return_value=server_clock),
            ) as fetch_clock,
            patch(
                "remove_members.suspend_aware_seconds",
                return_value=elapsed_at_sync,
            ),
            patch("remove_members.load_pending_kicks", return_value=([], [])),
            patch(
                "remove_members.fetch_voter_ids",
                new=AsyncMock(return_value=set()),
            ),
            patch(
                "remove_members.fetch_all_participants",
                new=AsyncMock(return_value=[user]),
            ),
            patch("remove_members.append_private_jsonl") as append_log,
            patch("remove_members.print_candidate_table"),
            patch("remove_members.tighten_session_permissions"),
            patch("remove_members.sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="REMOVE 1") as confirm,
            patch("builtins.print"),
        ):
            result = await run(args)

        self.assertEqual(result, 0)
        confirm.assert_called_once()
        self.assertIn("REMOVE 1", confirm.call_args.args[0])
        self.assertEqual(fetch_clock.await_count, 2)
        self.assertTrue(
            all(call.args == (client,) for call in fetch_clock.await_args_list)
        )
        client.kick_participant.assert_not_awaited()
        edit_banned_requests = [
            request
            for request in client.raw_requests
            if isinstance(request, functions.channels.EditBannedRequest)
        ]
        self.assertEqual(len(edit_banned_requests), 1)
        request = edit_banned_requests[0]
        self.assertIs(request.channel, chat)
        self.assertIs(request.participant, user)
        self.assertIs(request.banned_rights.view_messages, True)
        self.assertEqual(
            request.banned_rights.until_date,
            server_now + timedelta(minutes=10),
        )
        self.assertEqual(client.raw_requests, [request])
        log_records = [call.args[1] for call in append_log.call_args_list]
        self.assertEqual(
            [record["status"] for record in log_records],
            ["kick_started", "removed"],
        )
        self.assertFalse(
            any(record["status"].startswith("unban") for record in log_records)
        )
        client.disconnect.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
