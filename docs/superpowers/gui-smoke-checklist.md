# GUI manual smoke checklist

Run before each release, on at least one platform:

- [ ] `uv run email-export-import-gui` opens the welcome screen
- [ ] Language toggle switches every visible string (TR ↔ EN)
- [ ] Unfinished CLI session appears in the resume list; Resume asks only passwords
- [ ] Preset dropdown fills host/port/SSL; Custom leaves them editable
- [ ] Wrong password shows the auth error text (no crash, no traceback)
- [ ] Self-signed server raises the certificate dialog; Continue connects
- [ ] Plan screen counts match the mailbox; unchecking a folder lowers the total
- [ ] Progress advances (counter + bar + folder name)
- [ ] Cancel stops within a few seconds; relaunching offers to resume
- [ ] Done screen shows summary; failures listed when present
- [ ] Killing the app mid-run and relaunching resumes without duplicates
