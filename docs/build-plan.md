# Build plan: TriageGraph (c10-g08)

Version 12, 11 Oct 2026.

**Deadline:** 7 PM IST, Sunday 11 Oct 2026. We submit by 6:30 PM. Feature freeze is at 3 PM and we record the video at 4 PM.

**Status:** everything is merged: H1 to H8 (the whole core), extras 1 to 6, the UI review fixes, the new layout, the Admin Console, the TriageGraph name and several samples at once. What's left is the release work in section 16. Section 16 has the full timeline.

This is the single, current version of the plan. When something changes, we edit the section it belongs to and add a line to the decisions log (section 21). Older versions are in the Git history.

---

## 1. Goal and how we're judged

| # | Criterion | What we're doing | What a judge can check |
|---|---|---|---|
| 1 | Clean code: no dead code, no bugs, no security risks | Ruff, mypy, Vulture, Bandit, pip-audit and gitleaks in CI, plus tests that need no API key | Green CI badge on the README. The CI commands pass on a fresh clone |
| 2 | Public repo | github.com/akanani1/c10-g08, MIT licence, one pull request per feature | The repo, with no secret in any commit |
| 3 | 2 to 3 minute demo video | Script in `docs/demo-video-script.md`, recorded on the deployed app | Video link at the top of the README |
| 4 | Working prototype, deployed | Streamlit Community Cloud | Live link at the top of the README. The sample logs work with no setup |
| 5 | README that covers everything, including local setup | Tested by someone who didn't write the code, from a fresh clone | The "Run it locally" section |

---

## 2. What we're building

A Streamlit app. You upload an ops log, or pick one of our samples, and a set of agents works through it.

| Agent | What it does |
|---|---|
| Log Reader / Classifier | Parses each line into fields, groups repeated errors into patterns, and labels each issue with a category and a severity |
| Remediation | Finds a fix for each issue, says why, and cites the runbook it came from. If it isn't confident, it asks for more of the log and tries once more |
| Cookbook Synthesizer | Turns the fixes into a checklist: do now, this week, prevent next time |
| JIRA Ticket | Opens a ticket for each critical issue, or comments on the existing one if we've seen it before |
| Notification | Posts a summary to Slack, with the checklist and fixes in a thread and links to the tickets |
| Orchestrator | The LangGraph graph. It holds the shared state and decides what runs next |

**Core scope** (must ship): the six agents above, secret redaction, every log format in section 7, the approval step before anything is sent, JIRA as memory between runs, the results screen, the eval and the deploy.

**Extras** (section 17): an agent settings page, an assistant in the sidebar, and a few smaller features. We build them in a fixed order after the core, and whatever isn't done by the 3 PM freeze is dropped.

---

## 3. Stack

| Layer | Choice | Why |
|---|---|---|
| UI | Streamlit | One language, live status per agent, the fastest way to a decent demo |
| Orchestration | LangGraph | Conditional routing, the retry loop, a parallel step, and a pause for approval |
| Model calls | LangChain through OpenRouter | One key for chat models and embeddings. LangChain's OpenAI classes work against it by changing the base URL |
| Runbook search | LangChain `InMemoryVectorStore` | 15 short runbooks, embedded once when the app starts. No database to run |
| Tracing | LangSmith (optional) | Free tier. The trace link goes into Slack and JIRA |
| Slack | Slack app with a bot token (Web API) | Threads and message links, which a plain webhook can't do |
| JIRA | Jira Cloud REST API v3, free site | Search, create, comment, link |
| No-code glue | Jira Cloud app for Slack, and two Jira Automation rules | JIRA changes show up in Slack without us running a backend |
| Hosting | Streamlit Community Cloud | Allowed by the brief, and deploys straight from GitHub |
| Quality | Ruff, mypy, pytest, Vulture, Bandit, pip-audit, gitleaks, pre-commit, GitHub Actions | Criterion 1 |

**Models.** These are the only ones the app accepts. The list and the prices live in `incident_suite/config.py`, and the cost panel and settings page use the same list.

| Model | Used for | Price per million tokens (in / out) |
|---|---|---|
| `openai/gpt-4.1-mini` | Log Reader and Cookbook (default) | $0.40 / $1.60 |
| `openai/gpt-5-mini` | Remediation (default) | $0.25 / $2.00 |
| `openai/gpt-5-nano` | Allowed, for cheap tests | $0.05 / $0.40 |
| `google/gemini-2.5-flash` | Allowed | $0.30 / $2.50 |
| `openai/text-embedding-3-small` | Runbook search | $0.02 |

Embeddings are created with `check_embedding_ctx_length=False`, so LangChain sends plain text rather than OpenAI token IDs. Other providers can reject the token IDs.

**Not using:** LlamaIndex, LanceDB, FastAPI, Next.js, Vercel, Hugging Face Spaces, Docker. We don't need them, and leaving them out means fewer moving parts and less code to keep clean.

---

## 4. How a run works

```
Upload -> Redact -> Log Reader -> Remediation -> [Cookbook || JIRA plan*] -> Notification plan -> Approval** -> Send -> Results
                                     ^     |
                                     +widen+   (at most one retry per issue)

*  only when an issue reaches the ticket severity (critical by default)
** pauses only when something would really be sent
```

1. **Upload.** The file is checked (type and size), read in whatever format it's in (section 7), and every line is redacted before anything else sees it.
2. **Log Reader / Classifier.** Lines are grouped into patterns and the model labels the issues (section 5.1).
3. **Remediation.** It searches the runbooks for each issue and suggests a fix. If it isn't sure, the issue goes to `widen`, which fetches 10 more lines on either side of each evidence line, and Remediation tries once more. You can see the loop happen in the graph.
4. **Cookbook and JIRA plan run side by side.** JIRA plan only runs if at least one issue is critical.
5. **Notification plan** runs once both are finished, so the Slack message can include the checklist and the ticket links.
6. **Approval.** The JIRA and Slack agents each work in two halves. The plan half only reads and decides what would be sent. The send half does the writing. Between the two halves the graph pauses with LangGraph's `interrupt()`, and the app shows exactly what would go out. Approve sends it. Reject sends nothing and records the rejection.
7. **Results.** Everything ends up on the results screen (section 9).

**When the run pauses.** It only pauses when live integrations are on, the credentials are there, and the run found something worth sending. In dry run (the default), the run ends at the approval step with the previews on screen and nothing to approve. Setting `REQUIRE_APPROVAL=false` sends straight away.

**How it's built.** The graph is in `incident_suite/graph.py`. The state holds what the run produces: issues, fixes, the checklist, ticket and Slack plans and outcomes, errors, cost per agent, and one step record per node. Things that stay fixed during a run travel in LangGraph's runtime context instead: the parsed log, the run settings, and the model, runbook, JIRA and Slack services. That way the 50,000-line log isn't copied into every checkpoint. The checkpointer is `InMemorySaver`, the thread ID is the run ID, and the app keeps the compiled graph in `st.session_state`, so a Streamlit rerun doesn't lose a paused run.

You can do a full dry run from the command line: `python -m incident_suite.graph <log file>`. Add `--live` to send for real. It only works with `LIVE_INTEGRATIONS=true` and the credentials set, and it asks on the terminal before sending, just like the approval panel.

**Which runs get real clients.** `connect_services()` in `graph.py` gives the run a JIRA client and a Slack client only when the run is live and that service's credentials are all set. Otherwise the run gets none, and it stays a dry run.

---

## 5. The agents

Every agent takes and returns Pydantic models. Model output is checked against a JSON schema, with one retry if it doesn't match. Prompts live in `incident_suite/prompts/`, one file per agent, and log text always goes inside tags, as data and never as instructions.

### 5.1 Log Reader / Classifier

