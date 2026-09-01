[English](README.md) | [Русский](README.ru.md)

# Remove Telegram group members based on poll results

A small local Python and Telethon utility that works on behalf of your Telegram
account in two stages:

1. `list_non_voters.py` finds current group members who did not participate in
   the specified poll, prints them to the terminal, and saves an auditable JSON
   file.
2. `remove_members.py` reads that JSON file, checks the current group membership
   and votes again, and then removes eligible members only when explicitly run
   with `--execute`.

For each candidate, the output includes `ID`, `first name`, `last name`, and
`username`. Only the numeric Telegram ID is used for actions because names and
usernames can be missing or changed.

> [!WARNING]
> There is no guarantee that Telegram will not restrict or ban your account.
> Telegram does not publish a "safe" number of such actions. Small batches and
> delays may reduce the risk, but they do not eliminate it. Test the utility in
> a separate test group first, and do not run a mass cleanup from a valuable
> personal account.

## Requirements and limitations

- Python 3.12.
- The account running the utility must be a member of the group and have
  permission to remove members.
- That account must vote in the poll before it is closed. Otherwise, Telegram
  will not return the voter list (`POLL_VOTE_REQUIRED`), and this cannot be
  corrected after the poll has closed. This is a limitation of
  [`messages.getPollVotes`](https://core.telegram.org/method/messages.getPollVotes).
- Only a **closed, non-anonymous poll** in a group or supergroup is supported.
  For an open or anonymous poll, it is impossible to reliably determine the
  complete final list of non-voters.
- Broadcast channels are not supported.
- The utility must retrieve the complete participant list and every page of
  votes. It exits without removing anyone if the data is incomplete.
- The group owner, administrators, and the current account are excluded.
  Members who joined after the poll was published, and members whose join date
  cannot be verified, are also excluded. Members with an existing restriction
  are protected as well: the script does not replace restrictions set by
  another administrator.
- Bots and deleted accounts are included in the candidate list. Bots cannot
  vote, so this policy will predictably include them.
- In a basic group, the script performs a kick without a permanent ban. In a
  supergroup, it applies a single 10-minute temporary ban, after which Telegram
  allows the user to join again automatically. The script deliberately does
  not send an immediate second unban request: this prevents a failure between
  ban and unban from leaving a member blocked forever, and avoids removing a
  restriction applied by another administrator. Before every request, the
  deadline is recalculated from Telegram server time rather than the computer's
  clock. A suspend-aware check cancels the operation if there is an unexpected
  time jump or problematic clock synchronization after sleep.

## Obtaining `api_id` and `api_hash`

Telethon connects to the Telegram API as a client application. It requires
application credentials, not a bot token:

1. Go to [my.telegram.org](https://my.telegram.org) and sign in with the phone
   number of your Telegram account.
2. Enter the confirmation code sent by Telegram.
3. Open **API development tools**.
4. Create an application and provide the requested application name and short
   name.
5. Copy the issued `api_id` and `api_hash` to your local `.env` file as
   described below.

Official Telegram instructions:
[Obtaining api_id](https://core.telegram.org/api/obtaining_api_id).

Never publish your `api_hash`. Do not store login codes or your two-step
verification password in the project either.

## Installation

```bash
cd /home/ihoru/tmp/telegram-poll-cleanup
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

To run Ruff locally and use the same checks as GitHub Actions, also install the
development dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Create a local configuration file:

```bash
cp .env.example .env
chmod 600 .env
```

Fill in `.env`:

```dotenv
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_SESSION=telegram_poll_cleanup
```

`TELEGRAM_SESSION` is the name of the local session file, not an authorization
secret. With the value above, Telethon will create
`telegram_poll_cleanup.session` in the project directory.

### First login

On the first run, Telethon will ask for the following in sequence:

1. the account phone number in international format;
2. the code sent by Telegram;
3. the two-step verification password, if enabled.

After a successful login, authorization is stored in the `.session` file, so a
new code is usually not required on subsequent runs. Do not share this file: it
grants access to the account. Moving or renaming the session file may trigger a
new login request.

## 1. Generate the non-voter list

Provide a link to the poll message:

```bash
python list_non_voters.py \
  --poll-link "https://t.me/c/1234567890/42" \
  --output non_voters.json
```

Links to messages in public groups are also supported, for example
`https://t.me/group_username/42`.

Alternatively, provide the group and message ID separately:

```bash
python list_non_voters.py \
  --chat "@group_username" \
  --message-id 42 \
  --output non_voters.json
```

For a private group, `--chat` may be a numeric Telegram ID such as
`-1001234567890`:

```bash
python list_non_voters.py \
  --chat "-1001234567890" \
  --message-id 42 \
  --output non_voters.json
```

The script prints a candidate table and saves JSON metadata about the group,
poll, and account that created the export. These fields allow the second script
to reject a list created for a different group, poll, or account.

Review `non_voters.json` before proceeding. Missing `first_name`, `last_name`,
or `username` values are stored as `null` and are not errors by themselves.

### Exclusions file

The local `exclusions.txt` file protects members who must not be listed or
removed. Put one `@username` or numeric Telegram user ID on each line. Empty
lines and lines beginning with `#` are ignored:

```text
@example_username
123456789
```

Both scripts read `exclusions.txt` from the project directory by default. The
second script applies the current file again immediately before removal, so a
new entry protects a user even after the JSON list has been generated. Use
`--exclusions` to provide a different path when needed.

A numeric ID is safer than a username because usernames can be changed or
transferred to another account. `exclusions.txt` contains personal data and is
listed in `.gitignore`; only the safe `exclusions.example.txt` template is
published to GitHub.

## 2. Review and remove

Run a dry run first. Without `--execute`, **no members are removed**:

```bash
python remove_members.py
```

The script displays the group and the current final candidate list after
rechecking it. By default, it reads `non_voters.json`; use `--input` to select a
different file. Add `--execute` to perform the removal:

```bash
python remove_members.py --execute
```

Before the first removal, you must manually enter a confirmation in the form
`REMOVE N`, where `N` is the displayed candidate count.

By default, one run removes no more than 10 members, sequentially, with a random
delay of 15 to 30 seconds. Use `--limit 1` for a cautious first live test:

```bash
python remove_members.py --limit 1 --execute
```

The explicit equivalent of the default settings is:

```bash
python remove_members.py \
  --execute \
  --limit 10 \
  --min-delay 15 \
  --max-delay 30 \
  --log removal_results.jsonl
```

`--batch-size` remains available as an alias for `--limit`.

Immediately before taking action, the script retrieves the participants and
votes again. Before each individual removal, it rechecks the member's status
and latest join date, skipping anyone who left and rejoined, became an
administrator, or was already restricted by another administrator. The result
for each candidate is appended to `removal_results.jsonl`, so completed actions
are not lost if the process stops.

For a supergroup, `kick_started` is written before the request. If the
connection or process stops at an uncertain moment, further removals are
blocked until the 10-minute temporary ban expires, plus a one-minute safety
margin. The script does not attempt to remove the ban automatically. After the
`safe_retry_after` time recorded in the log, repeat the dry run: Telegram should
already have removed this specific temporary restriction automatically.

If a `FloodWaitError` or `PeerFloodError` occurs, execution stops immediately.
Do not restart the utility to bypass the restriction, do not switch to another
account, and observe the waiting period specified by Telegram.

## Data security

The `.env`, `*.session`, candidate list, and log files contain secrets or
personal data. They are listed in `.gitignore`, but this does not protect a file
with a custom name and does not replace careful handling.

Recommended file permissions:

```bash
chmod 600 .env *.session non_voters.json removal_results.jsonl
```

- Never add these files to Git, a cloud-synced folder, or a chat message.
- Do not publish screenshots containing `api_hash`, login codes, or personal
  data.
- Keep the export only as long as needed for review and execution.
- If the session file may have been exposed, close suspicious sessions under
  **Telegram -> Settings -> Devices** and create a new local session.

## Why the account restriction risk remains

The script uses the official Telegram API through the third-party Telethon
client, but this does not provide immunity from anti-spam restrictions.
Telegram monitors API abuse and may temporarily restrict actions or ban an
account. The risk depends on the account history, batch size, frequency, and
other undisclosed factors. In the
[official API ID instructions](https://core.telegram.org/api/obtaining_api_id),
Telegram explicitly warns that third-party API clients are monitored to prevent
abuse.

The project's safeguards - dry runs, rechecks, a limit of 10, delays of 15 to
30 seconds, a progress log, and immediate termination on a server restriction -
reduce the chance of mistakes and aggressive behavior. They do not guarantee
account safety.

## Publishing on GitHub

The repository uses the `main` branch and includes the
`.github/workflows/ci.yml` workflow. On every push and pull request, GitHub
Actions installs dependencies, runs Ruff and unit tests, and verifies Python
compilation.

1. Create an empty GitHub repository without automatically adding a `README`,
   `.gitignore`, or license.
2. Add it as a remote and push the branch:

```bash
git remote add origin git@github.com:OWNER/telegram-poll-cleanup.git
git push -u origin main
```

You can use the HTTPS repository URL instead of SSH. `.env`, session files,
participant exports, logs, local exclusions, and the virtual environment are
excluded through `.gitignore` and must not be uploaded to GitHub.

No license is included intentionally. Choose an appropriate license before
publishing this as a public open-source repository. A private repository does
not require one.
