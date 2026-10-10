# Manual test checklist

Run this after every deploy to Streamlit Cloud, and once locally before the final submit.
Copy it into a GitHub issue titled `Test run <date> <time>` and tick the boxes as you go.
File a separate issue for each failure (see "Bug report" at the bottom).

Deploy tested: `<commit SHA from the app footer or GitHub>`   Tester: `<name>`   Browser: `<Chrome/Edge + OS>`

Items marked *(later)* only apply once that feature has landed (build plan section 10 says
when). Skip them until then.

## 1. App starts
- [ ] The app link opens. If it was asleep, the wake-up takes under 2 minutes
- [ ] Title "Incident Suite" shows, with no red error box
- [ ] The sidebar shows Models / Slack / JIRA as connected or not connected, and **never shows a key**
- [ ] The mode shows "dry run" on the public app

## 2. Sample logs *(later, from H6)*
- [ ] Every sample in the picker runs to the end without an error
- [ ] Critical sample (#1): the graph lights up node by node, and the JIRA node runs
- [ ] Healthy sample (#13): JIRA is skipped, and the app says nothing would be sent to Slack
- [ ] Feedback loop: at least one sample shows Remediation sending an issue back to the Log Reader
- [ ] The 50k sample finishes, and shows its line count, pattern count, tokens and cost
- [ ] Each issue lists evidence line or row numbers that match the file (spot-check 2)

## 3. Upload *(later, from H2 to H6)*
- [ ] Each format uploads and is detected correctly: .log, .txt, .jsonl, .json (array), .json (`{"logs": [...]}`), .csv, .tsv, .parquet, .gz
- [ ] A CSV renamed to `.log` is still detected as CSV, and the app says so
- [ ] A file over 5 MB is refused with a clear message
- [ ] A `.zip`, `.exe` or `.xlsx` is refused with a clear message
- [ ] An empty file gives a clear message, not a crash
- [ ] A file with a 100 KB single line is accepted, and the line is cut
- [ ] A gzip that unpacks to more than 50 MB is refused (ask Ashvin for the test file)

## 4. Redaction *(later, from H2)*
- [ ] Upload `planted_secrets.log` (your redaction examples). Every secret shows as `[REDACTED:<type>]` in the parsed table, the evidence and the Slack/JIRA previews
- [ ] The redaction count is shown, e.g. "3 emails, 1 AWS key redacted"
- [ ] No planted secret appears anywhere on the page (Ctrl+F for each one)

## 5. Dry run and approval *(later, from H4 and H6)*
- [ ] Dry run: the Slack and JIRA previews show, and the run ends at the approval gate with nothing to approve
- [ ] Live mode on (local or demo only): Approve sends once; Reject sends nothing, and the rejection shows in the run log
- [ ] Refreshing the page while waiting for approval keeps the paused run

## 6. Live Slack and JIRA (with Raphael, local or demo only) *(later, from H5)*
- [ ] New critical issue: one ticket is created, and the Slack summary has a "View ticket" button
- [ ] Same log again: no new ticket, the existing ticket gets a comment, and Slack says "updated, seen N times"
- [ ] Close the ticket, run again: a new ticket labelled `regression`, linked to the old one
- [ ] Assign or resolve the ticket in JIRA: the automation posts in `#incident-alerts`
- [ ] The Slack thread has the full checklist. The JIRA ticket links back to Slack and to the trace
- [ ] A wrong JIRA token: the Slack summary still posts, with a warning that tickets failed
- [ ] Take screenshots of each step for the README

## 7. Errors and limits
- [ ] No raw Python stack trace appears anywhere on screen
- [ ] Running many times quickly hits the rate limit with a friendly message *(later)*
- [ ] Works at a phone-width window (or just doesn't break)

## 8. README fresh clone (H9, Windows)
- [ ] Follow the README "Run it locally" section word for word on a machine that has never had the repo
- [ ] `make setup` (or the pip steps), `make test` and `make run` all work. Note every step where you had to guess

## Bug report (one GitHub issue per bug)

```
Title: [bug] <what broke, in a few words>
Deploy / commit: <SHA>
Steps: 1. ... 2. ... 3. ...
Expected: ...
Actual: ...
Screenshot: <paste>
Severity: blocks demo / wrong output / cosmetic
```
