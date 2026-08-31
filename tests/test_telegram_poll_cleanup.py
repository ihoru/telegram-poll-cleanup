from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import errors, functions, types

from telegram_poll_cleanup import (
    CleanupError,
    Exclusions,
    ParticipantRecord,
    PollLocation,
    fetch_all_participants,
    friendly_rpc_error,
    load_exclusions,
    load_export_document,
    parse_poll_link,
    partition_exported_candidates,
    resolve_poll_location,
    select_candidates,
    validate_export_context,
)

POLL_PUBLISHED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
JOINED_BEFORE_POLL = POLL_PUBLISHED_AT - timedelta(days=1)
_DEFAULT_JOIN_DATE = object()


def make_participant(
    user_id: int,
    *,
    joined_at: datetime | None | object = _DEFAULT_JOIN_DATE,
    is_admin: bool = False,
    is_bot: bool = False,
    is_deleted: bool = False,
) -> ParticipantRecord:
    if joined_at is _DEFAULT_JOIN_DATE:
        joined_at = JOINED_BEFORE_POLL
    assert joined_at is None or isinstance(joined_at, datetime)
    return ParticipantRecord(
        id=user_id,
        first_name=f"First {user_id}",
        last_name=f"Last {user_id}",
        username=f"user{user_id}",
        is_bot=is_bot,
        is_deleted=is_deleted,
        is_admin=is_admin,
        joined_at=joined_at,
    )


def candidate(user_id: int) -> dict[str, int | str | None]:
    return {
        "id": user_id,
        "first_name": f"First {user_id}",
        "last_name": f"Last {user_id}",
        "username": f"user{user_id}",
    }


def valid_export_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "exported_at": "2026-08-01T13:00:00+00:00",
        "exported_by_user_id": 900,
        "chat": {
            "id": -1001234567890,
            "title": "Cleanup test group",
            "username": "cleanup_test",
        },
        "poll": {
            "message_id": 42,
            "poll_id": 700,
            "question": "Who participated?",
            "published_at": POLL_PUBLISHED_AT.isoformat(),
            "closed": True,
            "public_voters": True,
        },
        "policy": {
            "exclude_self": True,
            "exclude_admins_and_creator": True,
            "exclude_joined_after_poll": True,
            "exclude_unknown_join_date": True,
            "include_bots": True,
            "include_deleted_accounts": True,
        },
        "candidates": [candidate(1), candidate(2)],
    }


