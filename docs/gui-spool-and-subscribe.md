# GUI integration notes: disk spool + folder subscribe

Engine changes the desktop GUI must surface. The engine work is done and
tested — this document lists only the GUI-side changes.

## 1. Folder subscribe (no GUI work needed)

`migrate()` now subscribes every destination folder during planning.
Roundcube-style webmail lists only SUBSCRIBEd folders, so migrated folders
were invisible until now. Nothing to change in the GUI; existing runs pick
this up automatically on their next resume.

## 2. Optional disk spool (GUI work required)

New optional `spool` parameter on `migrate()`:

```python
from email_export_import.spool import MessageSpool

spool = MessageSpool.for_pair(src_email, dst_email, base_dir=None)  # or None
result = migrate(src, dst, plans, state, ..., spool=spool)
```

Semantics: each downloaded message body is written to disk
(`~/.email-export-import/spool/<src>__<dst>/<folder>/<uid>.eml` + `.json`
sidecar) and deleted right after its successful upload. Only failed uploads
accumulate; the next run uploads them from disk without re-downloading.
`spool=None` (default) keeps the current stream-through-memory behaviour.

GUI changes:

1. **Settings screen**: add a checkbox, default OFF.
   - `i18n` keys (add to BOTH locale files — the key-set equality test will
     fail otherwise), suggested:
     - TR: "Mesajları yükleme tamamlanana kadar diskte tut (başarısız
       yüklemeler tekrar indirilmeden diskten denenir)"
     - EN: "Keep downloaded messages on disk until uploaded (failed uploads
       retry from disk without re-downloading)"
2. **`Controller.start(...)`**: accept `spool: bool`, build
   `MessageSpool.for_pair(...)` when true, pass it to `migrate()`, and add
   `"spool": spool` to the `state.set_config(...)` dict (the CLI already
   writes/reads this key — keep the name identical for session
   compatibility).
3. **Resume prefill**: when loading a session, prefill the checkbox from
   `state.config.get("spool", False)`.
4. **Summary screen**: after a run with failures and spool enabled, show
   `spool.pending_count()` — "N messages kept on disk for retry".

Reference implementation: `email_export_import/cli.py` (search for
`use_spool` / `message_spool`) and tests in `tests/test_spool.py`,
`tests/test_transfer.py::test_spool_reuploads_from_disk_without_redownload`,
`tests/test_cli.py::test_spool_flag_saved_and_spool_dir_used`.
