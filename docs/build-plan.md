# Build plan: Multi-Agent DevOps Incident Analysis Suite (c10-g08)

Version 6, 10 Oct 2026. Status: draft, under team review. Nothing has been built yet.
Deadline: 8 PM IST, Sunday 11 Oct 2026. Target submission: 6:30 PM IST.

This replaces the Tools and Decisions tabs of the original spreadsheet wherever the two disagree.

Changes in v6: the repo layout (section 9) gets `.gitattributes`, `.editorconfig`, `SECURITY.md`, Dependabot, tests that mirror the package, one source of truth for dependencies, and prompts shipped as package data. We also wrote down why we're using a flat layout instead of `src/`.

Changes in v5: runbook search moves to LangChain (no LlamaIndex, no LanceDB). Hosting is Streamlit Community Cloud only, so the Hugging Face Spaces backup and the Dockerfile are gone.

---

## 0. How we get judged, and how we plan to score

| # | Criterion | What we're doing about it | What a judge can check |
|---|---|---|---|
| 1 | Clean code (no dead code, no bugs, no security risks) | Ruff, mypy, Vulture, Bandit, pip-audit and gitleaks, all running in CI, plus tests that need no API key | Green CI badge on the README, and `make check` passes on a fresh clone |
| 2 | Public repo | github.com/akanani1/c10-g08, MIT licence, readable commit history | The repo itself; no secret in any commit |
| 3 | 2 to 3 minute demo video | Written script and shot list (section 12.3), recorded on the deployed app | Video link at the top of the README |
| 4 | Working prototype, deployed | Streamlit Community Cloud | Live link at the top of the README; sample logs work with no setup |
| 5 | README that covers everything, including local setup | Full README (section 12.5), tested by someone following it from scratch | "Run it locally" section |

---

## 1. What we're building

A Streamlit app. You upload an ops log, or pick one of our samples, and a set of agents works through it:

| Agent | What it does |
|---|---|
| Log Reader / Classifier | Parses each line into fields, groups repeated errors into patterns, then labels each issue with a category and severity |
| Remediation | Finds a fix for each issue, explains why, and cites the runbook it came from. If it isn't confident, it asks the Log Reader for more context and tries again |
| Cookbook Synthesizer | Turns the fixes into a checklist: do now, do this week, prevent next time |
| JIRA Ticket | Opens a ticket for each critical issue, or updates the existing one if we've seen it before |
| Notification | Posts the result to Slack, with the checklist in a thread and links to the tickets |
| Orchestrator | The LangGraph graph that holds shared state and decides what runs next |

While a run is going, the app shows the graph with each node lighting up, the parsed log table, issue-to-fix rows, the Slack and JIRA results, and tokens and cost per agent.

---

## 2. Stack

| Layer | Choice | Reason |
|---|---|---|
| UI | Streamlit | One language, live status per agent, quickest way to a decent-looking demo |
| Orchestration | LangGraph | Conditional routing, the feedback loop, the parallel step, and a diagram we can show in the app |
| LLM calls | LangChain with OpenAI | A cheap model for classification, a stronger one for remediation. Exact model names get picked when we build |
| Runbook search (RAG) | LangChain `InMemoryVectorStore` with OpenAI embeddings | About 15 short runbooks. They're embedded once when the app starts and cached with `st.cache_resource`. No extra database or framework to install. If we ever need persistence, LangChain's LanceDB store drops in without changing the agents |
| Tracing | LangSmith (optional) | Free. Each run's trace link goes into Slack and JIRA |
| Slack | Slack app with a bot token (Web API) | Lets us thread replies and get message links. A plain webhook can't do either |
| JIRA | Jira Cloud REST API v3 on a free Atlassian site | Create, search, comment, link |
| Platform glue (no code) | Jira Cloud app for Slack, and Jira Automation rules | Sends JIRA changes back into Slack without us writing a backend |
| Hosting | Streamlit Community Cloud | Confirmed as allowed. Deploys straight from GitHub |
| Quality | Ruff, mypy, pytest, Vulture, Bandit, pip-audit, gitleaks, pre-commit, GitHub Actions | Criterion 1 |
| Not using | LlamaIndex, LanceDB, FastAPI, Next.js, Vercel, Hugging Face Spaces, Docker | Not needed. Fewer moving parts and less code to keep clean |

So the "Lang" stack is LangGraph for orchestration, LangChain for every model call and for runbook search, and LangSmith for tracing.

---

## 3. How a run flows

```
Upload -> Log Reader/Classifier -> Remediation -> [Cookbook Synthesizer  ||  JIRA Ticket*] -> Notification -> Results
               ^                       |
               +--- needs more context-+   (one retry per issue at most)

* JIRA only runs if at least one issue is critical
```

1. **Upload.** We accept .log, .txt and .json, up to 5 MB. Lines over a set length get cut. Each session has a rate limit.
2. **Log Reader / Classifier.**
   - Parse each line into `line_no, timestamp, level, service/host, message, error_code, request/trace id`. We handle plain-text app logs, JSON lines, nginx access and error logs, and Kubernetes/syslog.
   - Group lines by error signature, with numbers, IDs and IPs masked out. For example: `Connection refused to db:<port>` seen 412 times, first and last time seen, and a few example line numbers.
   - Keep WARN, ERROR and FATAL groups plus a few lines of context around them, so a deploy or config change just before the errors isn't lost.
   - Classify with structured output: `id, title, category, severity, confidence, evidence_lines, affected_service`.
3. **Remediation.** For each issue, search the runbook store (LangChain retriever, top 3 with similarity scores) and return `issue_id, fix steps, rationale, sources, confidence`. If the best score is under a threshold, or confidence is low, the issue goes back to the Log Reader for a wider context window and gets one more try. You can see this loop happen in the graph.
4. **Cookbook Synthesizer and JIRA Ticket run in parallel.**
5. **Notification runs after both finish**, so the Slack post can include the checklist and the ticket links.
6. **Results.** Graph with live node status, parsed fields table, pattern groups, issue/fix/rationale/source rows, checklist, Slack and JIRA outcome, tokens, cost and time per agent, and the trace link.

---

## 4. Slack and JIRA: what talks to what

### 4.1 The four connections

| Direction | How | Built by |
|---|---|---|
| App to JIRA | REST API: search, create, comment, link issues, add remote links | Our code |
| App to Slack | Bot token: post the summary, reply in thread, get the message link | Our code |
| JIRA to Slack | Jira Automation rules post into the channel when a ticket is assigned or resolved | Configured in JIRA, no code |
| Slack to JIRA | Jira Cloud app for Slack: ticket previews in the channel, assign, comment and change status from Slack | Installed in Slack, no code |

The app doesn't hear about changes in Slack or JIRA as they happen. That would need a small server running all the time, and Streamlit can't receive incoming requests. The app does check JIRA at the start of each run, though, so a ticket someone closed changes what the app does next time (see 4.2). JIRA is the only memory we have between runs, and we don't need a database.

### 4.2 When a ticket or message gets created

