# Build plan: Incident Suite (c10-g08)

Version 8, 10 Oct 2026.

**Deadline:** 7 PM IST, Sunday 11 Oct 2026. We submit by 6:30 PM. Feature freeze is at 3 PM and we record the video at 4 PM.

**Status:** H1 to H4 are merged: scaffold, parsing, agents, runbook search and the LangGraph graph. Next up is H5, the Slack and JIRA clients. Section 16 has the full timeline.

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

You can do a full dry run from the command line: `python -m incident_suite.graph <log file>`.

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

- **Plan (read-only):** for each critical issue, up to 3 per run, look up the newest ticket with the same signature and decide whether to create a ticket, comment on an existing one, open a regression, or skip because the cap is reached.
- **Send (after approval):** create or comment, then link regressions to the old ticket.
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
| A critical issue that already has an open ticket | A comment on it: seen again, run ID, count, time window | Summary says "OPS-12 updated, seen again" |
| A critical issue whose ticket is closed | A new ticket labelled `regression`, linked to the old one | Summary marks it as a regression |
| More than 3 critical issues | Tickets for the 3 most severe | All listed, with the rest marked "not ticketed" |
| Rejected at the approval step | Nothing | Nothing. The rejection is shown in the app |
| JIRA is down or the token is wrong | Nothing written, so nothing gets duplicated | Summary still posts, with a warning that tickets failed |
| Slack is down or the token is wrong | Not affected | The error shows in the app |
| Dry run (no credentials, or live switched off) | Nothing | Nothing. The app shows exactly what it would have sent |

### 6.3 How we spot a repeat

Each ticket carries two labels, `incident-suite` and `sig-<hash>`, where the hash comes from the pattern's signature (section 5.1). Before creating anything, the JIRA agent searches:

`project = OPS AND labels = "sig-<hash>" ORDER BY created DESC`

If the newest match isn't done, we comment on it. If it is done, we open a new ticket and link it to the old one as a regression.

### 6.4 Order of calls after approval

1. JIRA: for up to 3 critical issues, create or comment. Regressions are linked to the old ticket.
2. Slack: post the summary with `chat.postMessage` and keep the message's `ts`.
3. Slack: reply in that thread with the checklist and the fix for each issue.
4. Slack: get the message link with `chat.getPermalink`.
5. JIRA: on each ticket from step 1, add remote links to the Slack message and the LangSmith trace.

Both clients retry on 429 and 5xx with backoff, and respect Slack's `Retry-After` header. Every call has a timeout. If a call fails, the app shows the error and carries on rather than crashing.

### 6.5 What goes into the messages

- **Slack summary:** the worst severity, the log name, the run ID, and up to 5 issues, each with its evidence line numbers, the first fix step and its ticket. Anything beyond that is counted as "+N more in the app".
- **Slack thread reply:** the checklist, then each fix with its root cause, steps and runbook sources.
- **JIRA ticket:**
  - Title: `[CRITICAL] <issue title> (<service>)`
  - Priority: Highest for critical, High for high
  - Labels: `incident-suite`, `sig-<hash>`, and `regression` where it applies
  - Description: what happened, evidence lines, fix steps, rationale, runbook sources and run ID
  - Remote links: the Slack thread and the trace

All log text is escaped before it goes into Slack or JIRA, and we never turn a URL found in a log into a link.

### 6.6 No-code setup (Raphael, about 20 minutes)

**Jira Cloud app for Slack**
- Install it from the Slack Marketplace and connect our Atlassian site.
- Invite it to `#incident-alerts`. Ticket links then show as preview cards with assign, comment and status buttons.
- Leave its own channel notifications off, so people don't get the same news twice.

**Jira Automation rules** (Project settings, then Automation)
1. **Resolved:** when an issue labelled `incident-suite` moves to Done, send "OPS-12 resolved by <name>: <summary>" to Slack.
2. **Picked up:** when an issue labelled `incident-suite` is assigned, send "OPS-12 picked up by <name>".

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
| Upload size | `MAX_UPLOAD_MB`, default 5 |
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