- **Parse.** Each line becomes a `LogLine`: line number, timestamp, level, service or host, message, error code and trace ID. It handles app logs, nginx access and error logs, Kubernetes and syslog, plus the structured formats in section 7.
- **Group.** Numbers, IDs and IPs are masked out so repeated errors share a signature. For example, `Connection refused to db:<port>` seen 412 times, with the first and last time and a few example lines. Each signature also gets a 12-character hash, which JIRA uses to spot repeats (section 6.3).
- **Filter.** It keeps WARN, ERROR and FATAL patterns, 3 lines of context around them, and any deploy or config-change lines. A change just before the errors is often the cause.
- **Classify.** The model gets a compact digest and returns issues with a title, category, severity, confidence, evidence lines and affected service. Big digests are split into chunks of about 6,000 tokens and the results are merged by signature. Evidence lines the model didn't actually see are dropped, and each issue gets an ID (`ISS-1`, `ISS-2`, and so on).

### 5.2 Remediation

- Searches the runbooks (section 8) for each issue and passes the matching runbooks to the model in full, so it sees the "Fix now" steps.
- Returns the fix steps, the rationale, a likely root cause (one or two sentences, framed as a hypothesis, citing line numbers), the sources and a confidence.
- Sources can only be runbooks that were actually retrieved. The model can't invent one.
- Asks for more context when no runbook matched, when the best match scores under 0.35, or when its own confidence is under the threshold (0.5 by default). Each issue gets one retry at most.

### 5.3 Cookbook Synthesizer

Turns all the fixes into one checklist with three groups: do now, this week, prevent. Each item names the issues it covers, items that point to unknown issues are dropped, and each group has at most 6 items.

### 5.4 JIRA Ticket

- **Plan (read-only):** for each critical issue, up to the ticket cap (3 by default, up to 5), look up the newest ticket with the same signature and decide whether to create a ticket, comment on an existing one, open a regression, or skip because the cap is reached.
- **Send (after approval):** create or comment, then link regressions to the old ticket. If the ticket is created but the link fails, the ticket still counts as sent and the error is shown.
- **Link back (after Slack):** add a remote link to the Slack summary on every ticket this run touched.
- The rules are in section 6.2.

### 5.5 Notification

- **Plan:** decide whether the run is worth a Slack post (at least one medium issue), then write the summary and the thread reply.
- **Send (after approval):** post the summary, reply in its thread, and get the message link.
- The content is in section 6.5.

---

## 6. Slack and JIRA

### 6.1 What talks to what

| Direction | How | Built by |
|---|---|---|
| App to JIRA | REST API: search, create, comment, link issues, add remote links | Our code |
| App to Slack | Bot token: post the summary, reply in the thread, get the message link | Our code |
| JIRA to Slack | Jira Automation rules post into the channel when a ticket is assigned or resolved | Set up in JIRA, no code |
| Slack to JIRA | Jira Cloud app for Slack: ticket previews, plus assign, comment and change status from Slack | Installed in Slack, no code |

The app doesn't hear about changes in Slack or JIRA as they happen. That would need a server running all the time, and Streamlit can't receive incoming requests. It does check JIRA at the start of each run, so a ticket someone closed changes what happens next time. JIRA is our only memory between runs, which means we don't need a database.

### 6.2 When a ticket or message is created

| What the run finds | JIRA | Slack |
|---|---|---|
| Nothing, or only low-severity issues | Nothing | Nothing. Results stay in the app, so the channel doesn't fill up with noise |
| Medium or high issues, nothing critical | Nothing | One summary, with the checklist in a thread |
| A new critical issue | A new ticket | Summary with a link to each ticket |
| A critical issue that already has an open ticket | A comment on it: seen again, run ID, count, time window | Summary says "SCRUM-12 updated, seen again" |
| A critical issue whose ticket is closed | A new ticket labelled `regression`, linked to the old one | Summary marks it as a regression |
| More critical issues than the ticket cap (3 by default, up to 5) | Tickets for the most severe, up to the cap | All listed, with the rest marked "not ticketed (over the ticket cap)" |
| Rejected at the approval step | Nothing | Nothing. The rejection is shown in the app |
| JIRA is down or the token is wrong | Nothing written, so nothing gets duplicated | Summary still posts, with a warning that tickets failed |
| Slack is down or the token is wrong | Not affected | The error shows in the app |
| Dry run (no credentials, or live switched off) | Nothing | Nothing. The app shows exactly what it would have sent |

### 6.3 How we spot a repeat

Each ticket carries two labels, `incident-suite` and `sig-<hash>`, where the hash comes from the pattern's signature (section 5.1). Before creating anything, the JIRA agent searches:

`project = SCRUM AND labels = "sig-<hash>" ORDER BY created DESC`

If the newest match isn't done, we comment on it. If it is done, we open a new ticket and link it to the old one as a regression ("relates to").

The search goes through `GET /rest/api/3/search/jql`. Atlassian has removed the older `/rest/api/3/search` from Jira Cloud (checked 10 Oct). We ask for one result and only the `status` field, and treat a ticket as done when its status category is `done`. The hash is checked against `[0-9a-f]{8,16}` before it goes into JQL, so nothing else can get into the query.

### 6.4 Order of calls after approval

1. JIRA: for critical issues up to the ticket cap, create or comment. Regressions are linked to the old ticket.
2. Slack: post the summary with `chat.postMessage` and keep the message's `ts`.
3. Slack: reply in that thread with the checklist and the fix for each issue.
4. Slack: get the message link with `chat.getPermalink`.
5. JIRA: on each ticket from step 1, add remote links to the Slack message and the LangSmith trace. The Slack link is in from H5. The trace link joins in H8, through the same call.
6. JIRA: on each ticket this run opened, store the Slack channel and `ts` as the issue property `incident_suite.slack`. A ticket that only got a comment keeps the thread where it was first reported. The assistant uses this (section 17.2).

Both clients send every call through one helper, `send_with_retry` in `integrations/__init__.py`:

- Every call has a timeout: 5 seconds to connect, 10 to answer.
- On a 429 or a 5xx it tries up to 3 times. It waits for `Retry-After` when the service sends a number of seconds, otherwise 1 second and then 2. It never waits more than 30 seconds, so the app doesn't hang.
- A failed connection is retried, because the request never arrived. A timeout while waiting for the answer is not: JIRA may already have created the ticket, and a duplicate is worse than one error.

If a call fails, the app shows the error and carries on rather than crashing. If the Slack summary posts but the thread or the link fails, the post still counts as sent and the error is shown. Error messages carry the status and the service's error code, never a token.

### 6.5 What goes into the messages

- **Slack summary:** the worst severity, the log name, the run ID, and up to 5 issues, each with its evidence line numbers, the first fix step and its ticket. Anything beyond that is counted as "+N more in the app".
- **Slack thread reply:** the checklist, then each fix with its root cause, steps and runbook sources.
- **JIRA ticket:**
  - Title: `[CRITICAL] <issue title> (<service>)`
  - Priority: Highest for critical, High for high
  - Labels: `incident-suite`, `sig-<hash>`, and `regression` where it applies
  - Description: what happened, evidence lines, fix steps, rationale, runbook sources and run ID
  - Remote links: the Slack thread and the trace

- **JIRA comment on an open ticket:** "Seen again in run <ID>", the pattern's count and time window ("Seen 412 times between 14:00:00 and 14:07:30"), and the evidence lines.

All log text is escaped before it goes into Slack or JIRA, and we never turn a URL found in a log into a link. In practice, `integrations/payloads.py` does this:

- In Slack, log text goes into `plain_text` blocks, so it can't mention `@channel` or fake a link. The only `mrkdwn` is our own ticket links, and any log text near them is escaped (`&`, `<`, `>`).
- In JIRA, log text goes into plain ADF text nodes with no link marks.
- In both, URLs in log text are defanged first (`https[:]//`, `www[.]`), and link previews are switched off in Slack.
- The only links we build point to our own tickets, the Slack message and the trace. JIRA remote links must be `https`.
- Long text is cut to Slack's limits (150 characters in the header, 3,000 per section, 45 sections) and to JIRA's 255-character title, with a note that the full text is in the app.

### 6.6 No-code setup (Raphael, about 20 minutes)

**Jira Cloud app for Slack**
- Install it from the Slack Marketplace and connect our Atlassian site.
- Invite it to `#incident-alerts`. Ticket links then show as preview cards with assign, comment and status buttons.
- Leave its own channel notifications off, so people don't get the same news twice.

**Jira Automation rules** (Project settings, then Automation)
1. **Resolved:** when an issue labelled `incident-suite` moves to Done, send "SCRUM-12 resolved by <name>: <summary>" to Slack.
2. **Picked up:** when an issue labelled `incident-suite` is assigned, send "SCRUM-12 picked up by <name>".

Both rules post through a Slack incoming webhook. It belongs to the same Slack app, but it's a different credential from the bot token. The free JIRA plan allows 100 rule runs a month, so we only use event rules and don't burn runs testing them. Our app's own API calls don't count towards that limit.

---

## 7. Log input

### 7.1 Formats

| Format | Extensions | How we read it |
|---|---|---|
| Plain text | `.log`, `.txt` | Line by line. App logs, nginx access and error, Kubernetes and syslog |
| JSON lines | `.jsonl`, `.ndjson`, or `.json` with one object per line | One record per line |
| JSON document | `.json` holding an array, or an object with a list inside such as `{"logs": [...]}` | One record per item |
| CSV / TSV | `.csv`, `.tsv` | The header row gives the column names. The delimiter is detected |
| Parquet | `.parquet` | Read with `pyarrow` in batches, one record per row |
| Gzip | any of the above plus `.gz` | Decompressed as a stream, then read as the format inside |

Not supported: Windows `.evtx`, binary journald files, Excel, and archives with several files (`.zip`, `.tar`).

### 7.2 Detection

We look at the content, not just the extension. Parquet files start with `PAR1`, gzip with `1f 8b`, JSON with `{` or `[`, and CSV is confirmed by sniffing the first lines. If the content and the extension disagree, the content wins and the app says what it found ("Detected CSV, 3 columns"). Anything else is read as plain text, with bad bytes replaced.

### 7.3 Mapping columns to fields

Structured records have named fields rather than one line of text. We match them to `LogLine` fields by common names, ignoring case:

| Field | Column names we look for |
|---|---|
| `timestamp` | `timestamp`, `@timestamp`, `time`, `ts`, `datetime`, `date` |
| `level` | `level`, `severity`, `loglevel`, `log_level`, `lvl` |
| `service` | `service`, `app`, `application`, `component`, `host`, `hostname`, `pod`, `container` |
| `message` | `message`, `msg`, `log`, `text`, `event`, `description` |
| `error_code` | `error_code`, `code`, `status`, `status_code` |
| `trace_id` | `trace_id`, `traceid`, `request_id`, `correlation_id`, `span_id` |

- Columns we don't recognise are kept and shown in the parsed table.
- If there's no message column, the message is built from the other columns as `key=value` pairs.
- For structured files the line number is the row number, and evidence says "row 412".
- The app shows which mapping it used, so a wrong guess is easy to see.

### 7.4 Limits

| Limit | Value |
|---|---|
| Upload size | `MAX_UPLOAD_MB`, default 5 and at most 5. Streamlit refuses bigger files itself (`server.maxUploadSize` in `.streamlit/config.toml`), so a higher value would never take effect |
| Decompressed size (gzip) | 50 MB, checked as we decompress, which protects against zip bombs |
| Rows or lines | 200,000 |
| Columns | 200 |
| Characters per line or cell | 4,096, then cut with a marker |
| JSON nesting | 5 levels, flattened with dotted keys such as `http.status` |

Going over a limit stops the read with a clear message. Parquet is read with `pyarrow` only, with no pickle or custom types. We never save an upload to disk under the name the user gave it.

### 7.5 Redaction

Before the Log Reader sees anything, every line and every field is checked for secrets and personal data: email addresses, bearer tokens and JWTs, AWS access keys, private key blocks, and the values of `password=`, `secret=`, `token=`, `api_key=` and similar fields. Each match becomes `[REDACTED:<type>]`, and the app shows a count per type. Everything after this point only sees the redacted text, including the model, the screen, Slack and JIRA. Redaction is pattern-based, so it can miss unusual formats, and the README says so.

---

## 8. Runbooks and search

- Gulam wrote 15 runbooks in `data/runbooks/`, covering DB pool, OOM, disk full, TLS, brute-force logins, 5xx spikes, upstream timeouts, crash loops, bad config, third-party rate limits, DNS, Kafka lag, deploy rollback, oversized requests and scanner noise.
- Each runbook starts with front matter (`id`, `categories`, `typical_severity`, `services`, `signals`), followed by these sections: Symptoms, Likely causes, Diagnose, Fix now, Prevent, References.
- **Search:** each section is embedded on its own so a query can match the right part, but the results are grouped back into whole runbooks, and the agent gets each runbook in full. Before this change, the "Fix now" steps only reached the model for 2 of our 24 test issues. Now they reach it for all 24.
- The index is built once per app start and cached with `st.cache_resource`, which costs a fraction of a cent.

---

## 9. The app

The page has an upload box, a paste box and a sample picker. Up to three samples can be picked at once: each one is its own run (so each counts towards the run limits), and a switch above the results moves between them, each with its own approval, chat and assistant answer. While a run is going, the graph lights up: green for done, orange for working, grey for waiting, dashed for skipped. Each finished step is listed under it. The results screen has these parts:

| Part | What it shows | Where it comes from |
|---|---|---|
| Incident card, one per top issue | Severity, title, service, run ID, root cause, recommended fix, and confidence with its reasons | Classifier and Remediation |
| Pipeline strip | Each step's status (done, skipped, waiting for approval, failed) and time | The step records |
| Parsed log | Fields table, detected format, column mapping and redaction counts | Parser |
| Patterns | Signature, count, first and last seen | Grouping |
| Checklist | Do now, this week, prevent | Cookbook |
| Slack and JIRA previews | Exactly what will be sent, or what was sent | Notification and JIRA plans |
| Approval panel | Approve and Reject, plus what will be sent | Approval step |
| Runbook sources | The runbooks cited, with match scores | Remediation |
| Run log | Time, step, agent, action, status | The step records |
| Cost row | Tokens, cost and time per agent, and the trace link | Telemetry |
| Severity legend | Low, medium, high, critical, and what each one triggers, as one row of badges above the results | The rules in section 6.2 |

