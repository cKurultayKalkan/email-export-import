# email-export-import

Migrate a mailbox from one IMAP server to another — folders, read/starred
flags, original dates, and attachments preserved. Interruptible and
resumable: run it again and it picks up where it left off without
re-copying migrated messages.

## Install

```bash
uv sync
```

## Usage

Interactive wizard:

```bash
uv run email-export-import
```

Pick the old provider (Gmail, Outlook/Office365, Yahoo, iCloud, Yandex, or
Custom host/port), enter the email address and an **app password**, repeat
for the new provider, review the folder plan, confirm.

Non-interactive:

```bash
EEI_SRC_PASSWORD=... EEI_DST_PASSWORD=... uv run email-export-import \
  --src-preset gmail --src-email old@gmail.com \
  --dst-host imap.newserver.com --dst-email new@newserver.com \
  --yes
```

## Notes

- **App passwords:** Gmail, Outlook, Yahoo, iCloud, and Yandex all reject
  normal passwords over IMAP. The wizard shows the provider's app-password
  page before asking.
- **Gmail:** `[Gmail]/All Mail`, `[Gmail]/Important`, and `[Gmail]/Starred`
  are skipped by default — they are label views that would duplicate every
  message. Override with `--skip`.
- **Resume:** state lives in `~/.email-export-import/state/`. Interrupt
  with Ctrl-C anytime; re-run with the same accounts to resume. Override
  the location with `--state-dir`. Deleting the state directory makes the
  tool re-copy everything (duplicates on the destination). In the rare
  case a connection drops in the middle of a single upload, that one
  message may be duplicated — the resume state prevents duplicates across
  runs, not within a dropped upload.
- **Quota:** if the destination fills up, the run aborts immediately with
  a clear message; free space and re-run to resume.

## Development

```bash
uv run pytest
```
