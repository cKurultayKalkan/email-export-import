---
name: release
description: Cut a release — bump the version in both files, update CHANGELOG.md, run the full test suite, tag and push so CI builds and publishes the signed bundles.
disable-model-invocation: true
---

# Release

Cut release `vX.Y.Z` of email-export-import. The version to release is given
as the argument; if none is given, propose the next patch version and confirm
with the user before proceeding.

## Steps — in this order

1. **Preflight.** Working tree must be clean (`git status`) and on `main`.
   Stop and report if not.

2. **Version bump — two files, must match** (`tests/test_version.py` enforces):
   - `pyproject.toml` → `[project] version`
   - `email_export_import/__init__.py` → `__version__`

3. **CHANGELOG.md.** Ensure a `## vX.Y.Z — YYYY-MM-DD` section exists at the
   top describing this release (sections: `### Added` / `### Changed` /
   `### Fixed`, user-facing wording, bold lead-in per bullet — match the
   existing entries). Draft it from `git log <last-tag>..HEAD` if missing,
   and show it to the user before committing.

4. **Full test suite**: `uv run pytest`. All tests must pass — no skipping
   this step, no releasing with failures.

5. **Commit** the bump (`chore(release): vX.Y.Z`) and push `main`.

6. **Tag and push the tag** — this is what triggers the build:
   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```
   Push uses the cKurultayKalkan SSH identity. Do NOT use
   `gh workflow run` — it fails (no admin on the repo); the `v*` tag is the
   only trigger that publishes.

7. **Watch CI**: `gh run watch` on the `build-gui.yml` run for the tag.
   It builds macOS (signed + notarized DMG), Windows installer, Linux zip,
   the `eei-daemon` sidecar per OS, and publishes the GitHub release with
   `SHA256SUMS.txt`. Report the release URL when it is up.

## If CI fails

Do not delete/retag until the cause is understood. Common regressions are
documented in the workflow comments (flet pins its own Flutter; `yes |` +
`set +o pipefail` for the prompt; UTF-8 env on Windows; `shutil.make_archive`
for zips). Fix on `main`, then move the tag: `git tag -f vX.Y.Z && git push -f
origin vX.Y.Z`.