class PollLocationTests(unittest.TestCase):
    def test_parse_poll_link_supports_public_private_and_forum_links(self) -> None:
        cases = {
            "public": (
                "https://t.me/sample_group/42",
                PollLocation(chat_ref="@sample_group", message_id=42),
            ),
            "public_preview": (
                "https://telegram.me/s/sample_group/42",
                PollLocation(chat_ref="@sample_group", message_id=42),
            ),
            "private": (
                "https://t.me/c/1234567890/42?single",
                PollLocation(chat_ref=-1001234567890, message_id=42),
            ),
            "public_forum_topic": (
                "https://t.me/sample_group/77/42",
                PollLocation(chat_ref="@sample_group", message_id=42),
            ),
            "private_forum_topic": (
                "https://t.me/c/1234567890/77/42",
                PollLocation(chat_ref=-1001234567890, message_id=42),
            ),
        }

        for name, (link, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(parse_poll_link(link), expected)

    def test_parse_poll_link_rejects_malformed_links(self) -> None:
        invalid_links = (
            "https://example.com/sample_group/42",
            "https://t.me/sample_group/not-a-message-id",
            "https://t.me/sample_group/0",
            "https://t.me/c/not-a-chat/42",
            "https://t.me/+invite/42",
        )

        for link in invalid_links:
            with self.subTest(link=link), self.assertRaises(CleanupError):
                parse_poll_link(link)

    def test_resolve_poll_location_accepts_exactly_one_input_form(self) -> None:
        self.assertEqual(
            resolve_poll_location(
                poll_link="https://t.me/sample_group/42",
                chat=None,
                message_id=None,
            ),
            PollLocation(chat_ref="@sample_group", message_id=42),
        )
        self.assertEqual(
            resolve_poll_location(poll_link=None, chat="sample_group", message_id=42),
            PollLocation(chat_ref="@sample_group", message_id=42),
        )
        self.assertEqual(
            resolve_poll_location(poll_link=None, chat="-100123", message_id=42),
            PollLocation(chat_ref=-100123, message_id=42),
        )

        invalid_arguments = (
            {
                "poll_link": "https://t.me/sample_group/42",
                "chat": "sample_group",
                "message_id": 42,
            },
            {"poll_link": None, "chat": "sample_group", "message_id": None},
            {"poll_link": None, "chat": None, "message_id": 42},
            {"poll_link": None, "chat": "sample_group", "message_id": 0},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), self.assertRaises(CleanupError):
                resolve_poll_location(**arguments)


class CandidateSelectionTests(unittest.TestCase):
    def test_select_candidates_applies_protection_and_join_date_policy(self) -> None:
        participants = [
            make_participant(8, is_deleted=True),
            make_participant(6, joined_at=None),
            make_participant(4),
            make_participant(2),
            make_participant(9, joined_at=POLL_PUBLISHED_AT.replace(tzinfo=None)),
            make_participant(3, is_admin=True),
            make_participant(7, is_bot=True),
            make_participant(5, joined_at=POLL_PUBLISHED_AT + timedelta(seconds=1)),
            make_participant(1),
        ]

        selected, reasons = select_candidates(
            participants,
            voter_ids={2},
            own_user_id=4,
            poll_published_at=POLL_PUBLISHED_AT,
        )

        # Bots and deleted accounts are deliberately included by the supported policy.
        self.assertEqual([record.id for record in selected], [1, 7, 8])
        self.assertTrue(next(record for record in selected if record.id == 7).is_bot)
        self.assertTrue(
            next(record for record in selected if record.id == 8).is_deleted
        )
        self.assertEqual(
            reasons,
            {
                "eligible": 3,
                "voted": 1,
                "admin": 1,
                "self": 1,
                # Equality is deliberately excluded: Telegram's timestamps only
                # prove that the member did not join before the poll.
                "joined_after_poll": 2,
                "unknown_join_date": 1,
            },
        )

    def test_select_candidates_applies_username_and_id_exclusions(self) -> None:
        participants = [make_participant(1), make_participant(7), make_participant(8)]
        participants[0] = replace(participants[0], username="Protected_User")
        exclusions = Exclusions(
            usernames=frozenset({"protected_user"}),
            user_ids=frozenset({7}),
        )

        selected, reasons = select_candidates(
            participants,
            voter_ids=set(),
            own_user_id=900,
            poll_published_at=POLL_PUBLISHED_AT,
            exclusions=exclusions,
        )

        self.assertEqual([record.id for record in selected], [8])
        self.assertEqual(reasons["excluded"], 2)


class ExclusionsFileTests(unittest.TestCase):
    def test_load_exclusions_normalizes_usernames_ids_comments_and_duplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "exclusions.txt"
            path.write_text(
                "# protected\n@Alice_User\nalice_user\n123456\n\n",
                encoding="utf-8",
            )

            exclusions = load_exclusions(path)

        self.assertEqual(exclusions.usernames, frozenset({"alice_user"}))
        self.assertEqual(exclusions.user_ids, frozenset({123456}))

    def test_load_exclusions_allows_a_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "missing.txt"
            exclusions = load_exclusions(path)

        self.assertEqual(exclusions.usernames, frozenset())
        self.assertEqual(exclusions.user_ids, frozenset())

    def test_load_exclusions_rejects_invalid_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "exclusions.txt"
            path.write_text("@bad username\n", encoding="utf-8")

            with self.assertRaisesRegex(CleanupError, "строка 1"):
                load_exclusions(path)


class ParticipantList(list[object]):
    total: int


class RawCountClient:
    def __init__(
        self,
        *,
        chat: types.Chat | types.Channel,
        counts: list[int],
        result_size: int,
        reported_total: int,
    ) -> None:
        self.chat = chat
        self.counts = iter(counts)
        self.requests: list[object] = []
        participants = ParticipantList(
            SimpleNamespace(id=user_id) for user_id in range(1, result_size + 1)
        )
        participants.total = reported_total
        self.get_participants = AsyncMock(return_value=participants)

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        count = next(self.counts)
        if isinstance(request, functions.channels.GetFullChannelRequest):
            return SimpleNamespace(
                full_chat=SimpleNamespace(participants_count=count)
            )
        if isinstance(request, functions.messages.GetFullChatRequest):
            raw_participants = [
                types.ChatParticipant(
                    user_id=user_id,
                    inviter_id=900,
                    date=JOINED_BEFORE_POLL,
                )
                for user_id in range(1, count + 1)
            ]
            return SimpleNamespace(
                full_chat=SimpleNamespace(
                    participants=types.ChatParticipants(
                        chat_id=self.chat.id,
                        participants=raw_participants,
                        version=1,
                    )
                )
            )
        raise AssertionError(f"Unexpected raw request: {request!r}")


def supergroup() -> types.Channel:
    return types.Channel(
        id=123,
        title="Test supergroup",
        photo=types.ChatPhotoEmpty(),
        date=None,
        megagroup=True,
    )


def basic_group() -> types.Chat:
    return types.Chat(
        id=123,
        title="Test group",
        photo=types.ChatPhotoEmpty(),
        participants_count=2,
        date=None,
        version=1,
    )


class ParticipantLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_all_participants_checks_raw_count_before_and_after(
        self,
    ) -> None:
        cases = (
            (supergroup(), functions.channels.GetFullChannelRequest),
            (basic_group(), functions.messages.GetFullChatRequest),
        )

        for chat, expected_request_type in cases:
            with self.subTest(chat_type=type(chat).__name__):
                client = RawCountClient(
                    chat=chat,
                    counts=[2, 2],
                    result_size=2,
                    reported_total=2,
                )

                participants = await fetch_all_participants(client, chat)

                self.assertEqual(len(participants), 2)
                self.assertEqual(len(client.requests), 2)
                self.assertTrue(
                    all(
                        isinstance(request, expected_request_type)
                        for request in client.requests
                    )
                )
                client.get_participants.assert_awaited_once_with(chat, limit=None)

    async def test_fetch_all_participants_rejects_a_count_change(self) -> None:
        chat = supergroup()
        client = RawCountClient(
            chat=chat,
            counts=[2, 3],
            result_size=2,
            reported_total=2,
        )

        with self.assertRaisesRegex(CleanupError, "Состав группы изменился"):
            await fetch_all_participants(client, chat)

    async def test_fetch_all_participants_rejects_an_incomplete_result(self) -> None:
        chat = basic_group()
        client = RawCountClient(
            chat=chat,
            counts=[3, 3],
            result_size=2,
            reported_total=3,
        )

        with self.assertRaisesRegex(CleanupError, "неполный список участников"):
            await fetch_all_participants(client, chat)


class RpcErrorTests(unittest.TestCase):
    def test_friendly_rpc_error_explains_poll_vote_requirement(self) -> None:
        message = friendly_rpc_error(errors.PollVoteRequiredError(request=None))

        self.assertIn("должен был проголосовать", message)
        self.assertIn("не голосует автоматически", message)


class ExportValidationTests(unittest.TestCase):
    def load(self, document: object) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "candidates.json"
            path.write_text(
                json.dumps(document, ensure_ascii=False),
                encoding="utf-8",
            )
            return load_export_document(path)

    def test_load_export_document_accepts_and_normalizes_valid_input(self) -> None:
        document = valid_export_document()
        document["candidates"] = [
            {
                "id": 1,
                "first_name": "Ivan",
                "last_name": None,
                "username": None,
                "ignored_field": "not copied",
            }
        ]

        loaded = self.load(document)

        self.assertEqual(
            loaded["candidates"],
            [
                {
                    "id": 1,
                    "first_name": "Ivan",
                    "last_name": None,
                    "username": None,
                }
            ],
        )

    def test_load_export_document_rejects_invalid_schema_and_field_types(self) -> None:
        mutations = {
            "schema version": lambda value: value.__setitem__("schema_version", 2),
            "boolean user id": lambda value: value.__setitem__(
                "exported_by_user_id", True
            ),
            "boolean chat id": lambda value: value["chat"].__setitem__("id", True),
            "missing chat title": lambda value: value["chat"].pop("title"),
            "non-positive message id": lambda value: value["poll"].__setitem__(
                "message_id", 0
            ),
            "open poll": lambda value: value["poll"].__setitem__("closed", False),
            "anonymous poll": lambda value: value["poll"].__setitem__(
                "public_voters", False
            ),
            "non-list candidates": lambda value: value.__setitem__(
                "candidates", {}
            ),
            "invalid optional name": lambda value: value["candidates"][0].__setitem__(
                "first_name", 123
            ),
        }

        for name, mutate in mutations.items():
            with self.subTest(name=name):
                document = copy.deepcopy(valid_export_document())
                mutate(document)
                with self.assertRaises(CleanupError):
                    self.load(document)

    def test_load_export_document_rejects_duplicate_candidate_ids(self) -> None:
        document = valid_export_document()
        document["candidates"] = [candidate(1), candidate(1)]

        with self.assertRaisesRegex(CleanupError, "Повторяющийся ID"):
            self.load(document)

    def test_load_export_document_requires_the_supported_policy(self) -> None:
        policy_keys = tuple(valid_export_document()["policy"])

        for key in policy_keys:
            with self.subTest(key=key):
                document = copy.deepcopy(valid_export_document())
                document["policy"][key] = False
                with self.assertRaises(CleanupError):
                    self.load(document)

    def test_validate_export_context_checks_account_chat_message_and_poll(self) -> None:
        document = valid_export_document()
        expected_context = {
            "own_user_id": 900,
            "chat_id": -1001234567890,
            "message_id": 42,
            "poll_id": 700,
        }
        validate_export_context(document, **expected_context)

        mismatches = {
            "account": {"own_user_id": 901},
            "chat": {"chat_id": -100999},
            "message": {"message_id": 43},
            "poll": {"poll_id": 701},
        }
        for name, replacement in mismatches.items():
            with self.subTest(name=name):
                actual_context = expected_context | replacement
                with self.assertRaises(CleanupError):
                    validate_export_context(document, **actual_context)


class ExportRevalidationTests(unittest.TestCase):
    def test_partition_exported_candidates_preserves_order_and_explains_skips(
        self,
    ) -> None:
        exported = [candidate(user_id) for user_id in range(1, 9)]
        current = {
            2: make_participant(2),
            3: make_participant(3),
            4: make_participant(4, is_admin=True),
            5: make_participant(5),
            6: make_participant(
                6, joined_at=POLL_PUBLISHED_AT + timedelta(seconds=1)
            ),
            7: make_participant(7, joined_at=None),
            8: make_participant(8, is_bot=True),
        }

        ready_ids, decisions = partition_exported_candidates(
            exported,
            current_participants=current,
            voter_ids={3},
            own_user_id=5,
            poll_published_at=POLL_PUBLISHED_AT,
        )

        self.assertEqual(ready_ids, [2, 8])
        self.assertEqual(
            [
                (decision.candidate["id"], decision.status, decision.reason)
                for decision in decisions
            ],
            [
                (1, "already_absent", "not_a_current_participant"),
                (3, "skipped_protected", "voted"),
                (4, "skipped_protected", "admin"),
                (5, "skipped_protected", "self"),
                (6, "skipped_protected", "joined_after_poll"),
                (7, "skipped_protected", "unknown_join_date"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