- **Confidence reasons come from real signals only:** how many evidence lines support the issue, how well the best runbook matched, and whether JIRA has seen this signature before.
- **No made-up numbers.** Everything on the page comes from the current run. There are no fleet KPIs or trend charts, because we store no history of runs.
- `app.py` builds the page. Small functions that prepare things for display (escaping, badges, confidence reasons, the graph picture, the tables) live in `incident_suite/ui.py`, so they can be unit-tested. Log text goes through `ui.md()` before it reaches `st.markdown`: markdown, HTML and links are escaped, and URLs are defanged. Tables and code blocks show text as it is.
- **A paused run survives reruns.** The compiled graph and the run's context are kept in `st.session_state`, so Approve resumes the same run. The checkpointer only turns our own state models back into objects (an allow-list in `graph.py`).
- **Abuse limits:** each visitor can start 5 runs per 10 minutes, and all visitors together 60 an hour, so opening new sessions doesn't get around it (`RunLimiter` in `security.py`).
- **Live sending needs the passcode.** When `LIVE_INTEGRATIONS=true` and `ADMIN_PASSCODE` is set, the sidebar asks for the passcode. Without it the visitor gets a dry run, and their run never contacts Slack or JIRA.
- **Without a key** the page still loads, and Analyse explains that `OPENROUTER_API_KEY` is needed.
- **Layout:**
  - A health bar pinned in the header strip shows which integrations are connected (never the keys) and whether the app is in dry run or live mode.
  - A fixed footer shows the version and the limits every run works within.
  - The assistant sits in a right-hand column that stays in view while you scroll: chat history at the top, the question box below it, then "Since the run".
  - The left bar is only the app's name and a menu: Analyze, Agent Settings and App Settings. The middle shows the chosen page, and the assistant stays on the right on every page.
  - The bars use a small static CSS block (`STYLE` in `app.py`), which never contains log or user text.

---

## 10. Security

- **Secrets:** they only come from `.env` locally or from Streamlit secrets in the cloud. `.env` is git-ignored, `.env.example` only holds placeholders, and gitleaks runs in pre-commit and in CI.
- **Log text is data:** it's wrapped in tags in every prompt. The agents have no tools that run commands or open URLs.
- **Redaction:** secrets and personal data are masked before the model sees anything (section 7.5).
- **Approval:** a person approves before anything is written to Slack or JIRA (section 4).
- **Uploads:** allowed types, size and row caps, a safe display name, and nothing is saved under the user's file name (section 7.4).
- **Abuse limits:** 5 runs per 10 minutes per visitor and 60 an hour across everyone, a token budget per run (200,000 by default), at most 3 tickets per run by default (an admin can raise it to 5), and one retry per issue.
- **Live integrations** are off on the public app. We only switch them on for the recorded demo, and then `ADMIN_PASSCODE` keeps every other visitor on a dry run.
- **Escaping:** everything sent to Slack or JIRA is escaped, and links are never built from log content.
- **Least privilege:** the Slack bot only has `chat:write`. The JIRA account only needs the SCRUM project. The OpenRouter key has a credit limit and is revoked after the hackathon.
- **Dependencies** are pinned. pip-audit and Bandit run in CI, and Dependabot opens update PRs every week.

---

## 11. Traceability, cost and big logs

**Traceability**
- Each issue keeps its evidence lines, both the numbers and the text.
- Each fix names the issue ID it belongs to and the runbooks it used.
- Each run has a run ID (`run-20261010-141502-a1b2c3`) and a fingerprint of the log. Both appear in the app, in Slack and in JIRA, next to the trace link.
- **The trace link.** We choose each run's trace ID ourselves, before the run starts, so the link is known even when a run sends straight away. `trace_link()` in `telemetry.py` makes sure the LangSmith project exists and builds the URL from the project's address and that ID. The link goes into the app's Cost tab, the Slack summary and a remote link on each JIRA ticket. If LangSmith can't be reached, the run carries on without a link. The trace holds the redacted log text the agents saw, nothing more.
- Slack links to JIRA and JIRA links back to Slack, so you can start from either one.

**Cost:** tokens, cost and time are recorded per agent, including failed calls, using the prices in section 3.

**Big logs**
- Grouping turns 50,000 lines into a few dozen patterns before the model sees anything.
- Whatever is still too big is split into chunks, classified separately, and merged by signature.
- Each run has a token budget, a ticket cap and one retry per issue.
- One sample is a 50,000-row Parquet file. The README and the video show its pattern count, tokens and cost.

---

## 12. Configuration

Locally these go in `.env`. On Streamlit Cloud they go in the app's Secrets, in TOML format. `incident_suite/config.py` reads both, with Streamlit secrets winning over the environment, and the environment winning over `.env`. A blank value means "use the default".

| Variable | Needed? | Default | What it's for |
|---|---|---|---|
| `OPENROUTER_API_KEY` | For live analysis | | Model calls and embeddings |
| `CLASSIFIER_MODEL` | No | `openai/gpt-4.1-mini` | Log Reader, and Cookbook when `COOKBOOK_MODEL` is blank |
| `REMEDIATION_MODEL` | No | `openai/gpt-5-mini` | Remediation |
| `COOKBOOK_MODEL` | No | same as `CLASSIFIER_MODEL` | Cookbook |
| `EMBEDDING_MODEL` | No | `openai/text-embedding-3-small` | Runbook search |
| `CONTEXT_LINES` | No | `3` | Log lines kept around each error (0 to 20) |
| `RUNBOOK_TOP_K` | No | `3` | Runbooks shown to Remediation (1 to 10) |
| `RETRY_CONFIDENCE` | No | `0.5` | Below this confidence, Remediation tries once more (0 to 1) |
| `LIVE_INTEGRATIONS` | No | `false` | Only send to Slack and JIRA when this is `true` |
| `REQUIRE_APPROVAL` | No | `true` | Pause for Approve / Reject before sending |
| `SLACK_BOT_TOKEN` | For live Slack | | Bot token (`xoxb-...`) |
| `SLACK_CHANNEL_ID` | For live Slack | | ID of `#incident-alerts` |
| `JIRA_BASE_URL` | For live JIRA | | For example `https://yourteam.atlassian.net`. Must be https |
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | For live JIRA | | The bot account |
| `JIRA_PROJECT_KEY` | For live JIRA | | The key in the ticket numbers. Ours is `SCRUM` |
| `JIRA_ISSUE_TYPE` | No | `Task` | Issue type to create |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | No | off, none, `c10-g08` | Tracing |
| `MAX_UPLOAD_MB` | No | `5` | Upload cap (1 to 5, Streamlit's own limit) |
| `MAX_TICKETS_PER_RUN` | No | `3` | Ticket cap (1 to 5) |
| `RUN_TOKEN_BUDGET` | No | `200000` | Tokens per run (up to 500,000) |
| `ADMIN_PASSCODE` | No | | Unlocks the settings pages (section 17.1). When set, live sending in the app also needs it. Without it the settings are read-only |

**Run settings.** `RunSettings` in `state.py` holds every per-run option: the model and prompt for each agent, context lines, runbook matches and the retry threshold, the ticket cap and severity, the token budget, approval, redaction types and live mode. It's built from the effective config (the deployment's values plus any Admin Console changes) for each run and travels with the run, and the agents read from it, never from the config directly.

---

## 13. Repo layout

Files that don't exist yet are marked *(planned)*. We don't add a file that isn't in this list without agreeing it first.