| What the run finds | JIRA | Slack |
|---|---|---|
| Nothing, or only low-severity issues | Nothing | Nothing. Results stay in the app so the channel doesn't fill up with noise |
| Medium or high issues, nothing critical | Nothing | One summary message, with the checklist in a thread |
| A new critical issue | A new ticket for each critical issue | Summary with a "View ticket" button for each |
| A critical issue with an open ticket already | No new ticket. We add a comment: seen again, run ID, count, time window | Summary says "OPS-12 updated, seen 3 times" |
| A critical issue whose ticket was closed | New ticket labelled `regression`, linked to the old one | Summary marks it as a regression |
| More than 3 critical issues | Tickets for the 3 most severe only | All listed, with "+N more not ticketed" |
| JIRA is down or the token is wrong | Nothing written, so nothing gets duplicated | Summary still posts, with a warning that tickets failed |
| Slack is down or the token is wrong | Not affected | Error shown in the app; results stay there |
| Dry run (no credentials, or the live switch is off) | Nothing sent | Nothing sent. The app shows exactly what it would have sent |

**How we spot a repeat.** Every pattern gets a short hash of its masked signature. The ticket carries it as a label, `sig-<hash>`, alongside `incident-suite`. Before creating anything, the JIRA agent runs a JQL search:
`project = OPS AND labels = "sig-<hash>" ORDER BY created DESC`
If the newest match isn't in the Done status category, we comment on it. If it is Done, we open a new ticket and link it to that one.

### 4.3 Order of calls in one run

1. JIRA agent: for up to 3 critical issues, search, then create or comment. Regressions get linked to the old ticket.
2. Notification agent: post the summary to the channel with `chat.postMessage` and keep the message `ts`.
3. Reply in that thread with the full checklist and the fix for each issue.
4. Get the message link with `chat.getPermalink`.
5. Back-link: on each ticket touched in step 1, add remote links to the Slack message and the LangSmith trace.

Both clients retry on 429 and 5xx with backoff, and respect Slack's `Retry-After` header. Every call has a timeout. If something fails, the app records it on screen and moves on rather than crashing.

### 4.4 What goes into the messages

**Slack summary (top-level post):** severity colour, log file name, run ID, line count and pattern count, the top issues with their evidence line numbers and a one-line fix each, ticket buttons, and a trace link.
**Slack thread reply:** the full checklist and the full fix for each issue, with sources.
**JIRA ticket:**
- Title: `[CRITICAL] <issue title> (<service>)`
- Priority: critical maps to Highest, high to High
- Labels: `incident-suite`, `sig-<hash>`, plus `regression` where it applies
- Description: what happened, evidence lines, fix steps, why, runbook sources, run ID
- Remote links: the Slack thread and the trace

All text from logs is escaped before it goes into Slack mrkdwn or JIRA. We never turn a URL found in a log into a link.

### 4.5 No-code pieces (team sets these up, about 20 minutes)

**Jira Cloud app for Slack**
- Install it into the workspace from the Slack Marketplace and connect our Atlassian site.
- Invite it to `#incident-alerts`. Ticket links then show as preview cards, with buttons to assign, comment and change status.
- Leave its own channel notifications off, so people don't get the same news twice. The automation rules below handle those.

**Jira Automation rules** (Project settings, then Automation)
1. Resolved: when an issue moves to Done and its labels include `incident-suite`, send a Slack message: "OPS-12 resolved by <name>: <summary>".
2. Picked up: when an issue labelled `incident-suite` gets assigned, send "OPS-12 picked up by <name>".
- Both rules post through a Slack incoming webhook. It belongs to the same Slack app, just a different credential from the bot token.
- The free JIRA plan has a monthly cap on automation runs. Two event-based rules won't come close to it, but we skip scheduled rules for that reason.

### 4.6 Stretch goal, only if we're ahead at H7

When an open ticket is seen again, reply in the original incident's Slack thread instead of only mentioning it in the new summary. To do this, we store the Slack channel and `ts` in a JIRA issue property (`incident_suite.slack`) when we first post. It's around 30 minutes of work, and no new infrastructure.

### 4.7 Ideas for later (README "What's next", not building now)

- Paging on-call through PagerDuty or Opsgenie for critical issues
- Pulling logs straight from Datadog, CloudWatch, Grafana Loki or Elasticsearch, or starting a run from an Alertmanager alert
- Slack buttons and a slash command (`/analyze`, Acknowledge, Create ticket, Mark resolved), which need a small backend that stays running
- JIRA webhooks feeding back into the app, so we learn which fixes actually resolved things
- GitHub: link incidents to recent deploys, or open a PR for a config fix
- Confluence: publish the checklist or a postmortem draft
- Jira Service Management incident issue types instead of plain tasks
- Microsoft Teams or email as alternatives to Slack

---

## 5. Traceability

- Each issue keeps its evidence lines, both line numbers and raw text.
- Each fix names its runbook source and the issue ID it belongs to.
- Each run has a run ID and a fingerprint of the log. Both show up in the app, in Slack and in JIRA, next to the trace link.
- Slack links to JIRA, and JIRA links back to Slack, so you can start from either one.

## 6. Scaling to big logs

- Grouping by pattern turns 50,000 lines into a few dozen patterns before the model sees anything.
- Whatever is still too big gets split into chunks, classified separately, then merged by signature.
- Each run has a fixed token budget, at most 3 tickets, and at most one retry per issue.
- One of the samples is a 50k-line log. The README and the video show its line count, pattern count, tokens and cost.

---

## 7. Sample data and evaluation

- 17 labelled logs across all four formats. They cover database connectivity, OOM, disk full, TLS certificate expiry, auth failures, 5xx spikes, timeouts, crash loops, bad config, rate limiting, DNS and queue backlog, with severities from low to critical. One of them is the 50k-line log.
- `labels.json` lists the expected issues, category and severity for each log.
- `eval/run_eval.py` reports category accuracy, severity accuracy, critical recall (did a ticket get opened when it should have), and average tokens, cost and time. The results table goes into the README.

---

## 8. Configuration

| Variable | Needed? | What it's for |
|---|---|---|
| `OPENAI_API_KEY` | Yes, for live analysis | LLM calls and embeddings |
| `CLASSIFIER_MODEL`, `REMEDIATION_MODEL`, `EMBEDDING_MODEL` | No (defaults set) | Which models to use |
| `LIVE_INTEGRATIONS` | No (default `false`) | Only actually send to Slack and JIRA when this is `true` |
| `SLACK_BOT_TOKEN` | For live Slack | Bot token (`xoxb-...`) with the `chat:write` scope |
| `SLACK_CHANNEL_ID` | For live Slack | Channel ID of `#incident-alerts` |
| `JIRA_BASE_URL` | For live JIRA | e.g. `https://yourteam.atlassian.net` |
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | For live JIRA | Account of the bot user |
| `JIRA_PROJECT_KEY` | For live JIRA | e.g. `OPS` |
| `JIRA_ISSUE_TYPE` | No (default `Task`) | Issue type to create |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | No | Tracing |
| `MAX_UPLOAD_MB`, `MAX_TICKETS_PER_RUN`, `RUN_TOKEN_BUDGET` | No (defaults set) | Limits |

Locally these live in `.env`, which is git-ignored. On Streamlit Cloud they go in the app's Secrets settings (TOML format). `config.py` reads both, so the code doesn't care where it's running.

---

## 9. Repo layout

