# Incident Suite demo video script

**Owner:** Safwan  
**Target duration:** 2:30–2:55  
**Hard limit:** 3:00  
**Recording target:** deployed Streamlit app, 1080p, clean browser profile  
**Primary demo:** critical payment database incident, followed by repeat-run memory and scale  
**Source requirements:** `docs/build-plan.md` sections 3, 4, 12.3, 18.2, 18.3, 18.4, and 21

The recording must show the product working, not only describe it. Replace bracketed
placeholders with measured values from the final run. If an integration or feature has
not landed, do not claim it on camera; use the approved fallback in the production notes.

## 1. Story and claims

The video demonstrates this path:

1. Upload or select a log.
2. Redact secrets before analysis.
3. Parse and group the log into issues with evidence.
4. Use remediation and the feedback loop to find runbook-backed fixes.
5. Write an actionable checklist.
6. Pause for human approval before sending anything.
7. Post a Slack summary and thread, and create or update the JIRA ticket.
8. Run the same incident again to show memory, then show the 50,000-line result.

Do not show credentials, `.env` files, API responses containing secrets, personal Slack
messages, or unrelated browser tabs.

## 2. Timed script and shot list

| Time | Picture / action | Voice-over | Production notes |
|---|---|---|---|
| 0:00–0:15 | Full-screen scrolling log text, then cut to the Incident Suite title | “It is 3 AM, the pager goes off, and there are fifty thousand lines of logs. Incident Suite gives the first pass to a team of AI agents that finds the problems, suggests fixes, and tells the team what to do next.” | Start on the problem. No logo intro. Keep text readable. |
| 0:15–0:35 | README architecture diagram; highlight Upload, Log Reader, Remediation, Cookbook, JIRA, and Slack | “A LangGraph orchestrator coordinates five agents. The Log Reader parses and classifies, Remediation finds a runbook-backed fix, the Cookbook writes the checklist, and JIRA and Slack prepare the response.” | Use the final Mermaid diagram. Keep the cursor deliberate. |
| 0:35–0:48 | App home screen; show dry-run mode and select critical sample #1 (`payment_db_conn_refused.log`) | “We will use a payment database incident. The public demo is dry run by default, so we can inspect every action before anything is sent.” | Show the integration status without exposing values. |
| 0:48–1:10 | Start the run; graph nodes light up; parsed table and redaction count appear | “Secrets are redacted before the Log Reader sees the data. The parser keeps the evidence lines, groups repeated errors into patterns, and gives each issue a category and severity.” | Pause on `[REDACTED:<type>]`, counts, and two evidence rows. |
| 1:10–1:22 | Remediation result and runbook source; show feedback edge when available | “Remediation matches each issue to a runbook with a rationale. When confidence is not high enough, it asks the Log Reader for more context instead of guessing. That is the feedback loop.” | Show the actual runbook title and evidence. If not landed, show the planned graph and say the step is part of the final flow only when implemented. |
| 1:22–1:30 | Cookbook checklist and prepared Slack/JIRA previews | “The fixes become an ordered checklist: do now, do this week, and prevent next time. The notification and ticket payloads are prepared, but they are not sent yet.” | Hold long enough to read one checklist item. |
| 1:30–1:38 | Approval panel; click **Approve** | “Nothing leaves the app until a person approves it.” | Make the approval gate the visual focus. |
| 1:38–1:55 | Slack summary, ticket button, and thread checklist; then JIRA ticket | “Slack receives the summary and the full checklist in a thread. Critical issues get a JIRA ticket with the evidence, fix, and links back to Slack and the trace.” | Use a disposable workspace/project. Cut network waits. |
| 1:55–2:08 | Move JIRA ticket to Done; show the automation message in `#incident-alerts` | “When the ticket is resolved, the Jira automation reports that change back in the incident channel.” | Show only the test channel. If automation is unavailable, use a prepared screenshot and label it clearly. |
| 2:08–2:22 | Run the same log again; show existing ticket comment and updated count | “Run the same incident again and there is no duplicate ticket. The open ticket gets a comment and the Slack summary reports that the issue was seen again.” | Use the real ticket key and count from the run. |
| 2:22–2:38 | Select the 50,000-line sample; show result metrics | “The same pipeline also handles the large sample: [line count] lines become [pattern count] patterns in [time], using [token count] tokens at a cost of [cost].” | Read measured values from the final app; never invent them. |
| 2:38–2:50 | README evaluation table, green CI badge, repository and live-demo links | “We tested the labelled samples, the parser, redaction, upload limits, and the integration path. The code is public, tested, and live. The links are below.” | Show actual evaluation results and URLs. |
| 2:50–2:55 | End card with repository and live-demo URLs | “Incident Suite turns noisy incident logs into an approved, traceable response.” | Hold the end card for at least three seconds. |

### Voice-over fallback for unavailable features

If the final build does not include a feature, do not imply that it works. Replace that
shot with the dry-run preview or a README architecture frame and say:

> “This is the prepared response in the current prototype. The live integration is
> disabled in the public demo, so no external action is sent.”

## 3. Recording preparation

### App and data

- [ ] Confirm the deployed commit is the one being demonstrated
- [ ] Wake the app at least ten minutes before recording
- [ ] Run each selected sample once to warm the cache
- [ ] Confirm the critical sample, repeat-run result, and large-log metrics are real
- [ ] Confirm redaction examples use fake values only
- [ ] Keep the public deployment in dry run unless Raphael authorizes a disposable live test

### Slack and JIRA

- [ ] Use a disposable test project and `#incident-alerts`
- [ ] Remove or label old test tickets so the repeat-run story is unambiguous
- [ ] Open Slack to the bottom of the test channel
- [ ] Mute notifications and hide personal DMs
- [ ] Confirm the JIRA ticket contains no private data
- [ ] Confirm ticket links and trace links resolve before recording

### Screen and audio

- [ ] Use a clean browser profile at 110% zoom
- [ ] Close unrelated tabs and hide the bookmarks bar
- [ ] Do not show keys, `.env`, Streamlit secrets, terminal history, or personal information
- [ ] Record at 1080p with readable text and a steady cursor
- [ ] Check the microphone in a quiet room
- [ ] Record a full screen take and a voice-over take if separate editing is safer
- [ ] Record a backup take before the final edit

## 4. Editing and export

- [ ] Cut waits longer than two seconds, including model and JIRA automation waits
- [ ] Keep the total duration between 2:30 and 2:55 and below the three-minute limit
- [ ] Enable captions and manually check `LangGraph`, `JIRA`, `Slack`, and `redacted`
- [ ] Keep transitions simple; prioritize readable evidence over effects
- [ ] Blur or remove any accidental secret, token, personal message, or unrelated ticket
- [ ] End with repository and live-demo links visible for at least three seconds
- [ ] Export a 1080p MP4 and verify the exported file from start to finish
- [ ] Upload unlisted to YouTube or Loom
- [ ] Add the final video URL beside the live-demo URL at the top of `README.md`
- [ ] Re-open the uploaded video in a private/incognito window and verify playback

## 5. Final handoff

Record these values in the submission checklist:

| Item | Value |
|---|---|
| Final video URL | |
| Final video duration | |
| Deployed app URL | |
| Demonstrated commit SHA | |
| Repository URL | |
| Screenshot folder / links | |
| Known limitations disclosed | |