```
c10-g08/
├── app.py                        Streamlit page only, no business logic
├── incident_suite/
│   ├── __init__.py               version
│   ├── config.py                 settings from .env, environment and Streamlit secrets; model allow-list and prices
│   ├── state.py                  Pydantic models shared by the agents, and RunSettings
│   ├── graph.py                  LangGraph: nodes, routing, retry loop, approval; dry-run CLI
│   ├── security.py               upload checks and safe display names
│   ├── telemetry.py              run ID, tokens, cost and time per agent
│   ├── ui.py                     display helpers for app.py
│   ├── parsing/
│   │   ├── readers.py            format detection, one reader per format, limits; parse_upload()
│   │   ├── parser.py             text line formats and column mapping into LogLine
│   │   ├── redact.py             secret and PII masking
│   │   └── grouping.py           signature masking, hashing, grouping, pre-filter
│   ├── agents/
│   │   ├── __init__.py           OpenRouter model clients and structured output with one retry
│   │   ├── log_reader.py
│   │   ├── remediation.py
│   │   ├── cookbook.py
│   │   ├── jira_agent.py         plan and send
│   │   └── notification.py       plan and send
│   ├── prompts/                  log_reader.md, remediation.md, cookbook.md
│   ├── rag/
│   │   ├── runbooks.py           loads and splits the markdown runbooks
│   │   └── retriever.py          in-memory vector search, whole-runbook results
│   └── integrations/
│       ├── __init__.py           shared retry, backoff and timeout for both clients
│       ├── slack_client.py       post, thread reply, permalink
│       ├── jira_client.py        search, create, comment, links
│       └── payloads.py           Slack blocks and JIRA documents, pure functions
├── data/runbooks/                15 markdown runbooks
├── sample_logs/                  17 sample logs, labels.json and make_samples.py, which generates them all
├── eval/run_eval.py              accuracy report for the README
├── tests/
│   ├── conftest.py               shared fakes: model, embeddings, JIRA tracker, Slack messenger
│   ├── unit/                     mirrors the package
│   └── integration/              graph runs with fakes, sample logs, live OpenRouter smoke test
├── docs/                         this plan, qa-checklist.md, demo-video-script.md
├── .streamlit/config.toml        theme and upload cap
├── .github/                      ci.yml, dependabot.yml, SECURITY.md
├── .gitattributes                LF line endings, binary sample files
├── .gitignore
├── .pre-commit-config.yaml       Ruff, mypy, gitleaks, whitespace fixers
├── .env.example                  every variable, with placeholders
├── pyproject.toml                dependencies and tool settings
├── requirements.txt              pinned, generated from pyproject.toml
├── LICENSE                       MIT
└── README.md
```

Extras add `incident_suite/agents/assistant.py` and `incident_suite/prompts/assistant.md` (shipped with extra 2) (section 17).

**Why it looks like this**

- **Flat layout, not `src/`.** `src/` suits libraries. We're shipping an app, and Streamlit Cloud runs `app.py` from the repo root, so with `src/` we'd need an extra install step just to make imports work there. pytest finds the package through `pythonpath = ["."]`.
- **One list of dependencies.** They're declared once in `pyproject.toml`, and `requirements.txt` is generated from it with `pip-compile` on Python 3.12. Nobody edits it by hand, and CI fails if it's out of date.
- **Prompts are package data,** loaded with `importlib.resources` rather than a hard-coded path.
- **Tests mirror the code.** `tests/unit/parsing/test_parser.py` tests `incident_suite/parsing/parser.py`, and so on. Shared fakes live in `conftest.py`, so no test needs a key or the network.
- **Line endings.** Most of the team is on Windows, so `.gitattributes` keeps LF in the repo.
- **Nothing generated is committed:** no `__pycache__`, `.venv`, `.env`, coverage reports or Streamlit secrets.

---

## 14. Quality and testing

**CI** (GitHub Actions, on every push and pull request)
- **Lock and style:** a lock-file check, then Ruff lint and format check.
- **Types:** mypy in strict mode.
- **Dead code:** Vulture.
- **Security:** Bandit, pip-audit and gitleaks.
- **Tests:** pytest with coverage, which has to stay at 85% or above.

**Tests** run with no key: one per log format, redaction (a positive and a negative example per pattern), grouping, each agent with a fake model, runbook search with fake embeddings, the JIRA rules (new, open, closed, over the cap, JIRA down), graph routing (retry loop, JIRA skipped, approve sends exactly once, reject sends nothing), and the 17 sample logs against `labels.json`. `pytest -m live` runs the OpenRouter smoke test with a real key and is skipped without one.

**Clean code rules**
- One job per module, short functions, and docstrings on public functions.
- No commented-out code, notebooks or scratch files.
- Agents return Pydantic models. If an agent fails, the user sees a clear message and the run carries on where it can. No stack traces on screen.

**Eval** (`eval/run_eval.py`, Gulam runs it with `python eval/run_eval.py`): it runs every sample as a dry run with the real models and compares the result with `labels.json`. It reports issues found, category accuracy, severity accuracy, whether the fix mentions the label's key idea, critical recall (was a ticket planned when it should have been), tickets that shouldn't have been opened, the Slack decision, and average tokens, cost and time per run, plus the 50k-row log on its own. An expected issue counts as found when a found issue shares an evidence line, or else has the same service, or else has the signature hint in its title. The scoring is unit-tested without a key. The results table goes into the README.

**Sample logs.** There are 17 of them, all generated by `sample_logs/make_samples.py` so the line numbers in `labels.json` always match. A test fails if the files and the script ever disagree.

| # | File | Format | Scenario | Severity |
|---|---|---|---|---|
| 1 | `payment_db_conn_refused.log` | Plain text | DB pool exhausted after a deploy | Critical |
| 2 | `orders_oom_killed.log` | Kubernetes | Memory climbing, `OOMKilled`, restart | Critical |
| 3 | `api_disk_full.txt` | Syslog | `No space left on device` | Critical |
| 4 | `gateway_tls_expired.jsonl` | JSON lines | Certificate expiry, then handshake failures | High |
| 5 | `auth_bruteforce.json` | JSON array | Failed logins from a few IPs, lockouts | High |
| 6 | `nginx_5xx_spike.log` | Nginx access | 502/503/504 spike on `/api/checkout` | High |
| 7 | `search_upstream_timeouts.tsv` | TSV | Upstream timeouts to search | Medium |
| 8 | `worker_crashloop.log` | Kubernetes | `CrashLoopBackOff` | Critical |
| 9 | `inventory_bad_config.txt` | Plain text | Missing env var and bad YAML at startup | High |
| 10 | `api_rate_limited.csv` | CSV | 429s from a third-party API | Medium |
| 11 | `notify_dns_failures.log` | Syslog | `NXDOMAIN` | High |
| 12 | `kafka_consumer_lag.parquet` | Parquet | Consumer lag rising | Medium |
| 13 | `healthy_service.csv` | CSV | Mostly INFO, a few harmless WARNs | Low, nothing sent |
| 14 | `mixed_incident.json` | JSON document | Deploy, then DB errors, timeouts and 5xx | Critical and high |
| 15 | `payment_db_conn_refused_repeat.log` | Plain text | Same signature as #1, to test repeats and regressions | Critical |
| 16 | `edge_nginx_error.log.gz` | Gzip of nginx error log | Oversized bodies, 404 floods, minor noise | Low |
| 17 | `large_50k_mixed.parquet` | Parquet | 50,000 rows with about 20 patterns hidden in noise | Mixed |

---

## 15. Team

| Person | Role | Owns |
|---|---|---|
| **Ashvin** (with Claude) | Lead, core code | Scaffold and CI, state models, parsing, agents and prompts, the graph, Slack and JIRA clients, UI and extras. Reviews and merges every pull request |
| **Raphael** | Integrations and deploy | OpenRouter key with a credit limit; Slack workspace, app, token, webhook and (for the assistant) the `channels:history` scope; JIRA site, SCRUM project, token and the two automation rules; LangSmith; Streamlit Cloud and its secrets, including `ADMIN_PASSCODE`. Tests the live Slack and JIRA flows and takes screenshots |
| **Gulam Mohammad** | Data | The 15 runbooks, the eval run and the README results table |
| **Safwan** | QA, docs and demo | QA checklist after each deploy, architecture diagram, README fresh-clone test on Windows, video script, recording and editing, and the demo-day checklist |

