from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, errors, functions, types, utils

SCHEMA_VERSION = 1
DEFAULT_SESSION_NAME = "telegram_poll_cleanup"
MAX_CANDIDATES = 100_000


class CleanupError(RuntimeError):
    """An expected error that can be shown to the operator."""


@dataclass(frozen=True)
class AppConfig:
    api_id: int
    api_hash: str
    session_path: Path


@dataclass(frozen=True)
class PollLocation:
    chat_ref: int | str
    message_id: int


@dataclass(frozen=True)
class PollContext:
    chat: Any
    message: Any

    @property
    def poll(self) -> Any:
        return self.message.media.poll


@dataclass(frozen=True)
class ParticipantRecord:
    id: int
    first_name: str | None
    last_name: str | None
    username: str | None
    is_bot: bool
    is_deleted: bool
    is_admin: bool
    joined_at: datetime | None

    def as_candidate(self) -> dict[str, int | str | None]:
        return {
            "id": self.id,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "username": self.username,
        }


@dataclass(frozen=True)
class CandidateDecision:
    candidate: Mapping[str, Any]
    status: str
    reason: str


@dataclass(frozen=True)
class Exclusions:
    usernames: frozenset[str]
    user_ids: frozenset[int]

    def matches(self, participant: ParticipantRecord) -> bool:
        if participant.id in self.user_ids:
            return True
        username = participant.username
        return username is not None and username.casefold() in self.usernames


EMPTY_EXCLUSIONS = Exclusions(usernames=frozenset(), user_ids=frozenset())


def load_exclusions(path: Path) -> Exclusions:
    path = path.expanduser()
    if not path.exists():
        return EMPTY_EXCLUSIONS

    usernames: set[str] = set()
    user_ids: set[int] = set()
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                value = raw_line.strip()
                if not value or value.startswith("#"):
                    continue
                if value.isdecimal():
                    user_id = int(value)
                    if user_id <= 0:
                        raise CleanupError(
                            f"Invalid ID in {path}, line {line_number}."
                        )
                    user_ids.add(user_id)
                    continue

                username = value.removeprefix("@")
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
                    raise CleanupError(
                        f"Invalid username in {path}, line {line_number}: "
                        f"{value}"
                    )
                usernames.add(username.casefold())
    except OSError as error:
        raise CleanupError(f"Could not read exclusions file {path}: {error}") from error

    return Exclusions(usernames=frozenset(usernames), user_ids=frozenset(user_ids))


def load_config(base_dir: Path) -> AppConfig:
    load_dotenv(base_dir / ".env", override=False)

    raw_api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    raw_session = os.getenv("TELEGRAM_SESSION", DEFAULT_SESSION_NAME).strip()

    if not raw_api_id or not api_hash:
        raise CleanupError(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in the .env file."
        )

    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise CleanupError("TELEGRAM_API_ID must be an integer.") from exc
    if api_id <= 0:
        raise CleanupError("TELEGRAM_API_ID must be a positive integer.")

    session_value = raw_session or DEFAULT_SESSION_NAME
    session_path = Path(session_value).expanduser()
    if not session_path.is_absolute():
        session_path = base_dir / session_path

    return AppConfig(api_id=api_id, api_hash=api_hash, session_path=session_path)


def create_client(config: AppConfig) -> TelegramClient:
    return TelegramClient(
        str(config.session_path),
        config.api_id,
        config.api_hash,
        flood_sleep_threshold=0,
        receive_updates=False,
    )


def session_file_path(session_path: Path) -> Path:
    if session_path.suffix == ".session":
        return session_path
    return Path(f"{session_path}.session")


def tighten_session_permissions(session_path: Path) -> None:
    for candidate in (
        session_file_path(session_path),
        Path(f"{session_file_path(session_path)}-journal"),
        Path(f"{session_file_path(session_path)}-wal"),
        Path(f"{session_file_path(session_path)}-shm"),
    ):
        if candidate.exists():
            candidate.chmod(0o600)


