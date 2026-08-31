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
    load_poll_context,
    participant_to_record,
    print_candidate_table,
    resolve_poll_location,
    select_candidates,
    tighten_session_permissions,
    write_private_json,
)

BASE_DIR = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Сформировать список текущих участников закрытого неанонимного "
            "Telegram-опроса, которые не проголосовали."
        )
    )
    parser.add_argument("--poll-link", help="Ссылка t.me на сообщение с опросом")
    parser.add_argument(
        "--chat",
        help="Username группы (@group) или числовой ID; используйте с --message-id",
    )
    parser.add_argument(
        "--message-id",
        type=int,
        help="ID сообщения с опросом; используйте с --chat",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("non_voters.json"),
        help="Файл результата (по умолчанию: non_voters.json)",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    location = resolve_poll_location(
        poll_link=args.poll_link,
        chat=args.chat,
        message_id=args.message_id,
    )
    config = load_config(BASE_DIR)
    client = create_client(config)

    try:
        await client.start()
        tighten_session_permissions(config.session_path)
        me = await client.get_me()
        if me is None:
            raise CleanupError("Не удалось определить авторизованный аккаунт.")
        if bool(getattr(me, "bot", False)):
            raise CleanupError("Нужен пользовательский аккаунт, а не bot token.")

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
        )

        document = build_export_document(
            own_user_id=int(me.id),
            context=context,
            candidates=candidates,
        )
        write_private_json(args.output, document)

        print_candidate_table(document["candidates"])
        print()
        print(f"Текущих участников получено: {len(participants)}")
        print(f"Уникальных голосовавших найдено: {len(voter_ids)}")
        print(f"Кандидатов на удаление: {len(candidates)}")
        print(
            "Исключено: "
            f"голосовали={reasons['voted']}, "
            f"владелец/администраторы={reasons['admin']}, "
            f"этот аккаунт={reasons['self']}, "
            f"вступили позже={reasons['joined_after_poll']}, "
            f"неизвестна дата вступления={reasons['unknown_join_date']}"
        )
        print(f"Список сохранён: {args.output.expanduser().resolve()}")
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
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    except errors.FloodWaitError as error:
        print(
            "Telegram потребовал паузу. Ничего не повторяйте минимум "
            f"{error.seconds} секунд. Список не сформирован.",
            file=sys.stderr,
        )
        return 3
    except errors.PeerFloodError:
        print(
            "Telegram ограничил аккаунт (PeerFlood). Остановитесь и проверьте @SpamBot.",
            file=sys.stderr,
        )
        return 3
    except errors.RPCError as error:
        print(f"Ошибка: {friendly_rpc_error(error)}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"Системная или сетевая ошибка: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Операция отменена.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