The page has an upload box and a sample picker. While a run is going, the graph shows which agent is working. The results screen has these parts:

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
| Severity legend | Low, medium, high, critical, and what each one triggers | The rules in section 6.2 |

- **Confidence reasons come from real signals only:** how many evidence lines support the issue, how well the best runbook matched, and whether JIRA has seen this signature before.
- **No made-up numbers.** Everything on the page comes from the current run. There are no fleet KPIs or trend charts, because we store no history of runs.
- `app.py` builds the page. Small functions that prepare things for display (escaping, badges, confidence reasons) go into one module, `incident_suite/ui.py`, so they can be unit-tested. Log text is escaped before it reaches any HTML.
- The sidebar shows which integrations are connected (never the keys) and whether the app is in dry run or live mode.

---

## 10. Security

- **Secrets:** they only come from `.env` locally or from Streamlit secrets in the cloud. `.env` is git-ignored, `.env.example` only holds placeholders, and gitleaks runs in pre-commit and in CI.
- **Log text is data:** it's wrapped in tags in every prompt. The agents have no tools that run commands or open URLs.
- **Redaction:** secrets and personal data are masked before the model sees anything (section 7.5).
- **Approval:** a person approves before anything is written to Slack or JIRA (section 4).
- **Uploads:** allowed types, size and row caps, a safe display name, and nothing is saved under the user's file name (section 7.4).
- **Abuse limits:** a per-session rate limit, a token budget per run (200,000 by default), at most 3 tickets per run, and one retry per issue.
- **Live integrations** are off on the public app. We only switch them on for the recorded demo.
- **Escaping:** everything sent to Slack or JIRA is escaped, and links are never built from log content.
- **Least privilege:** the Slack bot only has `chat:write`. The JIRA account only sees the OPS project. The OpenRouter key has a credit limit and is revoked after the hackathon.
- **Dependencies** are pinned. pip-audit and Bandit run in CI, and Dependabot opens update PRs every week.

---

## 11. Traceability, cost and big logs

**Traceability**
- Each issue keeps its evidence lines, both the numbers and the text.
- Each fix names the issue ID it belongs to and the runbooks it used.
- Each run has a run ID (`run-20261010-141502-a1b2c3`) and a fingerprint of the log. Both appear in the app, in Slack and in JIRA, next to the trace link.
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
| `CLASSIFIER_MODEL` | No | `openai/gpt-4.1-mini` | Log Reader and Cookbook |
| `REMEDIATION_MODEL` | No | `openai/gpt-5-mini` | Remediation |
| `EMBEDDING_MODEL` | No | `openai/text-embedding-3-small` | Runbook search |
| `LIVE_INTEGRATIONS` | No | `false` | Only send to Slack and JIRA when this is `true` |
| `REQUIRE_APPROVAL` | No | `true` | Pause for Approve / Reject before sending |
| `SLACK_BOT_TOKEN` | For live Slack | | Bot token (`xoxb-...`) |
| `SLACK_CHANNEL_ID` | For live Slack | | ID of `#incident-alerts` |
| `JIRA_BASE_URL` | For live JIRA | | For example `https://yourteam.atlassian.net`. Must be https |
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | For live JIRA | | The bot account |
| `JIRA_PROJECT_KEY` | For live JIRA | | For example `OPS` |
| `JIRA_ISSUE_TYPE` | No | `Task` | Issue type to create |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | No | off, none, `c10-g08` | Tracing |
| `MAX_UPLOAD_MB` | No | `5` | Upload cap (1 to 50) |
| `MAX_TICKETS_PER_RUN` | No | `3` | Ticket cap (0 to 3) |
| `RUN_TOKEN_BUDGET` | No | `200000` | Tokens per run (up to 500,000) |
| `ADMIN_PASSCODE` | No | | Unlocks live sending after prompts are edited, and the assistant's Slack and JIRA tools (section 17) |

