---
name: gui-smoke
description: Walk the pre-release GUI smoke checklist — launch the desktop app from source and verify the manual checklist in docs/superpowers/gui-smoke-checklist.md, reporting pass/fail per item.
disable-model-invocation: true
---

# GUI smoke test

Run the manual smoke checklist before a release or after risky GUI/daemon
changes.

## Procedure

1. Read `docs/superpowers/gui-smoke-checklist.md` — that file is the
   authoritative, up-to-date checklist. Do not work from a memorized copy.

2. Launch the app from source:
   ```bash
   uv run email-export-import-gui
   ```
   The app may spawn/attach to the `eei-daemon` background process — note in
   the report whether it connected to the daemon or fell back to in-process.

3. Walk the checklist top to bottom. Items you can verify yourself (app
   launches, dashboard renders, daemon rendezvous appears in
   `~/.email-export-import/`), verify. Items that need live IMAP accounts or
   human eyes (language toggle, wizard flow, pause/resume against a real
   server), hand to the user one at a time and record their answer — do not
   mark them passed on assumption.

4. Anything ambiguous or failing: capture the exact symptom (screenshot,
   console output, daemon log) before moving on.

## Report

A checkbox list mirroring the checklist file: ✅ pass / ❌ fail (with symptom)
/ ⏭ not testable now (with reason). End with a clear go / no-go for release.
Never report go with an unexplained ❌ or ⏭ on a resume/duplicate-safety item —
those are the tool's core guarantees.
