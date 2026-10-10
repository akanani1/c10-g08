# Demo video: script outline (target 2:40, hard limit 3:00)

Build plan sections 12.3 and 18.5. Record on the **deployed app** at 1080p (OBS or Loom).
Upload unlisted to YouTube or Loom. Record a backup take early in H10, around 5 PM tomorrow.

The voice-over lines are drafts: say them in your own words, but keep the timing. Speak at
about 150 words a minute, which is roughly 25 words per 10-second block.

| Time | On screen | Voice-over (draft) | Notes |
|---|---|---|---|
| 0:00 to 0:15 | A wall of scrolling log text, then the app's title | "It's 3 AM, the pager goes off, and you have fifty thousand lines of logs. Incident Suite hands them to a team of AI agents that find the problems, suggest fixes, and tell your team." | Hook. No logo intro |
| 0:15 to 0:35 | Architecture diagram (Mermaid from the README) | "A LangGraph orchestrator runs five agents. The Log Reader parses and classifies, Remediation finds a fix from our runbooks, the Cookbook writes a checklist, and the JIRA and Slack agents take action." | Point at each agent as you name it |
| 0:35 to 1:20 | Live run: pick critical sample #1. The graph lights up and the parsed table fills | "We pick a log where the payment database went down. Secrets are redacted first. The Log Reader groups the lines into a few patterns... Remediation isn't sure about one issue, so it sends it back for more context: that's the feedback loop. Each fix cites its runbook and the exact log lines." | Pause on the feedback-loop moment. Zoom in on the evidence line numbers |
| 1:20 to 1:25 | Approval panel. Click **Approve** | "Nothing leaves the app until a person approves it." | Section 18.5 |
| 1:25 to 1:45 | Slack: summary with ticket buttons, checklist in the thread, JIRA preview card | "In Slack, the team gets the summary, a View ticket button, and the full checklist in the thread." | Slack window prepared and already scrolled |
| 1:45 to 2:05 | JIRA ticket: evidence and links back to Slack and the trace. Move it to Done, and Slack posts "resolved" | "The ticket carries the evidence and links back to Slack and the trace. When someone resolves it, Slack hears about it automatically." | The automation can take a few seconds. Cut the wait in editing |
| 2:05 to 2:25 | Run the same log again: "OPS-12 updated, seen 2 times". Then the 50k-line sample with its pattern count and cost | "Run it again and there's no duplicate. It comments on the open ticket. And it scales: fifty thousand lines become about twenty patterns, for under a cent." | Read the real numbers off the screen |
| 2:25 to 2:40 | Eval table in the README, green CI badge, repo and live links | "We tested it on 17 labelled logs. Here's the accuracy. The code is public, tested, and live. Links below." | End card with both links for 3 seconds |

## Shot list and prep

- [ ] Wake the app 10 minutes before recording, and run each sample you'll show once (this warms the cache)
- [ ] Live integrations on, for the demo only (Raphael), then off again afterwards
- [ ] JIRA board: no leftover tickets from earlier tests, or a fresh filter
- [ ] Slack: `#incident-alerts` scrolled to the bottom, notifications muted, no personal DMs visible
- [ ] Browser: clean profile, 110% zoom, no bookmarks bar, no other tabs
- [ ] Desktop: no keys, `.env` or secrets on screen at any point
- [ ] Mic check. Record in a quiet room
- [ ] Record the screen first, then the voice-over separately if that's easier

## Editing

- Cut every wait longer than 2 seconds (model calls, JIRA automation)
- Captions on (YouTube auto-captions are fine, but check the words "LangGraph" and "JIRA")
- Final length between 2:30 and 2:55
- Put the link in the README, under the title, next to the live demo link