**Run settings.** `RunSettings` in `state.py` holds every per-run option: the model and prompt for each agent, context lines, runbook matches and the retry threshold, the ticket cap and severity, the token budget, approval, redaction types and live mode. It's built from the config for each new session and travels with the run, and the agents read from it, never from the config directly. The settings page (section 17.1) only has to produce a valid `RunSettings`.

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
│   ├── ui.py                     display helpers for app.py (planned)
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
│       ├── slack_client.py       post, thread reply, permalink, retries (planned)
│       ├── jira_client.py        search, create, comment, links, retries (planned)
│       └── payloads.py           Slack blocks and JIRA documents, pure functions (planned)
├── data/runbooks/                15 markdown runbooks
├── sample_logs/                  17 sample logs, labels.json and make_samples.py, which generates them all
├── eval/run_eval.py              accuracy report for the README (planned)
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

Extras add `pages/agent_settings.py`, `incident_suite/agents/assistant.py` and `incident_suite/prompts/assistant.md` if they ship (section 17).

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

**Eval** (`eval/run_eval.py`, Gulam runs it): category accuracy, severity accuracy, critical recall (was a ticket opened when it should have been), and average tokens, cost and time per run. The results table goes into the README.

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
| **Raphael** | Integrations and deploy | OpenRouter key with a credit limit; Slack workspace, app, token, webhook and (for the assistant) the `channels:history` scope; JIRA site, OPS project, token and the two automation rules; LangSmith; Streamlit Cloud and its secrets, including `ADMIN_PASSCODE`. Tests the live Slack and JIRA flows and takes screenshots |
| **Gulam Mohammad** | Data | The 15 runbooks, the eval run and the README results table |
| **Safwan** | QA, docs and demo | QA checklist after each deploy, architecture diagram, README fresh-clone test on Windows, video script, recording and editing, and the demo-day checklist |

**Ground rules**
- Only Ashvin uses Claude, so the others' work runs in parallel without it.
- One branch per person, with pull requests into `main`. Ashvin reviews each one, and nobody edits another person's folders.
- No secrets in Git or chat. Raphael shares keys privately and puts them straight into Streamlit secrets and each person's `.env`.
- Two check-ins: Saturday night before stopping, and Sunday around 1 PM.
- To sync after a merge: `git fetch`, then `git reset origin/main` (or `git pull` if you have no local changes). Never use `git reset --hard` with unsaved work.

---

## 16. Timeline

| Block | Work | Status |
|---|---|---|
| H1 | Scaffold, CI, config, state models and `RunSettings`, OpenRouter smoke test, bare deploy | Done |
| H2 | Readers for every format, parser, redaction, grouping and pre-filter | Done |
| H3 | Log Reader, runbook search, Remediation, Cookbook, JIRA and Notification plans; sample logs and runbooks | Done |
| H4 | The graph: retry loop, parallel step, approval pause; dry run from the command line | Done |
| H5 | Slack and JIRA clients. Dry run first, then live with Raphael. The team sets up the Jira Slack app and the automation rules at the same time | Next |
| H6 to H7 | Streamlit UI (section 9), sample picker, approval panel, deploy | |
| H8 | LangSmith, eval run, clean-code and security pass | |
| Extras | Section 17, in order, until the freeze | |

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
| 1 | Agent settings page | 1 to 1.5 h (slim version about 45 min) |
| 2 | Assistant: latest Slack and JIRA updates | 1 to 1.5 h, including the assistant itself |
| 3 | Assistant: draft a runbook when none matches | about 1 h |
| 4 | Assistant: chat about the current run | about 30 min once the assistant exists |
| 5 | Small extras: paste logs, postmortem download, "seen before" fix, error timeline | 15 to 45 min each |
| 6 | Run replay with an animated diagram, only if time allows | about 2 h |

### 17.1 Agent settings page

A second page, `pages/agent_settings.py`, with one card per agent and a global card.

| Card | Settings |
|---|---|
| Every agent | Model (from the allow-list, with prices) and system prompt (starts from the file in `prompts/`, with "Reset to default") |
| Log Reader | Context lines around each error |
| Remediation | Runbook matches (top-k) and the confidence threshold for the retry |
| JIRA | Ticket cap and the severity that opens a ticket |
| Global | Token budget, approval on or off, redaction types, dry run or live |
| Integrations | Slack, JIRA and LangSmith shown as connected or not. Never the keys |

