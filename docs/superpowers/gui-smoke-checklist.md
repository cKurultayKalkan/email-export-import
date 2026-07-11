# GUI manual smoke checklist

Run before each release, on at least one platform:

- [ ] `uv run email-export-import-gui` opens the dashboard (empty state or paused cards)
- [ ] Language toggle switches every visible string (TR ↔ EN)
- [ ] An unfinished CLI session appears as a paused card; Resume asks only the two passwords
- [ ] "+ New migration" wizard: preset fills host/port/SSL; Custom stays editable
- [ ] "Test connection" shows a spinner and the UI stays responsive while it runs
- [ ] Wrong password shows the auth error text (no crash, no traceback)
- [ ] Self-signed server raises the certificate dialog; Continue retries without freezing
- [ ] Plan screen counts match the mailbox; unchecking a folder lowers the total
- [ ] Starting a migration returns to the dashboard with a live progress card
- [ ] A second migration can be started while the first runs; both cards update
- [ ] Pause stops the run within a few seconds (longer only if the server is rate-limiting reconnects); card shows Paused
- [ ] Resume after pause continues without duplicates
- [ ] Cancel is terminal; Dismiss removes the card and it stays gone on relaunch
- [ ] Detail screen shows live progress and failures; Back keeps the run going
- [ ] Killing the app mid-run and relaunching shows the run as paused; resume completes without duplicates