```
c10-g08/
├── app.py                      Streamlit UI only, no business logic
├── incident_suite/             the Python package
│   ├── __init__.py
│   ├── graph.py                LangGraph: nodes, routing, feedback loop, parallel step
│   ├── state.py                Pydantic models: LogLine, Pattern, Issue, Remediation, Checklist, RunResult
│   ├── config.py               reads all settings in one place
│   ├── security.py             upload checks, rate limit, escaping
│   ├── telemetry.py            run ID, tokens, cost, timing, trace link
│   ├── parsing/                format detection, line parser, signature grouping, pre-filter
│   ├── agents/                 log_reader.py, remediation.py, cookbook.py, jira_agent.py, notification.py
│   ├── prompts/                one .md prompt per agent, shipped as package data
│   ├── rag/
│   │   ├── runbooks.py         loads and splits the markdown runbooks
│   │   └── retriever.py        LangChain InMemoryVectorStore + OpenAI embeddings, cached
│   └── integrations/
│       ├── slack_client.py     post, thread reply, permalink, retries
│       ├── jira_client.py      JQL search, create, comment, issue link, remote link, retries
│       └── payloads.py         builds Slack blocks and JIRA documents (pure functions)
├── data/runbooks/              about 15 markdown runbooks
├── sample_logs/                17 logs, labels.json, and a script that generates the 50k log
├── eval/run_eval.py            accuracy report for the README
├── tests/
│   ├── conftest.py             shared fixtures: fake LLM, fake embeddings, fake Slack and JIRA clients
│   ├── unit/                   mirrors the package: parsing/, agents/, rag/, integrations/, test_security.py
│   └── integration/            graph routing and one end-to-end run with all fakes
├── docs/                       this plan, architecture diagram, screenshots, Slack/JIRA setup guide
├── .streamlit/config.toml      theme and upload size limit
├── .github/
│   ├── workflows/ci.yml        lint, types, dead code, tests, security scans
│   └── dependabot.yml          weekly update PRs for pip and GitHub Actions
├── .editorconfig               same indentation, line endings and final newline in every editor
├── .gitattributes              LF line endings in the repo, so Windows and Linux diffs stay clean
├── .gitignore
├── .pre-commit-config.yaml     Ruff, mypy, gitleaks, end-of-file and whitespace fixers
├── .env.example                every variable, with placeholder values
├── pyproject.toml              project metadata, dependencies, and Ruff/mypy/pytest/coverage config
├── requirements.txt            pinned, generated from pyproject.toml (never edited by hand)
├── Makefile                    setup, run, test, check, eval, lock
├── SECURITY.md                 how to report a vulnerability
├── LICENSE                     MIT
└── README.md
```

**Notes on the layout**

- **Flat layout, not `src/`.** The `src/` layout is what PyPA recommends for libraries, because it stops tests from accidentally importing the local folder instead of the installed package. We're shipping an app, not a library, and Streamlit Community Cloud runs `app.py` straight from the repo root. With `src/` we'd need an extra install step in `requirements.txt` just to make imports work on the host, and that's one more thing that can break a deploy. Flat is equally clean and common for apps. pytest finds the package through `pythonpath = ["."]` in `pyproject.toml`.
- **One source for dependencies.** We list dependencies once, in `pyproject.toml`. `make lock` generates the pinned `requirements.txt` from it with pip-tools (`pip-compile`). Streamlit Cloud reads `requirements.txt`. Nobody edits that file by hand, so the two never drift apart. CI fails if `requirements.txt` is out of date.
- **Prompts as package data.** Prompt files are declared under `[tool.setuptools.package-data]`, so they travel with the package and load through `importlib.resources`, not hard-coded paths.
- **Tests mirror the code.** `tests/unit/parsing/test_parser.py` tests `incident_suite/parsing/parser.py`, and so on, so it's obvious where each test lives and what's missing. Shared fakes live in `conftest.py`, so no test needs a real key or network access.
- **Line endings.** Most of the team is on Windows. `.gitattributes` (`* text=auto eol=lf`) and `.editorconfig` stop Git from showing every file as changed because of CRLF vs LF.
- **Dependabot and `SECURITY.md`.** Small, but they show the repo is maintained responsibly, and GitHub displays both in the repo's Security tab.
- **Nothing generated gets committed.** No `__pycache__`, `.venv`, `.env`, coverage reports, embedding caches or Streamlit secrets. `.gitignore` covers all of them.

---

## 10. Timeline (about 10 working hours)

| Block | Work |
|---|---|
| H1 | Repo scaffold exactly as in section 9, with all tooling, CI, Dependabot, `.gitattributes`, `.editorconfig` and `SECURITY.md`. Config and state models. Sample logs, labels and runbooks. Deploy a bare app to Streamlit Cloud to make sure the dependencies install there |
| H2 | Parser, format detection, grouping and pre-filter, with tests |
| H3 | Log Reader/Classifier, runbook retriever, Remediation with issue IDs |
| H4 | Wire up the graph: feedback loop, parallel step, Notification after the join. Runs end to end from the command line |
| H5 | JIRA client (search, create, comment, link, remote link) and Slack client (post, thread, permalink). Dry run first, then live. Team sets up the Jira Slack app and the automation rules at the same time |
| H6 to H7 | Streamlit UI: live graph, parsed table, issue/fix rows, integrations panel, cost panel, sample picker. Stretch goal 4.6 if we're ahead |
| H8 | Redeploy with LangSmith. Run the eval. Clean-code and security pass. **Feature freeze.** |
| H9 | README with diagram, eval table and screenshots. Someone who didn't write the code follows the README from a fresh clone, on Windows and Linux |
| H10 | Record and edit the video. Add links to the README. Final CI check, submit |

Dropping LlamaIndex and LanceDB saves roughly half an hour in H3 and removes the biggest install risk on the host.

---

## 11. Project description vs. what we build

| From the project description | Where it's covered |
|---|---|
| Upload ops logs for live analysis | Upload and sample picker, live node status (sections 1, 3) |
| Log reader parses, categorizes and extracts fields | Parser, field extraction, grouping, classification (3.2) |
| Remediation maps each issue to fixes and rationale | One fix per issue ID, with rationale and sources (3.3) |
| Notification pushes solutions to Slack | Summary plus thread with checklist and fixes (4.3, 4.4) |
| Cookbook creates actionable checklists | Do now / this week / prevent (3.4) |
| JIRA creates tickets for critical issues | One per critical issue, with dedupe, regressions and a cap (4.2) |
| Orchestrator manages flow with LangGraph | Routing, loop, parallel step, diagram in the app (3) |
| Agents collaborate over structured logs | Remediation sends issues back to the Log Reader for more context (3.3) |
| Traceable, actionable output via Slack and JIRA | Evidence lines, sources, run ID, two-way links (4.4, 5) |
| Cross-tool notification | App to Slack and JIRA, JIRA to Slack, Slack to JIRA (4.1) |
| Scalable workflow | Grouping, chunking, fixed budgets, 50k-line proof (6) |

---

## 12. Winning criteria in detail

### 12.1 Clean code

No dead code
- Ruff, with the unused import, variable and argument rules on. Vulture in CI for unused functions.
- No commented-out code, notebooks or scratch files in the repo.

No bugs
- Type hints everywhere, checked with mypy.
- Every agent's input and output is a Pydantic model. Model output is validated against a schema, with one retry if it doesn't parse.
- Tests that run without any key: parser for each format, grouping, pre-filter, graph routing (loop fires, JIRA skipped when nothing is critical), retriever with fake embeddings, Slack and JIRA payload builders, dedupe decisions (open, closed, new), upload checks.
- One end-to-end test with a fake LLM and fake Slack and JIRA clients.
- If an agent fails, the user sees a clear message and the run carries on where it can. No raw stack traces on screen.