def parse_poll_link(link: str) -> PollLocation:
    parsed = urlparse(link.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in {
        "t.me",
        "www.t.me",
        "telegram.me",
        "www.telegram.me",
    }:
        raise CleanupError("Expected a poll message link in the form https://t.me/....")

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if parts and parts[0] == "s":
        parts = parts[1:]
    if len(parts) < 2:
        raise CleanupError("The link does not contain a message ID.")

    try:
        message_id = int(parts[-1])
    except ValueError as exc:
        raise CleanupError("The final part of the link must be a message ID.") from exc
    if message_id <= 0:
        raise CleanupError("The message ID must be a positive integer.")

    if parts[0] == "c":
        if len(parts) < 3 or not parts[1].isdigit():
            raise CleanupError("Invalid private group message link.")
        chat_ref: int | str = int(f"-100{parts[1]}")
    else:
        username = parts[0].lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{4,}", username):
            raise CleanupError("The link does not contain a valid group username.")
        chat_ref = f"@{username}"

    return PollLocation(chat_ref=chat_ref, message_id=message_id)


def resolve_poll_location(
    *, poll_link: str | None, chat: str | None, message_id: int | None
) -> PollLocation:
    if poll_link:
        if chat is not None or message_id is not None:
            raise CleanupError(
                "Use either --poll-link or the --chat and --message-id pair."
            )
        return parse_poll_link(poll_link)

    if chat is None or message_id is None:
        raise CleanupError(
            "Provide --poll-link or both --chat and --message-id."
        )
    if message_id <= 0:
        raise CleanupError("--message-id must be a positive integer.")

    normalized_chat: int | str
    try:
        normalized_chat = int(chat)
    except ValueError:
        normalized_chat = chat if chat.startswith("@") else f"@{chat}"
    return PollLocation(chat_ref=normalized_chat, message_id=message_id)


async def resolve_chat(client: TelegramClient, chat_ref: int | str) -> Any:
    try:
        return await client.get_entity(chat_ref)
    except (TypeError, ValueError) as original_error:
        if isinstance(chat_ref, int):
            async for dialog in client.iter_dialogs():
                if utils.get_peer_id(dialog.entity) == chat_ref:
                    return dialog.entity
        raise CleanupError(
            "Could not find the group. Make sure the account is a member and "
            "the link or ID is correct."
        ) from original_error


def ensure_supported_group(chat: Any) -> None:
    if isinstance(chat, types.Chat):
        if getattr(chat, "deactivated", False):
            raise CleanupError("The group has been deactivated.")
        return
    if (
        isinstance(chat, types.Channel)
        and bool(getattr(chat, "megagroup", False))
        and not bool(getattr(chat, "broadcast", False))
    ):
        return
    raise CleanupError(
        "Only basic groups and supergroups are supported; channels are not."
    )


async def load_poll_context(
    client: TelegramClient,
    chat_ref: int | str,
    message_id: int,
    *,
    require_closed: bool = True,
) -> PollContext:
    chat = await resolve_chat(client, chat_ref)
    ensure_supported_group(chat)

    message = await client.get_messages(chat, ids=message_id)
    if message is None:
        raise CleanupError("No message was found with the specified ID.")
    if not isinstance(getattr(message, "media", None), types.MessageMediaPoll):
        raise CleanupError("The specified message does not contain a poll.")

    poll = message.media.poll
    if not bool(getattr(poll, "public_voters", False)):
        raise CleanupError(
            "The poll is anonymous, so Telegram does not expose voter identities."
        )
    if require_closed and not bool(getattr(poll, "closed", False)):
        raise CleanupError(
            "The poll is still open. Close it before generating the list."
        )

    return PollContext(chat=chat, message=message)


async def fetch_voter_ids(
    client: TelegramClient, chat: Any, message_id: int
) -> set[int]:
    voter_ids: set[int] = set()
    offset: str | None = None
    seen_offsets: set[str] = set()
    expected_count: int | None = None

    while True:
        result = await client(
            functions.messages.GetPollVotesRequest(
                peer=chat,
                id=message_id,
                limit=100,
                offset=offset,
            )
        )
        page_count = int(result.count)
        if expected_count is None:
            expected_count = page_count
        elif page_count != expected_count:
            raise CleanupError("The vote count changed during export.")
        for vote in result.votes:
            peer = getattr(vote, "peer", None)
            if not isinstance(peer, types.PeerUser):
                raise CleanupError(
                    "A vote submitted as a channel or another chat was found. "
                    "It cannot be safely associated with a specific member."
                )
            voter_ids.add(int(peer.user_id))

        next_offset = getattr(result, "next_offset", None) or ""
        if not next_offset:
            break
        if next_offset in seen_offsets:
            raise CleanupError("Telegram returned a repeated vote offset.")
        seen_offsets.add(next_offset)
        offset = next_offset

    if expected_count is not None and len(voter_ids) != expected_count:
        raise CleanupError(
            f"Incomplete voter list: received {len(voter_ids)} of {expected_count}."
        )
    return voter_ids


async def fetch_all_participants(client: TelegramClient, chat: Any) -> list[Any]:
    count_before = await fetch_current_participant_count(client, chat)
    result = await client.get_participants(chat, limit=None)
    count_after = await fetch_current_participant_count(client, chat)
    total = getattr(result, "total", None)
    if count_before != count_after:
        raise CleanupError(
            "Group membership changed during export. Try again later."
        )
    if len(result) != count_after or (
        total is not None and int(total) != count_after
    ):
        raise CleanupError(
            f"Incomplete member list: received {len(result)} of {count_after}. "
            "Removal based on incomplete data is not allowed."
        )
    return list(result)


async def fetch_current_participant_count(client: TelegramClient, chat: Any) -> int:
    if isinstance(chat, types.Channel):
        result = await client(functions.channels.GetFullChannelRequest(chat))
        count = getattr(result.full_chat, "participants_count", None)
        if count is None:
            raise CleanupError(
                "Telegram did not provide the total supergroup member count."
            )
        return int(count)

    if isinstance(chat, types.Chat):
        result = await client(functions.messages.GetFullChatRequest(chat.id))
        participants = getattr(result.full_chat, "participants", None)
        if isinstance(participants, types.ChatParticipantsForbidden):
            raise CleanupError(
                "Telegram hid the basic group member list; a safe export is impossible."
            )
        if not isinstance(participants, types.ChatParticipants):
            raise CleanupError("Telegram returned an unknown group membership format.")
        return len(participants.participants)

    raise CleanupError("Could not determine the group member count.")


_ADMIN_PARTICIPANT_TYPES = (
    types.ChannelParticipantAdmin,
    types.ChannelParticipantCreator,
    types.ChatParticipantAdmin,
    types.ChatParticipantCreator,
)
_PARTICIPANT_UNSET = object()


def participant_to_record(
    user: Any, *, participant: Any = _PARTICIPANT_UNSET
) -> ParticipantRecord:
    if participant is _PARTICIPANT_UNSET:
        participant = getattr(user, "participant", None)
    joined_at = getattr(participant, "date", None)
    if not isinstance(joined_at, datetime):
        joined_at = None

    return ParticipantRecord(
        id=int(user.id),
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        username=getattr(user, "username", None),
        is_bot=bool(getattr(user, "bot", False)),
        is_deleted=bool(getattr(user, "deleted", False)),
        is_admin=isinstance(participant, _ADMIN_PARTICIPANT_TYPES),
        joined_at=joined_at,
    )


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def eligibility_reason(
    participant: ParticipantRecord,
    *,
    voter_ids: set[int],
    own_user_id: int,
    poll_published_at: datetime,
    exclusions: Exclusions = EMPTY_EXCLUSIONS,
) -> str:
    if exclusions.matches(participant):
        return "excluded"
    if participant.id == own_user_id:
        return "self"
    if participant.is_admin:
        return "admin"
    if participant.id in voter_ids:
        return "voted"
    if participant.joined_at is None:
        return "unknown_join_date"
    if _to_utc(participant.joined_at) >= _to_utc(poll_published_at):
        return "joined_after_poll"
    return "eligible"


def select_candidates(
    participants: Iterable[ParticipantRecord],
    *,
    voter_ids: set[int],
    own_user_id: int,
    poll_published_at: datetime,
    exclusions: Exclusions = EMPTY_EXCLUSIONS,
) -> tuple[list[ParticipantRecord], Counter[str]]:
    candidates: list[ParticipantRecord] = []
    reasons: Counter[str] = Counter()
    for participant in participants:
        reason = eligibility_reason(
            participant,
            voter_ids=voter_ids,
            own_user_id=own_user_id,
            poll_published_at=poll_published_at,
            exclusions=exclusions,
        )
        reasons[reason] += 1
        if reason == "eligible":
            candidates.append(participant)
    candidates.sort(key=lambda item: item.id)
    return candidates, reasons


def _poll_question(poll: Any) -> str:
    question = getattr(poll, "question", "")
    return str(getattr(question, "text", question))


def build_export_document(
    *,
    own_user_id: int,
    context: PollContext,
    candidates: Sequence[ParticipantRecord],
) -> dict[str, Any]:
    chat = context.chat
    poll = context.poll
    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_by_user_id": own_user_id,
        "chat": {
            "id": utils.get_peer_id(chat),
            "title": str(getattr(chat, "title", "")),
            "username": getattr(chat, "username", None),
        },
        "poll": {
            "message_id": int(context.message.id),
            "poll_id": int(poll.id),
            "question": _poll_question(poll),
            "published_at": _to_utc(context.message.date).isoformat(),
            "closed": bool(poll.closed),
            "public_voters": bool(poll.public_voters),
        },
        "policy": {
            "exclude_self": True,
            "exclude_admins_and_creator": True,
            "exclude_joined_after_poll": True,
            "exclude_unknown_join_date": True,
            "include_bots": True,
            "include_deleted_accounts": True,
        },
        "candidates": [candidate.as_candidate() for candidate in candidates],
    }