**Ground rules**
- Only Ashvin uses Claude, so the others' work runs in parallel without it.
- One branch per person, with pull requests into `main`. Ashvin reviews each one, and nobody edits another person's folders.
- No secrets in Git or chat. Raphael shares keys privately and puts them straight into Streamlit secrets and each person's `.env`.
- Two check-ins: Saturday night before stopping, and Sunday around 1 PM.
- To sync after a merge, on `main`: `git pull --ff-only` if you have no local changes. If you do, use `git reset --keep origin/main`, which updates your files and stops rather than overwrite your own edits. Plain `git reset origin/main` isn't enough: it leaves the old files on disk. Never use `git reset --hard` with unsaved work.

---

## 16. Timeline

| Block | Work | Status |
|---|---|---|
| H1 | Scaffold, CI, config, state models and `RunSettings`, OpenRouter smoke test, bare deploy | Done |
| H2 | Readers for every format, parser, redaction, grouping and pre-filter | Done |
| H3 | Log Reader, runbook search, Remediation, Cookbook, JIRA and Notification plans; sample logs and runbooks | Done |
| H4 | The graph: retry loop, parallel step, approval pause; dry run from the command line | Done |
| H5 | Slack and JIRA clients. Dry run first, then live with Raphael. The team sets up the Jira Slack app and the automation rules at the same time | Done. Live test passed on 10 Oct |
| H6 to H7 | Streamlit UI (section 9), sample picker, approval panel, deploy | Done. Redeploy pending |
| H8 | LangSmith, eval run, clean-code and security pass | Done. Gulam runs the eval |
| Extras | Section 17, in order, until the freeze | 1 to 6 merged (6 as its fallback, the replay slider). UI fixes, layout, Admin Console, TriageGraph name and several samples at once merged |

**Sunday 11 Oct (IST)**

| Time | What happens |
|---|---|
| Morning | Ashvin has 4 extra hours: finish H5 to H8, then start the extras |
| 1 PM | Check-in |
| 3 PM | **Feature freeze.** Any unfinished extra is dropped |
| 3 to 4 PM | README final pass, fresh-clone test, screenshots, eval table |
| 4 PM | Record the video, with a backup take |
| 6 PM | Final checks: links, video, CI, live integrations switched off |
| 6:30 PM | **Submit** |
| 7 PM | Hard deadline |

---

## 17. Extras, in build order

We build these after the core works, from the top down. Each one ships as its own pull request with tests and green CI, so we can stop at any point. Nothing new starts after about 2:30 PM, and anything unfinished at 3 PM is dropped. If an extra ships, it gets a line in the README and, if it's worth it, a few seconds in the video.

| Order | Extra | Time |
|---|---|---|
| 1 | Admin Console (replaced the agent settings page) | 1 to 1.5 h |
| 2 | Assistant: latest Slack and JIRA updates | 1 to 1.5 h, including the assistant itself |
| 3 | Assistant: draft a runbook when none matches | about 1 h |
| 4 | Assistant: chat about the current run | about 30 min once the assistant exists |
| 5 | Small extras: paste logs, postmortem download, "seen before" fix, error timeline | 15 to 45 min each |
| 6 | Run replay with an animated diagram, only if time allows | about 2 h |

### 17.1 Admin Console

Two pages in the left bar's menu, Agent Settings and App Settings, change settings for every visitor of the running app, without a redeploy. It replaced the first version of extra 1, a per-session settings page with editable prompts, which we dropped after the UI review.

Each page has cards, and each card has its own Save button, so one bad value never blocks the rest.

| Page | Card | Settings |
|---|---|---|
| Agent Settings | Models | OpenRouter key and the token budget per run |
| | Log Reader, Remediation, Cookbook | Provider and model (from the allow-list, with the price under it). Log Reader also has context lines; Remediation has runbook matches (top-k) and the retry confidence. Each setting has a one-line explanation |
| | Embedding | The embedding model, with its price |
| App Settings | General | Live integrations, approval, upload limit (1 to 5 MB), tickets per run (1 to 5) |
| | Slack | Bot token and channel ID |
| | JIRA | Site, email, project key and API token. The issue type is fixed to Task |
| | LangSmith | Tracing on or off, project and API key |

**The rule:**
- **Defaults** always come from `.env`, the environment or Streamlit secrets, and the settings pages start from them.
- **Opening** a settings page from the menu asks for `ADMIN_PASSCODE` in a small dialog, once per session (`admin_ok`), until you press Lock. The passcode itself only ever comes from the deployment's settings. Without one there, the pages open read-only for everyone.
- **A saved change** applies to every visitor until the app sleeps, restarts or is redeployed. Then the defaults come back. It lives in the server's memory (`AdminOverrides` in `config.py`). Nothing is written to disk, because Streamlit Community Cloud has no lasting storage and we have no database.
- **Checks:** a change is checked with exactly the same rules as the env values (`AppConfig`), so models only come from the allow-list and the upload limit can never go above 5 MB. A value equal to the default isn't stored.
- **Keys are never shown.** A key box shows only a hint, the first and last 4 characters (`sk-o…a1b2`), so you can tell which key is in use but the key never reaches the page. Keys shorter than 16 characters only say "set". Left blank, the current key stays. After a save the box is cleared, so a typed key doesn't linger in the session.
- **Where a key is sent changes only with the key.** Changing the JIRA site or email needs the JIRA token too, and changing the Slack channel needs the Slack token too. Otherwise someone with the passcode could send our token, or our messages, somewhere else.
- **Visibility:** each settings page lists the settings that differ from the defaults. **Back to defaults** drops all changes at once.
- **Demo moment:** switch the Remediation model, run the same sample again, and compare the cost and the output.

### 17.2 Assistant: latest Slack and JIRA updates

The assistant is a small, read-only LangGraph agent in the sidebar. It uses the cheap model, counts towards the run's token budget, and shows up in the cost row. Its tools can read data but never write anything.

This first assistant feature answers "what's happened since?" for this run's incident:

- **Slack:** it reads replies in the app's own thread for this run. When a ticket was seen before, it also reads the original incident's thread. To find that thread, we store the Slack channel and `ts` on the ticket (as a JIRA issue property, `incident_suite.slack`) when we first post. Replying in the original thread from the Notification agent is not built; the run still posts its own summary.
- **JIRA:** it reads status, assignee and the latest comments, only for this run's tickets labelled `incident-suite`. The query is fixed in code, so the model can't write its own JQL.
- **Limits:** it only works in live mode with the admin passcode entered. Everything it reads is redacted and treated as data. It can't browse Slack or JIRA, or answer open questions like "what's in Slack".
- **Needs:** the `channels:history` scope on the Slack app (done on 10 Oct).
- **How it's built:** `agents/assistant.py` is a three-step LangGraph: read the Slack threads, read the tickets, summarise with `openai/gpt-5-nano`. If nothing could be read, it says why and makes no model call. The readers are protocols that the Slack client (`conversations.replies`) and the JIRA client (issue property, then status, assignee and the last 3 comments, refused for tickets without the `incident-suite` label) implement. The prompt is `prompts/assistant.md`.
- **On screen:** the sidebar's **Assistant** section appears after a run. The **What's happened since?** button only shows once the run posted to Slack or JIRA and the admin passcode is entered. Its cost goes into the run's Cost tab as "Assistant".

### 17.3 Assistant: draft a runbook when none matches