No security risks
- Secrets only come from env or Streamlit secrets. `.env` is ignored, `.env.example` holds placeholders, and gitleaks runs in pre-commit and CI.
- Log content is data, never instructions. It's wrapped in delimiters, and the agents have no tools that run commands or open URLs.
- Uploads: extension allow-list, size cap, UTF-8 with replacement characters, line-length cap. We never write files using a name the user gave us.
- Abuse limits: per-session rate limit, token budget, ticket cap. Live integrations are off by default on the public app.
- Anything sent to Slack or JIRA is escaped. No links from user content.
- Dependencies are pinned. pip-audit and Bandit run in CI.
- Least privilege: the bot token only has `chat:write`, the JIRA account only has access to the OPS project, and the OpenAI key has a spend cap.

Readable
- One job per module, short functions, docstrings on public functions, prompts kept in their own files.
- Tests mirror the package structure, and shared fakes live in one `conftest.py`.
- Same formatting everywhere: Ruff format, `.editorconfig`, and LF line endings through `.gitattributes`.
- Commits go feature by feature with clear messages.

Maintained
- Dependencies are declared once in `pyproject.toml` and locked into `requirements.txt` with `make lock`. CI checks the lock file is current.
- Dependabot opens weekly update PRs for pip packages and GitHub Actions.
- `SECURITY.md` explains how to report a problem.

### 12.2 Public repo
- MIT licence. Repo description and topics set (langgraph, langchain, multi-agent, devops, sre, streamlit, slack, jira).
- gitleaks scan of the full history before we make it public.
- Badges: CI, licence, Python version, live demo.

### 12.3 Demo video (target 2:40)

| Time | Shot |
|---|---|
| 0:00 | The problem: it's 3 AM and you have 50k lines of logs. One line on what we built |
| 0:15 | Architecture diagram: five agents and the orchestrator |
| 0:35 | Live run on the deployed app with a critical sample. The graph lights up, the parsed table fills in, the feedback loop fires on one issue, then the issue/fix rows and the checklist appear |
| 1:25 | Slack: the summary arrives with ticket buttons, and the checklist is in the thread. The ticket link shows a JIRA preview card |
| 1:45 | JIRA: the ticket has evidence lines and links back to Slack and the trace. Move it to Done, and Slack posts "resolved" |
| 2:05 | Run the same log again: it comments on the open ticket instead of creating a duplicate. Then the 50k-line log, with its pattern count and cost |
| 2:25 | Eval table, green CI, repo and live links |

Record at 1080p with OBS or Loom, with a written voice-over script. Upload unlisted to YouTube or Loom. Record a backup take early in H10.

### 12.4 Deployed prototype
- Streamlit Community Cloud, deployed from `main`. Every push redeploys.
- Judges don't need to set anything up. The sample buttons work, the Slack and JIRA payload previews are always visible, and we include screenshots of real Slack and JIRA output.
- Free apps sleep when idle. Before judging: open the app to wake it, run each sample once, check secrets, check the LangSmith project.
- The first deploy happens in H1, so any surprises show up early.
- If Streamlit Cloud itself has problems on the day, we still have the recorded video and the local setup in the README.

### 12.5 README
Sections: title and links, the problem, screenshots, architecture (Mermaid), how it works, Slack and JIRA integration, tech stack, run it locally, connecting Slack/JIRA/LangSmith, configuration, tests and eval, results, security, deployment, project layout, limitations and future work, team and licence. Someone who didn't write the code tests it from a fresh clone in H9. A first version is already in the repo folder.

---

## 13. Accounts to set up (team)

- **OpenAI**: a key just for this hackathon, with a $10 spend cap. Revoke it afterwards.
- **Slack**:
  1. Create a workspace and a `#incident-alerts` channel.
  2. At api.slack.com/apps, create an app called "Incident Suite". Under OAuth & Permissions, add the bot scope `chat:write` and install it to the workspace. Copy the bot token (`xoxb-...`).
  3. In the channel, run `/invite @Incident Suite`. Copy the channel ID from the channel details.
  4. In the same app, turn on Incoming Webhooks and add one for `#incident-alerts`. Only the Jira Automation rules use this one.
  5. Install the Jira Cloud app from the Slack Marketplace, connect the Atlassian site, and invite it to the channel.
- **JIRA**:
  1. Create a free Atlassian Cloud site and a Kanban project with key `OPS`.
  2. Check that the Priority field is on the create screen.
  3. Create an API token at id.atlassian.com, under Security, then API tokens. Ideally use a separate bot account.
  4. Add the two automation rules from 4.5.
- **LangSmith**: get an API key at smith.langchain.com. Project name `c10-g08`.
- **GitHub**: akanani1/c10-g08, public.
- **Streamlit Community Cloud**: sign in at share.streamlit.io with GitHub and give it access to the repo.

---

## 14. Risks

| Risk | What we'll do |
|---|---|
| Streamlit Cloud app goes to sleep | Wake it before judging and before recording |
| Streamlit Cloud has an outage on the day | The recorded video and the local setup steps still show everything working |
| A dependency won't install on Streamlit Cloud | We find out in H1 with the bare deploy. With LlamaIndex and LanceDB gone, the dependency list is small |
| OpenAI rate limits or outage during the demo | Keep a recorded backup take, cache results for the sample logs, show a clear error in the app |
| Runbook embeddings get recomputed on every page load | Cached with `st.cache_resource`, so it happens once per app start (about 15 small docs, a fraction of a cent) |
| Slack rate limit (about 1 message per second per channel) | One summary plus one thread reply per run, and we respect `Retry-After` |
| JIRA free-plan automation limits | Event rules only, no scheduled rules |
| Public users spamming our Slack and JIRA | Live integrations are off by default; we turn them on only for the recorded demo |
| JIRA API details have changed (for example the JQL search endpoint) | Check Atlassian's current REST docs in H5 before writing the client |

---

## 15. Decisions log

| Decision | Outcome |
|---|---|
| Deadline | 8 PM IST, Sunday 11 Oct. Target submission 6:30 PM IST (final checks by 6:00 PM) |
| UI | Streamlit |
| Hosting | Streamlit Community Cloud (confirmed allowed in the brief) |
| Orchestration and model calls | LangGraph and LangChain, OpenAI models |
| Runbook search | LangChain in-memory vector store with OpenAI embeddings. LlamaIndex and LanceDB dropped |
| Tracing | LangSmith, optional |
| Slack | Bot token (Web API), plus an incoming webhook only for Jira Automation |
| JIRA | Cloud REST API v3, one ticket per critical issue, label-based dedupe, cap of 3 |
| Two-way sync | Jira Cloud app for Slack and two Jira Automation rules, no code |
| Repo layout | Flat layout (package at the repo root), tests mirroring the package, details in section 9 |
| Dependencies | Declared once in `pyproject.toml`; `requirements.txt` generated with pip-tools |

## 16. Still open

- Who owns what. The team decides later.
- Team review of this plan, in progress.
- GitHub connection for Claude (later).
- Keys: OpenAI, Slack bot token, channel ID and webhook, JIRA site, email, token and project key, LangSmith.

---

## 17. Stretch goal: run replay with an animated agent diagram

Status: optional. Only if time allows. Nothing in sections 0 to 16 depends on it, and nothing there changes because of it.

### 17.1 Idea

A "Replay this run" view that walks through a finished run one step at a time, with the agent diagram animating as work moves from one agent to the next. The idea comes from an agent-loop walkthrough page we liked. Theirs is scripted by hand; ours plays back real recorded runs, so what judges step through is what the app actually did.