def write_private_json(path: Path, document: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            os.chmod(temporary_path, 0o600)
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _required_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CleanupError(f"The {name} field must be a JSON object.")
    return value


def _required_int(value: Any, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CleanupError(f"The {name} field must be an integer.")
    if positive and value <= 0:
        raise CleanupError(f"The {name} field must be a positive integer.")
    return value


def _optional_string(value: Any, name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise CleanupError(f"The {name} field must be a string or null.")
    return value


def load_export_document(path: Path) -> dict[str, Any]:
    try:
        with path.expanduser().open(encoding="utf-8") as stream:
            document = json.load(stream)
    except FileNotFoundError as exc:
        raise CleanupError(f"Candidate list file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanupError(f"Could not read the JSON candidate list: {exc}") from exc

    root = _required_mapping(document, "document root")
    if root.get("schema_version") != SCHEMA_VERSION:
        raise CleanupError(
            f"Only schema_version={SCHEMA_VERSION} is supported."
        )

    _required_int(root.get("exported_by_user_id"), "exported_by_user_id", positive=True)
    chat = _required_mapping(root.get("chat"), "chat")
    _required_int(chat.get("id"), "chat.id")
    if not isinstance(chat.get("title"), str):
        raise CleanupError("The chat.title field must be a string.")

    poll = _required_mapping(root.get("poll"), "poll")
    _required_int(poll.get("message_id"), "poll.message_id", positive=True)
    _required_int(poll.get("poll_id"), "poll.poll_id", positive=True)
    if poll.get("closed") is not True or poll.get("public_voters") is not True:
        raise CleanupError("The list must refer to a closed, non-anonymous poll.")

    policy = _required_mapping(root.get("policy"), "policy")
    expected_policy = {
        "exclude_self": True,
        "exclude_admins_and_creator": True,
        "exclude_joined_after_poll": True,
        "exclude_unknown_join_date": True,
        "include_bots": True,
        "include_deleted_accounts": True,
    }
    if any(policy.get(key) is not value for key, value in expected_policy.items()):
        raise CleanupError("The candidate policy in the file is not supported.")

    raw_candidates = root.get("candidates")
    if not isinstance(raw_candidates, list):
        raise CleanupError("The candidates field must be a list.")
    if len(raw_candidates) > MAX_CANDIDATES:
        raise CleanupError(f"The list contains more than {MAX_CANDIDATES} entries.")

    normalized_candidates: list[dict[str, int | str | None]] = []
    seen_ids: set[int] = set()
    for index, raw_candidate in enumerate(raw_candidates):
        candidate = _required_mapping(raw_candidate, f"candidates[{index}]")
        user_id = _required_int(
            candidate.get("id"), f"candidates[{index}].id", positive=True
        )
        if user_id in seen_ids:
            raise CleanupError(f"Duplicate member ID: {user_id}")
        seen_ids.add(user_id)
        normalized_candidates.append(
            {
                "id": user_id,
                "first_name": _optional_string(
                    candidate.get("first_name"), f"candidates[{index}].first_name"
                ),
                "last_name": _optional_string(
                    candidate.get("last_name"), f"candidates[{index}].last_name"
                ),
                "username": _optional_string(
                    candidate.get("username"), f"candidates[{index}].username"
                ),
            }
        )

    normalized = dict(root)
    normalized["chat"] = dict(chat)
    normalized["poll"] = dict(poll)
    normalized["policy"] = dict(policy)
    normalized["candidates"] = normalized_candidates
    return normalized


def validate_export_context(
    document: Mapping[str, Any],
    *,
    own_user_id: int,
    chat_id: int,
    message_id: int,
    poll_id: int,
) -> None:
    if int(document["exported_by_user_id"]) != own_user_id:
        raise CleanupError(
            "The list was created by another Telegram account. Use the original session."
        )
    if int(document["chat"]["id"]) != chat_id:
        raise CleanupError("The current group does not match the group in the list.")
    if int(document["poll"]["message_id"]) != message_id:
        raise CleanupError("The message ID does not match the list metadata.")
    if int(document["poll"]["poll_id"]) != poll_id:
        raise CleanupError("The poll in the message changed and no longer matches the list.")


def partition_exported_candidates(
    exported_candidates: Sequence[Mapping[str, Any]],
    *,
    current_participants: Mapping[int, ParticipantRecord],
    voter_ids: set[int],
    own_user_id: int,
    poll_published_at: datetime,
    exclusions: Exclusions = EMPTY_EXCLUSIONS,
) -> tuple[list[int], list[CandidateDecision]]:
    ready_ids: list[int] = []
    decisions: list[CandidateDecision] = []
    for candidate in exported_candidates:
        user_id = int(candidate["id"])
        participant = current_participants.get(user_id)
        if participant is None:
            decisions.append(
                CandidateDecision(
                    candidate=candidate,
                    status="already_absent",
                    reason="not_a_current_participant",
                )
            )
            continue

        reason = eligibility_reason(
            participant,
            voter_ids=voter_ids,
            own_user_id=own_user_id,
            poll_published_at=poll_published_at,
            exclusions=exclusions,
        )
        if reason == "eligible":
            ready_ids.append(user_id)
        else:
            decisions.append(
                CandidateDecision(
                    candidate=candidate,
                    status="skipped_protected",
                    reason=reason,
                )
            )
    return ready_ids, decisions


def _display_text(value: Any, *, limit: int = 32) -> str:
    if value is None:
        return ""
    normalized = " ".join(str(value).replace("\t", " ").splitlines())
    if len(normalized) > limit:
        return f"{normalized[: limit - 1]}…"
    return normalized


def print_candidate_table(candidates: Sequence[Mapping[str, Any]]) -> None:
    headers = ("ID", "first name", "last name", "username")
    rows = [
        (
            str(candidate["id"]),
            _display_text(candidate.get("first_name")),
            _display_text(candidate.get("last_name")),
            _display_text(candidate.get("username")),
        )
        for candidate in candidates
    ]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(row: Sequence[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))

    print(render(headers))
    print(render(tuple("-" * width for width in widths)))
    for row in rows:
        print(render(row))


def append_private_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    path.chmod(0o600)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def friendly_rpc_error(error: errors.RPCError) -> str:
    error_name = type(error).__name__
    messages = {
        "PollVoteRequiredError": (
            "Telegram did not allow voter retrieval: this account had to vote "
            "in the poll. The script does not vote automatically."
        ),
        "BroadcastForbiddenError": (
            "Telegram does not allow voter retrieval for a poll in a broadcast channel."
        ),
        "MessageIdInvalidError": "Telegram did not find a message with that ID.",
        "ChatAdminRequiredError": (
            "The account does not have the administrator rights required for this action."
        ),
        "ChannelPrivateError": (
            "The group is unavailable to the account, or the account is no longer a member."
        ),
        "ChatWriteForbiddenError": "The account cannot perform administrative actions in the group.",
    }
    return messages.get(error_name, f"Telegram error {error_name}: {error}")