- **Changes are kept for your session only,** in `st.session_state`. Nothing is written to disk, so one visitor can't change prompts for everyone. You can export and import settings as JSON (validated like everything else) and reset them to the defaults.
- **Safety:** models only come from the allow-list, and the limits are enforced by `RunSettings`, not just by the widgets. If any prompt differs from the default, the run switches to dry run unless the admin passcode is entered.
- **Slim version,** if time is short: models and prompts only, without export and import.
- **Demo moment:** switch the Remediation model, run the same sample again, and compare the cost and the output.

### 17.2 Assistant: latest Slack and JIRA updates

The assistant is a small, read-only LangGraph agent in the sidebar. It uses the cheap model, counts towards the run's token budget, and shows up in the cost row. Its tools can read data but never write anything.

This first assistant feature answers "what's happened since?" for this run's incident:

- **Slack:** it reads replies in the app's own thread for this run. When a ticket was seen before, it also reads the original incident's thread. To find that thread, we store the Slack channel and `ts` on the ticket (as a JIRA issue property, `incident_suite.slack`) when we first post. The same stored value lets the Notification agent reply in the original thread when an open ticket is seen again.
- **JIRA:** it reads status, assignee and the latest comments, only for this run's tickets labelled `incident-suite`. The query is fixed in code, so the model can't write its own JQL.
- **Limits:** it only works in live mode with the admin passcode entered. Everything it reads is redacted and treated as data. It can't browse Slack or JIRA, or answer open questions like "what's in Slack".
- **Needs:** Raphael adds the `channels:history` scope to the Slack app.

### 17.3 Assistant: draft a runbook when none matches

- **When:** an issue still has no good runbook match after its retry.
- **What it does:** the assistant offers to draft a runbook in our template (front matter, then Symptoms, Likely causes, Diagnose, Fix now, Prevent, References), based on the issue and its evidence. The draft is labelled "AI draft, not reviewed".
- **Approval:** Approve or Discard. An approved draft has to pass the same checks as our own runbooks. It's then added to this session's search index and offered as a `.md` download. It's never written to disk, the repo, JIRA or Slack (decision 10 Oct, section 21).

### 17.4 Assistant: chat about the current run

A chat box in the sidebar that answers questions about this run only. It uses tools that read the run's issues, evidence lines, fixes, checklist, tickets and costs, and it cites line numbers in its answers. There's a cap on tool calls per question, and log text stays inside tags as data.

### 17.5 Small extras

| Extra | Time | What it adds |
|---|---|---|
| Paste logs as text | about 15 min | A text box next to the uploader, with the same checks and redaction |
| Postmortem download | about 30 min | A markdown postmortem (summary, timeline, root cause, fixes, follow-ups) built from the run |
| "Seen before" fix | 30 to 45 min | When a closed ticket has the same signature, show its resolution comment: "last time this was fixed by…". It's also another real reason for the confidence score |
| Error timeline | about 45 min | Errors per minute across the log, with deploy and config-change lines marked |

### 17.6 Run replay, only if time allows

- **What it is:** a "Replay this run" view that steps through a finished run. The log lines being looked at are highlighted, what the current agent received and returned is shown, and the graph animates as work moves between agents, including the retry loop and the parallel step.
- **Data:** every node already records a step, so no agent changes are needed. A few recorded sample runs can be saved as JSON, so the replay works without a key.
- **Player:** one self-contained HTML file with an inline SVG, embedded with `components.html`. Run data goes in as escaped JSON, never as raw HTML.
- **Fallback:** a simple step slider with the static graph and one caption per step.

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
  2. Create the "Incident Suite" app at api.slack.com/apps with the bot scope `chat:write`, plus `channels:history` if the assistant ships.
  3. Install it and copy the `xoxb-` token.
  4. Run `/invite @Incident Suite` in the channel and copy the channel ID.
  5. Add an incoming webhook for the automation rules.
  6. Install the Jira Cloud app.
- **JIRA:**
  1. Create a free Atlassian site and a Kanban project with the key `OPS`, with Priority on the create screen.
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
| The JIRA API has changed, for example the JQL search endpoint | Check Atlassian's current REST docs at the start of H5 |
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

**Still open:** the Streamlit Cloud deploy status and the Slack and JIRA credentials (Raphael).

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