### 17.2 What it shows

Three panels, plus controls and counters at the top.

| Panel | Content |
|---|---|
| The log | The uploaded log, with the lines this step is looking at highlighted |
| This step | What the current agent received and what it returned: issues, fixes, checklist items, ticket or Slack payload, tokens and cost |
| Where we are | The LangGraph diagram, animated (see 17.3) |

- **Controls:** Reset, Prev, Next, Play till the end.
- **Counters:** step x of N, current agent, model calls so far, tokens and cost so far, issues found, tickets created.
- **Caption:** one plain-English sentence per step. For example, "The Log Reader grouped 50,000 lines into 23 patterns" or "Remediation isn't confident about issue 3, so it's sending it back for more context". Captions come from simple templates over the recorded step, with no extra model call.
- **Glossary** under the diagram: pattern, severity, feedback loop, dry run.

### 17.3 Animation

- The active agent glows. Finished agents get a tick, and skipped ones (JIRA when nothing is critical) fade out.
- A pulse travels along the arrow when work passes from one agent to the next.
- The feedback loop from Remediation back to the Log Reader has its own animation, so the retry is easy to spot.
- Cookbook Synthesizer and JIRA Ticket light up at the same time, to show they run in parallel.

### 17.4 How we'd build it

- **Recording (part of the core build, a few minutes of work).** While a run executes, we save each LangGraph step's update to a run record: node name, timestamp, input summary, output, tokens and cost. It's useful for debugging even if the replay never gets built, and it means adding the replay later needs no agent changes.
- **Recorded sample runs.** Saved as JSON for 2 or 3 sample logs, under `sample_logs/replays/`. These play with no API key, so judges can step through a full run even if OpenAI or the live app has a bad moment.
- **Player.** One self-contained file, `incident_suite/ui/run_player.html`, with an inline SVG diagram, CSS and a little vanilla JavaScript. It's embedded with Streamlit's `components.html`, and the controls run in the browser, so stepping is smooth. No npm, no build step, no libraries loaded from the web.
- **Live runs** keep the plain highlighted diagram from the core plan. The animated player is only for finished or recorded runs.

### 17.5 Safety and clean code

- Run data goes into the player as escaped JSON. Log text is never inserted as raw HTML.
- Recorded runs never contain secrets, tokens or webhook URLs. A test checks this.
- The caption templates and the step-to-diagram mapping are pure Python functions with their own unit tests.

### 17.6 Priority, cost and cut-off

| Order | Item | Time |
|---|---|---|
| 1 | Core build (sections 0 to 16) | as planned |
| 2 | Stretch A: reply in the original Slack thread (section 4.6) | about 30 min |
| 3 | Stretch B: this replay with the animated diagram | about 2 to 2.5 h |

- **Cut-off:** if the core UI isn't done by the end of H7, Stretch B is dropped. Feature freeze at H8 doesn't move.
- **Fallback:** if there's only a little time, ship a simple step slider with the highlighted (not animated) diagram and the captions.
- **If it ships:** the README gets a short "Replay a run" section, and the demo video can pause on the feedback-loop step.

---

## 18. Additions from the team's UI mockups (v7, 10 Oct 2026)

Status: agreed. This section adds to sections 0 to 17 without rewriting them. Where it changes something earlier, it says so, and 18.5 lists every such change in one place. Everything else in the earlier sections stays as it is.

### 18.1 What we're adopting and what we're not

| From the mockups | Decision | Why |
|---|---|---|
| Secret and PII redaction before the model sees the log | Adopt (18.2) | Small, and it strengthens the security criterion |
| Human approval before anything is sent to Slack or JIRA | Adopt (18.3) | Cheap with LangGraph `interrupt()`, and it demos well |
| Incident card, pipeline strip, Slack and JIRA previews, runbook sources, run log table, severity legend | Adopt (18.4) | Matches what our run already produces |
| Next.js, FastAPI, Vercel, LlamaIndex, LanceDB, Slack webhook for posting | Skip | Dropped on purpose in v5 and v6 (section 2) |
| Separate Guardrails and Policy/Compliance agents | Skip | Upload checks and redaction cover it without extra agents |
| Multi-tenancy, malware scanning, WORM audit store, Microsoft Teams | Skip | Days of work, not hours. Teams stays in "Ideas for later" (4.7) |
| Fleet KPIs, trend charts, environment and service filters | Skip | Need a history of runs, and we deliberately store none (4.1). Filling them with made-up numbers would hurt us |

### 18.2 Secret and PII redaction

- **Where:** a new step between Upload and the Log Reader. Module `incident_suite/parsing/redact.py`.
- **What it masks:** email addresses, bearer tokens and JWTs, AWS access keys, private key blocks, and the values in `password=`, `passwd=`, `secret=`, `token=`, `api_key=` and similar key/value pairs. Each match becomes `[REDACTED:<type>]`.
- **How:** regex only, run on every line before grouping, so it's cheap even on the 50k-line log. The signature masking in section 3 (numbers, IDs, IPs) is unchanged and runs after it.
- **Everything downstream sees redacted text:** the model, evidence lines in the app, Slack, JIRA, and recorded replays (section 17).
- **On screen:** a count per type, for example "3 emails, 1 AWS key redacted".
- **Tests:** a positive and a negative example for each pattern, plus a test that no Slack payload, JIRA payload or recorded replay contains a planted sample secret.
- **Limits:** pattern-based, so it can miss things. The README says so.
- **Time:** about 30 minutes, in H2.

### 18.3 Approval gate

When `REQUIRE_APPROVAL` is true (the default), the flow in section 3 becomes:

```
Upload -> Redact -> Log Reader -> Remediation -> [Cookbook || JIRA plan*] -> Notification plan -> Approval gate** -> JIRA send* -> Slack send -> Results
                        ^                |
                        +-- more context-+   (unchanged: one retry per issue at most)

*  JIRA only runs if at least one issue is critical (unchanged)
** Only pauses when something would actually be sent
```

- **Plan and send.** The JIRA and Notification agents each split into two halves. The plan half is read-only: the JQL search, the create / comment / regression decision, and building the Slack blocks and JIRA documents. The send half does the writes, exactly as in section 4.3 steps 1 to 5.
- **How it pauses.** LangGraph `interrupt()` with the in-memory checkpointer (`MemorySaver`). The thread ID is the run ID. The compiled graph and checkpointer live in `st.session_state`, so a Streamlit rerun doesn't lose the paused run.
- **Approve** resumes the graph and sends. **Reject** ends the run with nothing sent, and the rejection is recorded in the run log and shown in the app.
- **When it pauses.** Only when live integrations are on, credentials are present, and the run found at least one medium issue. In dry run, the run ends at the gate with the previews on screen and nothing to approve.
- **Identity.** Approval is per session and there are no user accounts. We record the time and "approved in app", not a verified approver.
- **New row for the table in 4.2:** rejected at the approval gate → JIRA: nothing; Slack: nothing; the rejection is logged in the app.
- **Config:** `REQUIRE_APPROVAL` (default `true`). Setting it to `false` sends straight away, as in the original section 4.3.
- **Tests:** dry run stops at the gate; reject makes zero calls on the fake Slack and JIRA clients; approve sends exactly once; a paused run resumes after a simulated rerun.
- **Time:** about 40 minutes, across H4 (graph) and H6 (panel).

### 18.4 Results screen layout

This builds on the results list in sections 1 and 3, step 6.