- **When:** an issue still has no good runbook match after its retry.
- **What it does:** the assistant offers to draft a runbook in our template (front matter, then Symptoms, Likely causes, Diagnose, Fix now, Prevent, References), based on the issue and its evidence. The draft is labelled "AI draft, not reviewed".
- **Approval:** Approve or Discard. An approved draft has to pass the same checks as our own runbooks. It's then added to this session's search index and offered as a `.md` download. It's never written to disk, the repo, JIRA or Slack (decision 10 Oct, section 21).
- **How it's built:**
  - **When it's offered:** a card shows **Draft a runbook (AI)** when the issue's fix has no runbook source, or its best match is under 0.35 (`ui.needs_runbook`).
  - **The draft:** the assistant (`draft_runbook` in `agents/assistant.py`, cheap model, the `runbook` task in `prompts/assistant.md`) gets the issue, the steps suggested so far, and up to 20 redacted evidence lines in tags. It returns the template's fields, never markdown. Our code writes the file: front matter with `status: AI draft, not reviewed`, an id we choose (`draft-<category>-<hash>`), and the six sections in order.
  - **The checks:** `check_runbook()` in `rag/runbooks.py`, which every shipped runbook passes too (there's a test for that). Approve is greyed out while a check fails.
  - **After approval:** later runs in the session search a session-only index of our 15 runbooks plus the approved drafts. The draft's cost goes into the run's Cost tab.

### 17.4 Assistant: chat about the current run

A chat box in the sidebar that answers questions about this run only. It uses tools that read the run's issues, evidence lines, fixes, checklist, tickets and costs, and it cites line numbers in its answers. There's a cap on tool calls per question, and log text stays inside tags as data.

- **How it's built:** `ask_about_run` in `agents/assistant.py` is a one-node LangGraph loop on the cheap model. Each turn, the model returns either one tool call (`issues`, `evidence`, `fix`, `checklist`, `tickets` or `costs`, with an issue ID where needed) or its answer. Tool results go back inside `<tool_result>` tags.
- **Limits:** at most 4 tool calls, then it must answer, with a plain fallback if it still doesn't. Questions are cut at 500 characters. The last 3 questions and answers go along for follow-ups. It uses the run's token budget, and its cost shows in the Cost tab as "Assistant".
- **Access:** it works after any run, dry runs included, because it reads only the run's own results. It never touches Slack or JIRA.

### 17.5 Small extras

| Extra | Time | What it adds |
|---|---|---|
| Paste logs as text | about 15 min | A third choice, **Paste text**, next to the sample picker and the uploader. Pasted text becomes `pasted.log` (at most 1,000,000 characters) and goes through the same checks, redaction and format detection as an upload, so pasted JSON lines or CSV work too. In review |
| Postmortem download | about 30 min | A markdown postmortem (summary, timeline, root cause, fixes, follow-ups) built from the run. No model call: `ui.postmortem_markdown` uses only the run's results. The timeline puts deploy and config-change lines next to when each issue was first and last seen. Root causes are marked as hypotheses, log URLs are defanged, and the file says it's a draft to review. In review |
| "Seen before" fix | 30 to 45 min | When a closed ticket has the same signature, show its resolution comment: "last time this was fixed by…". It's also another real reason for the confidence score. The JIRA search already made by the plan step now also asks for `comment`, so there's no extra call. Only a closed ticket's last comment counts. It's redacted, cut to 500 characters, kept on the plan as `previous_fix`, and shown on the card. In review |
| Error timeline | about 45 min | Errors per minute across the log, with deploy and config-change lines marked. A **Timeline** tab shows ERROR and FATAL lines per bucket, in UTC. Buckets are 1, 5, 15, 60 or 240 minutes, so there are never more than 180 bars. Dashed lines mark deploys and config changes, including any up to an hour before the first error, since they're often the cause. Colours are the status colours from our dataviz palette, checked with its validator on the app's dark background. WARN is left out because it's mostly noise and its colour is too close to ERROR's. The chart is a Vega-Lite spec built by `ui.timeline_spec`, so it's unit-tested and needs no new dependency. In review |

### 17.6 Run replay, only if time allows

- **What it is:** a "Replay this run" view that steps through a finished run. The log lines being looked at are highlighted, what the current agent received and returned is shown, and the graph animates as work moves between agents, including the retry loop and the parallel step.
- **Data:** every node already records a step, so no agent changes are needed. A few recorded sample runs can be saved as JSON, so the replay works without a key.
- **Player:** one self-contained HTML file with an inline SVG, embedded with `components.html`. Run data goes in as escaped JSON, never as raw HTML.
- **Fallback:** a simple step slider with the static graph and one caption per step.
- **What's built (11 Oct):** the fallback, as a **Replay** tab. A slider moves through the run's recorded steps. Earlier steps keep their final colour, the current one lights up, and later ones wait. The retry shows as the Log Reader lighting up again, with "(the retry: more context)" in the caption (`ui.replay_frame`). The animated player, recorded sample runs as JSON, and the per-step inputs and outputs are not built: they need new files and agent changes, so they wait for a decision.

---

## 18. Demo video

Target length is 2:40. The full script and shot list are in `docs/demo-video-script.md`. In outline:

1. **The problem:** 50,000 lines at 3 AM.
2. **Architecture:** the diagram.
3. **A live run** on the deployed app: the retry loop fires, then the fixes and the checklist appear.
4. **Approve** on camera.
5. **Slack:** the summary and the thread.
6. **JIRA:** the ticket and its links. Move it to Done and Slack posts "resolved".
7. **Repeat run:** the same log adds a comment instead of a duplicate ticket. Then the 50k-row log, with its pattern count and cost.
8. **Wrap-up:** the eval table, green CI, and the repo and live links.

We record at 1080p and upload it unlisted.

---

## 19. Accounts and deployment

- **OpenRouter:** a key just for this hackathon, with a credit limit. Revoke it afterwards. Use a cheap paid model for the demo, because free `:free` models are rate-limited and can be busy.
- **Slack:**
  1. Create a workspace and the `#incident-alerts` channel.
  2. Create the "TriageGraph" app at api.slack.com/apps with the bot scope `chat:write`, plus `channels:history` if the assistant ships.
  3. Install it and copy the `xoxb-` token.
  4. Run `/invite @TriageGraph` in the channel and copy the channel ID.
  5. Add an incoming webhook for the automation rules.
  6. Install the Jira Cloud app.
- **JIRA:**
  1. Create a free Atlassian site and a project. Ours is a Scrum project with the key `SCRUM`. Priority must be on the create screen.
  2. Create an API token at id.atlassian.com, ideally for a separate bot account.
  3. Add the two automation rules (section 6.6).
- **LangSmith:** create an API key, and use the project name `c10-g08`.
- **Streamlit Community Cloud:**
  1. Deploy from `main` with `app.py` and Python 3.12.
  2. Put the secrets in TOML format.
  3. Every push to `main` redeploys.
  4. Free apps sleep when idle, so before judging and recording we open the app, run each sample once, and check the secrets.
  5. If Streamlit Cloud has trouble on the day, the video and the local setup in the README still show everything working.

---

## 20. Risks

| Risk | What we'll do |
|---|---|
| The Streamlit app is asleep | Wake it before judging and before recording |
| Streamlit Cloud has an outage | The video and the local setup still show everything |
| A dependency won't install on Streamlit Cloud | Checked with the bare deploy in H1, including `pyarrow` |
| OpenRouter is slow, rate-limited or down during the demo | Use a paid model, keep a backup take, and show a clear error in the app |
| A paused run is lost when Streamlit reruns | The graph and checkpointer live in `st.session_state`, and a test covers resuming |
| Redaction misses a secret format | Listed as a limitation. Live integrations stay off on the public app |
| Slack rate limit (about 1 message per second per channel) | One summary and one thread reply per run, and we respect `Retry-After` |
| JIRA automation limit on the free plan | Event rules only, with no scheduled rules or test bursts |
| Public visitors spamming our Slack and JIRA | Live integrations stay off on the public app, and the admin passcode guards the assistant's Slack and JIRA tools |
| The JIRA API has changed, for example the JQL search endpoint | Checked on 10 Oct: we use `/rest/api/3/search/jql`, because the old `/search` is gone. The live test with Raphael confirms the rest |
| Extras eat into core time | Fixed order, one PR each, and a hard stop at 3 PM |

---

## 21. Decisions log

| When | Decision |
|---|---|
| Plan v1 to v6 | Streamlit UI on Streamlit Community Cloud. LangGraph and LangChain. No LlamaIndex, LanceDB, FastAPI, Next.js, Vercel or Docker |
| Plan v1 to v6 | Runbook search with LangChain's in-memory vector store |
| Plan v1 to v6 | Slack bot token (Web API). An incoming webhook is used only by the Jira Automation rules |
| Plan v1 to v6 | JIRA Cloud REST v3: one ticket per critical issue, label-based dedupe, at most 3 per run. Two-way sync through the Jira Slack app and two automation rules, with no code |
| Plan v1 to v6 | Flat repo layout. Dependencies declared once in `pyproject.toml`, with `requirements.txt` generated by pip-tools |
| 10 Oct | The team's mockups are the visual brief. We adopt redaction, the approval step and the results layout. We don't build separate guardrail agents, fleet KPIs, multi-tenancy or Teams |
| 10 Oct | OpenRouter is the single model provider, with one key |
| 10 Oct | Upload accepts text, JSON lines, JSON documents, CSV, TSV, Parquet and gzip, detected by content |
| 10 Oct | `RunSettings` travels with each run, so the settings page only touches the UI |
| 10 Oct | Team roles as in section 15 |
| 10 Oct | No Makefile or `.editorconfig`. The README lists the CI commands, and the repo keeps only files in section 13 |
| 10 Oct | All 17 sample logs are generated by `make_samples.py`. Gulam writes the runbooks |
| 10 Oct | Runbook search returns whole runbooks, not single sections |
| 10 Oct | Deadline 7 PM IST Sunday. Submit 6:30 PM, freeze 3 PM, record 4 PM |
| 10 Oct | Extras order: settings page, assistant Slack and JIRA updates, runbook drafting, chat, small extras, then replay only if time allows |
| 10 Oct | AI-drafted runbooks stay in the session and can be downloaded. They're never posted to JIRA or saved to the repo |
| 10 Oct | The plan and the README are rewritten as one current version. From now on we change sections in place and log the decision here |
| 10 Oct | H5: the JIRA agent now gets a `TicketDraft` (issue, fix, pattern, run ID), so comments can give the count and time window. Regression links and the link back to Slack are separate steps that can fail without losing the ticket. The shared retry helper lives in `integrations/__init__.py`. Log text goes to Slack as plain text with URLs defanged. The command line gets `--live`, which asks before sending |
| 10 Oct | All four keys checked with a read-only script, then a live run on the payment sample. Our JIRA project key is `SCRUM`, not `OPS`. The sync instruction in section 15 changes, because `git reset origin/main` leaves old files on disk |
| 11 Oct | H6 to H7: the graph streams each step to the page, so the picture lights up live. 5 runs per 10 minutes per visitor. The checkpointer only restores our own state models |
| 11 Oct | H8: we choose the trace ID before each run, so the LangSmith link goes into the app, Slack and JIRA. The eval script scores against `labels.json` and prints the README table. Security pass: when `ADMIN_PASSCODE` is set, live sending needs it; a shared cap of 60 runs an hour; file names, errors and the service name in the Remediation prompt are now escaped. `langsmith` is declared as a direct dependency |
| 11 Oct | Extra 1, the agent settings page, ships in full (export and import included). The admin passcode stays unlocked for the session once entered. `pages/` joins the CI tools |
| 11 Oct | Extra 1 gets an Admin card. With the passcode, an admin changes live mode, approval, the upload limit, tickets per run, the token budget and the JIRA project and issue type for everyone, until the app sleeps or restarts. Defaults come from env. Where secrets are sent (JIRA URL and email, Slack channel) stays env-only. `MAX_UPLOAD_MB` is now 1 to 5, Streamlit's own cap |
| 11 Oct | Extra 2, the assistant, ships read-only. It's a three-step LangGraph on the cheap model; it reads only this run's threads and tickets, redacted and inside tags. New tickets store their Slack thread as a JIRA issue property. Replying in the original Slack thread is left out for time. Extras go up as stacked PRs overnight, to merge in order |
| 11 Oct | Extra 3: the model fills the runbook template's fields and our code writes the markdown, so drafts always have the template's shape. `check_runbook()` is shared with our own runbooks. The drafting instructions live in `assistant.md` as a second task, rather than a new prompt file |
| 11 Oct | Extra 4: the chat is a small tool loop that works through our structured-output client (the model picks one tool or answers), so the same fakes test it. It's open after any run, because it reads only that run |
| 11 Oct | Extra 5 goes up as one PR per small extra, so each is easy to review. Pasted text is treated exactly like an uploaded `.log` file |
| 11 Oct | Extra 6 ships only as its planned fallback, a step slider, because the full player needs new files (recorded runs as JSON) and changes to every agent |
| 11 Oct | UI review after testing: a new run clears the old results at once; the assistant, chat and runbook drafting show progress; the severity legend is a one-line row above the results; redaction types have plain names. Next: a layout PR (top health bar, sticky footer, assistant in a right-hand column) and an Admin Console in the sidebar (settings for everyone, behind the passcode) |
| 11 Oct | Layout: health bar in the header strip, fixed footer, assistant in a right-hand column. Sticky positioning didn't work for the top bar, so it's fixed in the header strip, and the column itself is sticky. The admin passcode only appears when live mode is on |
| 11 Oct | The Admin Console replaces the agent settings page and the Admin card: two pages, Agent Settings and App Settings, reached from a menu in the left bar, with a Save per card, for everyone behind the passcode. The left bar is only a menu, the middle shows the page, and the assistant stays on the right. The passcode is asked in a dialog when a settings page is opened. Keys can now be changed too, with one rule: where a key is sent changes only together with that key. Prompt editing and JSON export are dropped. New settings: `COOKBOOK_MODEL`, `CONTEXT_LINES`, `RUNBOOK_TOP_K`, `RETRY_CONFIDENCE`. Tickets per run is now 1 to 5 (default 3). `pages/` is removed |
| 11 Oct | Polish after testing: less space at the top, the footer centred in its bar and lined up with the content, the menu icons aligned, Enter submits the passcode, and key boxes show a hint (first and last 4 characters) instead of just "set" |
| 11 Oct | The app is called TriageGraph. Only names people see change: the page title, header and footer, the "Opened by" line in JIRA tickets, the assistant prompt, the LangSmith run name, the OpenRouter app title and the docs. The package (`incident_suite`), the repo name and the `incident-suite` JIRA label stay, because repeat detection searches by that label. Our existing Slack app keeps working; renaming its display name is optional |
| 11 Oct | Several samples at once: up to three, one run each, with a switch between the results. We don't merge logs into one run, because formats differ and the results would mix. If one log fails, its error stays above the other runs' results |
| 11 Oct | Everything up to #36 is merged. Final docs pass: the QA checklist gets the layout, settings-page and several-samples checks, and the video script switches live mode on and off from App Settings, off camera |

**Still open:** the Streamlit Cloud redeploy (Raphael), and the eval run and README results (Gulam).

---

## 22. Ideas for later (not building now)

- Paging on-call through PagerDuty or Opsgenie
- Pulling logs straight from Datadog, CloudWatch, Grafana Loki or Elasticsearch, or starting a run from an alert
- Slack buttons and a `/analyze` command, which need a small backend that stays running
- JIRA webhooks feeding back into the app, so we learn which fixes actually worked
- Linking incidents to recent GitHub deploys, or opening a PR for a config fix
- Publishing the checklist or a postmortem to Confluence
- Jira Service Management incident types instead of plain tasks
- Microsoft Teams or email as alternatives to Slack
