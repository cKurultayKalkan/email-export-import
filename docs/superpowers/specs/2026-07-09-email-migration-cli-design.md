# email-export-import — Design Spec

**Date:** 2026-07-09
**Status:** Approved

## Purpose

Interactive Python CLI that migrates a mailbox from server A to server B over IMAP, preserving folder structure, flags, original dates, and attachments. Resumable and idempotent: re-running after an interruption skips already-migrated messages and never creates duplicates.

## Scope

- **In:** IMAP→IMAP migration, provider presets, interactive wizard, resume/dedup, per-message error tolerance, optional non-interactive flags.
- **Out (YAGNI):** POP3, OAuth2, calendars/contacts, GUI, continuous delta-sync daemon.

## Stack

| Concern | Choice |
|---|---|
| Language | Python 3.11+ |
| IMAP transport | `IMAPClient` |
| CLI framework | `Typer` |
| Terminal UI | `Rich` (prompts, tables, progress bars) |
| Tests | `pytest` |
| Package manager | `uv` |

## Architecture

```
email_export_import/
  cli.py          # Typer app, wizard flow, wires everything
  providers.py    # preset registry: Gmail/Outlook/Yahoo/iCloud/Yandex/Custom
  connection.py   # IMAPClient wrapper: connect, login, auto-reconnect, retry
  folders.py      # folder listing, delimiter translation, SPECIAL-USE mapping
  transfer.py     # core engine: fetch → append, flags + internaldate preserved
  state.py        # resume state: JSON per (src,dst) pair, dedup index
  models.py       # dataclasses: Account, ProviderPreset, TransferProgress
  errors.py       # typed exceptions
tests/
```

Each module has one job, communicates through typed interfaces (dataclasses in `models.py`), and is unit-testable without the network except `connection.py` and the integration path of `transfer.py`.

## Provider Presets (`providers.py`)

Registry of presets; each carries `host`, `port`, `ssl`, an app-password hint (shown before the password prompt, with a link to the provider's app-password page), and an optional folder skip-list.

| Preset | Host | Notes |
|---|---|---|
| Gmail | `imap.gmail.com:993` | App-password hint. Skip-list: `[Gmail]/All Mail`, `[Gmail]/Important`, `[Gmail]/Starred` (labels duplicate messages; All Mail contains everything — migrating it alongside label folders would copy every message twice or more). Skip-list shown at confirm step, user-editable. |
| Outlook/Office365 | `outlook.office365.com:993` | App-password hint. |
| Yahoo | `imap.mail.yahoo.com:993` | App-password hint. |
| iCloud | `imap.mail.me.com:993` | App-password hint. |
| Yandex | `imap.yandex.com:993` | App-password hint. |
| Custom | user-entered | Host/port/SSL prompted. |

Selecting a preset pre-fills host/port/SSL; the user may override any field.

## Wizard Flow (`cli.py`)

1. **Source:** pick preset (or Custom) → host/port/SSL pre-filled, overridable → enter email + app-password (masked prompt).
2. **Test connect** to source; show folder list with message counts (via `STATUS`).
3. **Destination:** same flow as source.
4. **Confirm:** show migration plan — folder count, approximate message count, source→destination folder mapping, active skip-list. User can edit skip-list here.
5. **Run:** per-folder transfer with Rich progress (per-folder bar + overall bar).
6. **Summary:** migrated / skipped / failed counts; list of failed messages (folder + subject/Message-ID).

**Non-interactive mode:** all wizard inputs also accepted as CLI flags/env vars (password via env var only, never argv). Same engine; wizard is skipped when required flags are present.

## Transfer Engine (`transfer.py`)

Per folder:

1. Ensure destination folder exists (create if missing, after name translation — see Folders).
2. Fetch **cheap metadata batch** for all messages: `UID`, `RFC822.SIZE`, `FLAGS`, `INTERNALDATE`, `BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]`. No bodies yet — keeps memory flat.
3. For each message not already in state: fetch full raw body (`BODY.PEEK[]`) individually, or in small size-bounded batches; `APPEND` to destination with original flags and internaldate; record in state; release the body from memory before the next fetch.
4. Preservation is automatic: the raw RFC822 message is appended verbatim, so attachments, MIME structure, and headers survive untouched.

**Flags:** re-apply `\Seen`, `\Flagged`, `\Answered`, `\Draft`, `\Deleted`; never `\Recent` (server-managed).
**Date:** `INTERNALDATE` passed to `APPEND` so the destination keeps the original date.

## Folders (`folders.py`)

- **Delimiter translation:** read each server's hierarchy delimiter from `LIST`/`NAMESPACE`; translate paths (e.g. source `Work/Projects` → destination `Work.Projects` when the destination uses `.`).
- **Special folders:** map Sent/Drafts/Trash/Junk/Archive via RFC 6154 SPECIAL-USE attributes when the server advertises them; fall back to well-known name matching otherwise. Unknown folders are created 1:1 (after delimiter translation).
- **UTF-7 folder names:** handled by IMAPClient.

## State / Resume / Dedup (`state.py`)

- Location: `~/.email-export-import/state/<src-email>__<dst-email>.json`. Directory `chmod 700`, files `chmod 600` (contain email addresses; never passwords).
- Per folder, state records:
  - the folder's `UIDVALIDITY` at time of writing,
  - the set of migrated **Message-IDs**,
  - for messages lacking a Message-ID, the source **UID**.
- **Resume:** on re-run, a message is skipped if its Message-ID is in state, or (for ID-less messages) its UID is in state **and** the folder's `UIDVALIDITY` is unchanged. If `UIDVALIDITY` changed, UID-based entries for that folder are discarded (Message-ID entries remain valid).
- **Known trade-off:** two distinct messages sharing one Message-ID within the same folder → second is skipped. Rare; accepted.
- State is written incrementally during the run (flush after each append or small batch), so Ctrl-C loses at most the last batch.

## Connection Handling (`connection.py`)

- Wrapper over IMAPClient: connect, login, and friendly error translation (DNS fail, TLS fail, auth rejected → human messages, wizard offers retry/edit instead of crashing).
- **Auto-reconnect:** long transfers outlive server idle timeouts (Gmail drops sessions after minutes). On dropped connection: reconnect, re-select folder, retry the in-flight message up to 3 times before counting it failed.

## Error Handling

| Failure | Behavior |
|---|---|
| Connect/login fails in wizard | Friendly message; user retries or edits host/port. No crash. |
| Single message fetch/append fails | Retry 3 times (with reconnect); then log, count failed, continue. One bad message never kills the run. |
| Destination quota exceeded | Detect quota-related `APPEND` errors (e.g. `OVERQUOTA`) → abort early with clear message instead of failing every remaining message for hours. |
| Ctrl-C | Flush state, exit cleanly, print resume hint. |
| Message too large for destination | Per-message failure (logged in summary). |

## Security

- Passwords: masked prompt (interactive) or env var (non-interactive). Held in memory only — never logged, never written to state or disk.
- State directory `700` / files `600`.
- TLS by default (port 993); plain connections only if the user explicitly configures a Custom preset that way.

## Testing

- **Unit (no network):** provider registry; delimiter/special-folder mapping; state dedup logic including UIDVALIDITY invalidation; quota-error detection.
- **Integration:** transfer engine against a local Dovecot in Docker (fallback: mocked IMAPClient fixture). Cases: one folder round-trip with flags + internaldate preserved; attachment survives byte-identical; re-run skips everything; interrupted run resumes without duplicates.

## Open Questions

None — all resolved during brainstorming.