| Piece | What it shows | Where the data comes from |
|---|---|---|
| Incident card (one per top issue) | Severity badge, title, service, run ID, root cause, recommended fix, confidence with its reasons | Classifier and Remediation output |
| Pipeline strip | Each step with status (done, running, skipped, waiting for approval, failed) and time taken | Run record (17.4) |
| Slack preview | The summary and thread exactly as they will be or were sent | Notification plan payload |
| JIRA preview | Ticket title, priority, labels, status, and create / comment / regression | JIRA plan payload |
| Approval panel | Approve and Reject buttons, plus what will be sent | Approval gate (18.3) |
| Runbook sources | The runbooks cited, with match scores | Remediation output |
| Run log table | Time, step, agent, action, status, evidence | Run record (17.4) |
| Cost row | Tokens, cost and time per agent, and the trace link | Telemetry |
| Severity legend | Low, medium, high, critical, and what each one triggers | The rules in 4.2 |

- **New `root_cause` field** in the Remediation output (adds to section 3, step 3): one or two sentences, framed as a hypothesis, citing evidence line numbers.
- **Confidence reasons come from real signals only:** how many evidence lines support the issue, the best runbook match score, and whether JIRA has seen this signature before. No made-up numbers.
- **Render helpers** live in `incident_suite/ui/components.py` as pure functions with unit tests. `app.py` calls them. Log text is escaped before it reaches any HTML.
- **Time:** inside H6 to H7.

### 18.5 Changes to earlier sections, in one place

| Section | Change |
|---|---|
| 3 (flow) | Redaction step, and the approval gate when `REQUIRE_APPROVAL` is true (18.2, 18.3) |
| 3, step 3 | Remediation also returns `root_cause` (18.4) |
| 4.2 | New row for a rejected approval (18.3) |
| 8 | New variable `REQUIRE_APPROVAL`, default `true` |
| 9 | New files `incident_suite/parsing/redact.py` and `incident_suite/ui/components.py`, with matching tests under `tests/unit/` |
| 10 | H2 +30 min (redaction), H4 and H6 +40 min (approval), H6 to H7 follow the layout in 18.4 |
| 12.1 | Security also covers redaction before the model, and a human approval before any external write |
| 12.3 | Around 1:20, click Approve on camera before the Slack shot |
| 14 | Two new risks, in 18.6 |

### 18.6 New risks

| Risk | What we'll do |
|---|---|
| A paused run is lost when Streamlit reruns the script | Keep the graph and checkpointer in `st.session_state`, and test resuming |
| Redaction misses a secret format | Documented as a limitation. Live integrations stay off on the public app, and the evidence shown is redacted text |

### 18.7 Decisions (adds to section 15)

| Decision | Outcome |
|---|---|
| Teammate mockups | Used as the visual brief for the UI. Stack and agent list unchanged |
| Redaction | Regex-based, before the Log Reader, in `parsing/redact.py` |
| Human approval | LangGraph `interrupt()` before JIRA and Slack writes, on by default |
| Dashboard metrics | Per-run only. No fleet KPIs or trend charts |

---

## 19. Stretch C: extras if we have 2 to 3 more hours

Status: optional, not agreed yet. Nothing in sections 0 to 18 depends on it.

These compete for the same time as Stretch B, the animated replay (section 17). Our suggestion is Stretch C first: each item ships on its own and can be cut without affecting the others, and they add product value rather than presentation.

| Order | Item | Time | What it adds |
|---|---|---|---|
| 1 | "Seen before" fix memory | 30 to 45 min | When a closed JIRA ticket has the same `sig-<hash>` label, pull its resolution comment and show "last time this was fixed by…". Reuses the JQL search from 4.2, and gives the confidence score a real reason |
| 2 | Incident timeline | about 45 min | Errors per minute over the log's time range, with deploy and config-change lines marked. Real data from the parsed log, and it backs up a root cause like "started after the deploy" |
| 3 | "Ask about this incident" chat | 45 to 60 min | `st.chat_input` that answers questions using only this run's issues, evidence lines and fixes, and cites line numbers. Log text stays inside delimiters as data, as in 12.1 |
| 4 | Postmortem draft download | about 30 min | A markdown postmortem (summary, timeline, root cause, fixes, follow-ups) built from the run, downloaded with `st.download_button` |
| 5 | Paste logs as text | about 15 min | A text box next to the uploader, going through the same upload checks and redaction |

- **Cut-off:** the same as 17.6. Nothing starts after H7, and feature freeze at H8 doesn't move.
- **If they ship:** each gets a line in the README and a test. Items 1 and 2 are worth a few seconds in the video.

---

## 20. Stretch C item: Agent settings page

Status: agreed as a Stretch C item (section 19), about 1 to 1.5 h. It ranks after "seen before" fix memory and the incident timeline, and ahead of the chat. The idea comes from an admin "Agents Setup" screen a team member liked: one card per agent with model, prompt and options.

### 20.1 What the page shows

A second Streamlit page, "Agent settings", with one card per agent and a global card.

| Card | Settings |
|---|---|
| Every agent | Model (dropdown from an allow-list in `config.py`, with the price per million input and output tokens), system prompt (editor that starts from the file in `prompts/`, with "Reset to default") |
| Log Reader / Classifier | Lines of context kept around each error |
| Remediation | Runbook top-k, and the confidence threshold that triggers the retry loop |
| JIRA Ticket | Maximum tickets per run, and which severity opens a ticket |
| Global | Run token budget, `REQUIRE_APPROVAL` on or off, which redaction types are on (18.2), dry run or live |
| Integrations | Slack, JIRA and LangSmith shown as connected or not connected. Read-only, never the keys |

### 20.2 Where changes are kept

- **Per session only**, in `st.session_state`. Each visitor's changes affect only their own runs. Nothing is written to disk: Streamlit Cloud has no database and wipes its disk on restart, and a shared save would let any visitor change the prompts for everyone.
- **Export and import** the settings as JSON (`st.download_button` and `st.file_uploader`), validated against the settings model before use.
- **Reset all** returns to the defaults from `config.py` and the prompt files.

### 20.3 Safety

- Keys stay in env or Streamlit secrets. The page never shows them, and there is no field to enter one.
- Models come from the allow-list only. No free-text model names.
- If any prompt differs from its default, the run is forced to dry run, unless an admin passcode from secrets (`ADMIN_PASSCODE`) is entered for the session. Edited prompts can't post to our real Slack or JIRA.
- Numbers have bounds (for example top-k 1 to 10, at most 3 tickets, a token budget ceiling), enforced by the settings model, not just the widgets.
- Imported JSON goes through the same validation. Edited prompts still wrap log text in delimiters as data (12.1).

### 20.4 How it fits the code

- **Core build, H1 (about 15 min):** a Pydantic `RunSettings` model in `state.py` (or `config.py`) holding everything in 20.1, built from the defaults in config. It travels in the graph state, and agents read it from there instead of from config directly. This is cleaner even if the page never ships, and it makes the page a UI-only job.
- **Stretch:** `pages/agent_settings.py` for the UI. The prompt diff check and the "force dry run" rule are pure functions in `incident_suite/` with unit tests.
- **Tests:** settings validation (bounds, allow-list, bad JSON), forced dry run when a prompt is edited, passcode unlocks live, defaults round-trip through export and import.

### 20.5 Not doing

- Multiple providers (OpenRouter, Gemini). OpenAI only, as in section 2. LangChain makes adding one later a small change.
- Stream, regular and batch modes. They don't matter for one log at a time.
- Saving settings for all users. That would need a database and logins.

