from __future__ import annotations

import unittest
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from telethon import types

from telegram_poll_cleanup import (
    CleanupError,
    fetch_all_participants,
    fetch_voter_ids,
)


def votes_page(
    count: int,
    user_ids: Sequence[int],
    *,
    next_offset: str | None = None,
    non_user_peer: Any | None = None,
) -> types.messages.VotesList:
    votes = [
        types.MessagePeerVote(
            peer=types.PeerUser(user_id),
            option=b"option",
            date=None,
        )
        for user_id in user_ids
    ]
    if non_user_peer is not None:
        votes.append(
            types.MessagePeerVote(
                peer=non_user_peer,
                option=b"option",
                date=None,
            )
        )
    return types.messages.VotesList(
        count=count,
        votes=votes,
        chats=[],
        users=[],
        next_offset=next_offset,
    )


class RequestClient:
    def __init__(self, responses: Sequence[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        return self.responses.pop(0)


class ParticipantResult(list[Any]):
    def __init__(self, values: Sequence[Any], *, total: int) -> None:
        super().__init__(values)
        self.total = total


class ParticipantClient:
    def __init__(self, result: ParticipantResult) -> None:
        self.result = result

    async def get_participants(self, chat: Any, limit: int | None = None) -> Any:
        return self.result

    async def __call__(self, request: Any) -> Any:
        participants = [
            types.ChatParticipant(user_id=user_id, inviter_id=1, date=None)
            for user_id in range(1, self.result.total + 1)
        ]
        return SimpleNamespace(
            full_chat=SimpleNamespace(
                participants=types.ChatParticipants(
                    chat_id=request.chat_id,
                    participants=participants,
                    version=1,
                )
            )
        )


class PollVotesAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_voter_ids_paginates_and_uses_message_id(self) -> None:
        client = RequestClient(
            [
                votes_page(3, [3, 1], next_offset="page-2"),
                votes_page(3, [2]),
            ]
        )

        result = await fetch_voter_ids(
            client, types.InputPeerChat(chat_id=10), message_id=42
        )

        self.assertEqual(result, {1, 2, 3})
        self.assertEqual([request.id for request in client.requests], [42, 42])
        self.assertEqual([request.limit for request in client.requests], [100, 100])
        self.assertEqual(
            [request.offset for request in client.requests], [None, "page-2"]
        )
        self.assertTrue(all(request.option is None for request in client.requests))

    async def test_fetch_voter_ids_rejects_incomplete_or_changing_results(self) -> None:
        cases = {
            "missing vote": [votes_page(2, [1])],
            "changing count": [
                votes_page(2, [1], next_offset="page-2"),
                votes_page(3, [2]),
            ],
            "non-user vote": [
                votes_page(1, [], non_user_peer=types.PeerChannel(20))
            ],
            "repeated offset": [
                votes_page(2, [1], next_offset="again"),
                votes_page(2, [], next_offset="again"),
            ],
        }

        for name, responses in cases.items():
            with self.subTest(name=name), self.assertRaises(CleanupError):
                await fetch_voter_ids(
                    RequestClient(responses),
                    types.InputPeerChat(chat_id=10),
                    message_id=42,
                )


class ParticipantsAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_all_participants_requires_the_full_total(self) -> None:
        chat = types.Chat(
            id=10,
            title="Test group",
            photo=types.ChatPhotoEmpty(),
            participants_count=2,
            date=None,
            version=1,
        )
        complete = ParticipantResult(["one", "two"], total=2)
        self.assertEqual(
            await fetch_all_participants(ParticipantClient(complete), chat),
            ["one", "two"],
        )

        incomplete = ParticipantResult(["one"], total=2)
        with self.assertRaises(CleanupError):
            await fetch_all_participants(ParticipantClient(incomplete), chat)


if __name__ == "__main__":
    unittest.main()