### 20.6 Demo moment

Switch the Remediation model on this page, rerun the same sample, and show cost and output side by side. A few seconds in the video if it ships.

### 20.7 Changes to earlier sections

| Section | Change |
|---|---|
| 8 | New optional variable `ADMIN_PASSCODE` |
| 9 | New `pages/agent_settings.py`; `RunSettings` in the package |
| 10 | H1 +15 min for `RunSettings` in the graph state |
| 19 | One more item in the Stretch C list, ranked third |

---

## 21. Team and ownership

Status: agreed, 10 Oct 2026. This settles "Who owns what" in section 16. Only Ashvin uses Claude, so the code path stays with him, and everyone else takes work that runs in parallel without it. Each person owns their own folders, so nobody hits merge conflicts.

### 21.1 Who does what

| Person | Role | Owns | Folders and outputs |
|---|---|---|---|
| **Ashvin** (with Claude) | Lead, core code | Repo scaffold and CI; state models and `RunSettings`; parser and redaction; agents and prompts; the LangGraph graph and approval gate; Slack and JIRA clients; Streamlit UI; reviewing and merging pull requests | `incident_suite/`, `app.py`, `tests/`, `.github/` |
| **Raphael** | Integrations and deploy | OpenRouter key with a credit limit (section 22); Slack workspace, app, bot token and webhook; JIRA site, OPS project, API token and the two automation rules; Jira Cloud app in Slack; LangSmith; Streamlit Cloud app and its secrets. Later: test the live Slack and JIRA flows end to end (new ticket, repeat comment, regression, "resolved" message) and take screenshots | Accounts, `docs/setup-slack-jira.md`, screenshots |
| **Gulam Mohammad** | Data | The 17 sample logs and `labels.json`; about 15 runbooks (DB pool, OOM, disk, TLS, auth, 5xx, timeouts, crash loop, config, rate limit, DNS, Kafka lag and so on). Later: run the eval and fill in the README results table | `sample_logs/`, `data/runbooks/`, eval results |
| **Safwan** | QA, docs and demo | Manual test checklist, run after every deploy; planted-secret examples for the redaction tests; architecture diagram; README fresh-clone test on Windows; video script, recording and editing; demo-day checklist (wake the app, run the samples) | `docs/`, the video, bug reports as GitHub issues |

### 21.2 Timeline

| When | Ashvin + Claude | Raphael | Gulam Mohammad | Safwan |
|---|---|---|---|---|
| Today, evening | Scaffold, state models, parser, OpenRouter smoke test, bare deploy | All accounts and keys | First 5 logs and 5 runbooks | Video script, test checklist |
| Tomorrow, morning | Agents, graph, Slack and JIRA clients | Live Slack and JIRA testing from about H5 | Remaining logs, runbooks and `labels.json` | Test each deploy, file issues |
| Tomorrow, afternoon | UI, then feature freeze around 3 PM | Screenshots, final secrets check | Eval run, README results table | README fresh-clone test, screenshots, recording at about 4 PM |
| Tomorrow, 6:00 PM | Final links, video and CI check | Live integrations back off | | Demo-day checklist |
| Tomorrow, 6:30 PM | Submit | | | |

### 21.3 Ground rules

- **Claude helps the others through Ashvin.** Before they start, Claude drafts the templates they work from: an example log in each format, a runbook template, the `labels.json` schema, the test checklist and the video script outline.
- **A branch per person, pull requests into `main`.** Ashvin reviews each pull request. Nobody edits another person's folders.
- **No secrets in git or chat.** Raphael shares keys privately and puts them straight into Streamlit secrets and each person's `.env`.
- **Two check-ins:** tonight before stopping, and tomorrow around 1 PM.
- **Syncing after a pull from GitHub:** `git fetch` and then `git reset origin/main` (or `git pull` when you have no local changes). Never `git reset --hard` with unsaved work.

---

## 22. Model provider: OpenRouter instead of OpenAI

Status: agreed, 10 Oct 2026. All model calls and embeddings go through OpenRouter with one key, `OPENROUTER_API_KEY`. Everything else in the stack (section 2) stays the same and is free: Streamlit Community Cloud, GitHub and Actions on a public repo, Slack free plan, Jira Cloud Free, LangSmith Developer.

### 22.1 Why it still fits the plan

- OpenRouter serves both chat models and embeddings (`POST /api/v1/embeddings`, for example `openai/text-embedding-3-small`), so runbook search (RAG) stays as planned.
- LangChain's existing OpenAI classes work against it by changing the base URL. No new framework.
- One key reaches models from several providers, which also makes the model list on the Agent settings page (section 20) easy to widen later.

### 22.2 How we wire it

```python
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

llm = ChatOpenAI(
    model=settings.classifier_model,          # e.g. "openai/<model>"
    base_url=OPENROUTER_BASE_URL,
    api_key=settings.openrouter_api_key,
)

embeddings = OpenAIEmbeddings(
    model=settings.embedding_model,           # e.g. "openai/text-embedding-3-small"
    base_url=OPENROUTER_BASE_URL,
    api_key=settings.openrouter_api_key,
    check_embedding_ctx_length=False,          # send plain text, not OpenAI token IDs
)
```

- `check_embedding_ctx_length=False` matters. By default LangChain turns text into OpenAI token IDs before sending it, and providers other than OpenAI can reject that.
- The base URL lives in `config.py`, not scattered through agents.
- Exact model names get picked in H1, with prices in the same allow-list the cost panel and the Agent settings page use.

### 22.3 H1 smoke test (about 5 minutes, before anything is built on it)

One chat call and one embedding call with the real key, run as a small script and kept as an opt-in integration test (skipped when no key is set, so CI stays key-free). If embeddings fail, fall back to calling the OpenRouter embeddings endpoint directly with a tiny LangChain `Embeddings` wrapper.

### 22.4 Cost and limits

- Put a **credit limit on the key** in OpenRouter (replaces the $10 OpenAI spend cap in section 13). Revoke it after the hackathon.
- Use a cheap paid model for the demo. The free `:free` models are rate-limited and can be busy, which is risky on camera.
- The run token budget, the ticket cap and the per-session rate limit (section 12.1) stay as they are.

### 22.5 Free-plan limits worth knowing

| Service | Limit | Effect on us |
|---|---|---|
| Jira Cloud Free automation | 100 rule runs a month | Our two rules only fire on assign and resolve. Don't burn runs testing them; our app's API calls don't count |
| LangSmith Developer | 5,000 traces a month | Far more than we need |
| Streamlit Community Cloud | Sleeps when idle | Wake it before judging (section 12.4) |

### 22.6 Changes to earlier sections

| Section | Change |
|---|---|
| 2 | "LangChain with OpenAI" becomes "LangChain with OpenRouter"; embeddings through OpenRouter |
| 8 | `OPENAI_API_KEY` becomes `OPENROUTER_API_KEY`. Model variables hold OpenRouter model names |
| 10 | H1 adds the smoke test in 22.3 |
| 13 | OpenAI account step becomes: create an OpenRouter key with a credit limit (Raphael) |
| 14 | New risk: OpenRouter embeddings not drop-in compatible. Mitigation in 22.3 |
| 15 | New decision: OpenRouter as the single model provider |
| 20.5 | Multiple providers become possible through OpenRouter's one key, if time allows |

---

## 23. Supported log formats

Status: agreed, 10 Oct 2026. Logs arrive in many shapes, so upload accepts more than plain text. This extends section 3, step 1 (Upload) and step 2 (parsing). Everything else in the flow stays the same: whatever the format, the parser turns it into the same `LogLine` records before grouping, so the agents never see the difference.

### 23.1 Accepted formats

| Format | Extensions | How we read it |
|---|---|---|
| Plain text | `.log`, `.txt` | Line by line. Format detection as in section 3 (app logs, nginx access and error, Kubernetes/syslog) |
| JSON lines | `.jsonl`, `.ndjson`, `.json` with one object per line | One record per line |
| JSON document | `.json` holding an array of objects, or an object with a list inside (e.g. `{"logs": [...]}`) | One record per array item |
| CSV / TSV | `.csv`, `.tsv` | Header row gives the column names; delimiter sniffed |
| Parquet | `.parquet` | Read with `pyarrow` in batches, one record per row |
| Gzip | any of the above with `.gz` (e.g. `app.log.gz`) | Decompressed as a stream, then read as the inner format |
| Pasted text | text box (Stretch C) | Same as plain text |

Not supported (README says so): Windows `.evtx`, binary journald files, Excel, and archives holding several files (`.zip`, `.tar`).

### 23.2 Detection

- We look at the content, not just the extension: Parquet files start with `PAR1`, gzip with `1f 8b`, JSON with `{` or `[`, and CSV is confirmed with Python's `csv.Sniffer`.
- If the content disagrees with the extension, the content wins and the app says what it detected ("Detected CSV, 3 columns").
- If nothing matches, the file is read as plain text, with replacement characters for bad bytes.

### 23.3 Structured files: mapping columns to fields

CSV, TSV, Parquet and JSON records have named fields, not one text line. We map them onto `LogLine` by common names, case-insensitive:

| `LogLine` field | Column names we look for |
|---|---|
| `timestamp` | `timestamp`, `@timestamp`, `time`, `ts`, `datetime`, `date` |
| `level` | `level`, `severity`, `loglevel`, `log_level`, `lvl` |
| `service` / `host` | `service`, `app`, `application`, `component`, `host`, `hostname`, `pod`, `container` |
| `message` | `message`, `msg`, `log`, `text`, `event`, `description` |
| `error_code` | `error_code`, `code`, `status`, `status_code` |
| `trace_id` | `trace_id`, `traceid`, `request_id`, `correlation_id`, `span_id` |

- Columns we don't recognise are kept as extra fields and shown in the parsed table.
- If no message column exists, the message is built from the remaining columns as `key=value` pairs, so grouping and the model still have text to work with.
- The line number becomes the row number, and evidence on screen, in Slack and in JIRA says "row 412" for structured files.
- The app shows the mapping it used, so a wrong guess is visible.

### 23.4 Limits and safety

- **Upload cap** stays at `MAX_UPLOAD_MB` (default 5) for the uploaded bytes. Compressed formats can expand a lot, so there are also caps after reading: at most 50 MB decompressed for `.gz`, at most 200,000 rows or lines, at most 200 columns, and the existing line-length cap per cell or line. Going over a cap stops the read with a clear message rather than running out of memory.
- **Gzip** is decompressed as a stream with the size cap enforced as we go, which protects against zip bombs.
- **Parquet** is read with `pyarrow` only, in batches, with no pickle or custom-type loading.
- **JSON** has a nesting-depth limit; nested objects are flattened with dotted keys (`http.status`) up to that depth.
- **Redaction (18.2)** runs on every field of every record, not just `message`.
- We never write the uploaded file to disk under the user's file name (12.1).

### 23.5 Code and dependencies

- New module `incident_suite/parsing/readers.py`: one small reader per format, all returning the same records, plus `detect_format()`.
- New dependency: `pyarrow` for Parquet. CSV, TSV, JSON and gzip use the standard library, so we don't need pandas just for this. The H1 bare deploy checks `pyarrow` installs on Streamlit Cloud.
- `.streamlit/config.toml` and the uploader's allowed types list the new extensions.
- **Tests** (all key-free): one per format, wrong extension vs real content, column mapping with odd names, missing message column, malformed CSV row, invalid JSON, gzip over the size cap, a Parquet file with too many rows.
- **Time:** about 45 to 60 minutes, in H2 with the parser.

### 23.6 Sample logs, updated to cover every format

The 17 samples keep the same scenarios but spread across the formats. Gulam writes the text, JSON and CSV files by hand; the Parquet and gzip files are generated from them by `sample_logs/make_samples.py` (Claude drafts the script), because they're binary.

| # | File | Format | Scenario | Severity |
|---|---|---|---|---|
| 1 | `payment_db_conn_refused.log` | Plain text | DB pool exhausted, `Connection refused`, starts after a deploy | Critical |
| 2 | `orders_oom_killed.log` | Kubernetes/syslog | Memory climbing, `OOMKilled`, pod restart | Critical |
| 3 | `api_disk_full.txt` | Plain text (syslog) | `No space left on device`, failed writes | Critical |
| 4 | `gateway_tls_expired.jsonl` | JSON lines | Certificate-expiry warnings, then handshake failures | High |
| 5 | `auth_bruteforce.json` | JSON array | Burst of failed logins from a few IPs, lockouts | High |
| 6 | `nginx_5xx_spike.log` | Nginx access | 502/503/504 spike on `/api/checkout` | High |
| 7 | `search_upstream_timeouts.tsv` | TSV | `upstream timed out` to the search service | Medium |
| 8 | `worker_crashloop.log` | Kubernetes | `CrashLoopBackOff`, back-off restarts | Critical |
| 9 | `inventory_bad_config.txt` | Plain text | Missing env var / bad YAML at startup | High |
| 10 | `api_rate_limited.csv` | CSV | 429s from a third-party API, retries | Medium |
| 11 | `notify_dns_failures.log` | Syslog | `NXDOMAIN`, name resolution failures | High |
| 12 | `kafka_consumer_lag.parquet` | Parquet (generated) | Consumer lag rising, backlog building | Medium |
| 13 | `healthy_service.csv` | CSV | Mostly INFO, a few harmless WARNs | Low (nothing sent) |
| 14 | `mixed_incident.json` | JSON document (`{"logs": [...]}`) | Deploy, then DB errors, timeouts and 5xx together | Critical + High |
| 15 | `payment_db_conn_refused_repeat.log` | Plain text | Same signature as #1, later: tests repeat comment and regression | Critical |
| 16 | `edge_nginx_error.log.gz` | Gzip of nginx error log (generated) | Body too large, 404 floods, minor noise | Low |
| 17 | `large_50k_mixed.parquet` | Parquet (generated) | 50,000 rows, ~20 patterns hidden in noise: scaling, pattern count, cost | Mixed |

`labels.json` refers to line numbers for text files and row numbers for structured files.

### 23.7 Changes to earlier sections

| Section | Change |
|---|---|
| 3, step 1 | Upload accepts the formats in 23.1, detected as in 23.2 |
| 3, step 2 | Structured records are mapped to `LogLine` as in 23.3 before grouping |
| 6 | Row caps and streaming reads (23.4) also protect the 50k-row sample |
| 7 | Sample set as in 23.6 |
| 9 | New `incident_suite/parsing/readers.py` and `sample_logs/make_samples.py` |
| 10 | H2 +45 to 60 min |
| 12.1 | Upload checks also cover decompressed size, row and column caps |
| 2 | New dependency `pyarrow` |
