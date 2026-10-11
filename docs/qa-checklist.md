# TriageGraph QA test checklist

**Owner:** Safwan  
**Purpose:** Repeatable QA record for local runs and every Streamlit Cloud deploy  
**Source requirements:** `README.md`, `docs/build-plan.md`, `tests/`, and the application contract  
**Status values:** `PASS`, `FAIL`, `BLOCKED`, or `NOT LANDED`

This checklist is the execution record. Do not mark a test `PASS` from code inspection
alone. Record the tested commit, environment, evidence, and any issue filed.

## 1. Test-run record

Copy this section into a GitHub issue titled `Test run <date> <time>`.

| Field | Value |
|---|---|
| Test-run issue | |
| Commit / deploy SHA | |
| App URL | |
| Date and time (IST) | |
| Tester | Safwan |
| Browser and version | |
| Operating system | |
| Local or deployed | |
| App mode | Dry run / Live (demo only) |
| Previous deploy tested | |

### Result summary

| Result | Count |
|---|---:|
| PASS | |
| FAIL | |
| BLOCKED | |
| NOT LANDED | |

**Release decision:** `GO` / `NO-GO`  
**Blocking issue numbers:**  
**Evidence links:**  

## 2. Execution rules

1. Run the smallest applicable section after each deploy; run the complete checklist
   before submission.
2. Use dry run by default. Enable live integrations only with Raphael for a planned
   demo test, and never with real secrets in screenshots or recordings.
3. Test against the deployed commit: the latest `main` commit on GitHub, as shown under
   Streamlit Cloud's app settings.
4. A failed test requires a separate GitHub issue using the bug template in section 10.
5. Do not mark a feature `FAIL` when it has not landed. Mark it `NOT LANDED` and
   identify the owner or dependency.

## 3. P0 smoke tests: every deploy

| ID | Check | Expected result | Status / evidence |
|---|---|---|---|
| SMK-01 | Open the app URL | App opens; if asleep, it wakes within two minutes | |
| SMK-02 | Inspect the page title and main content | Title is `TriageGraph`; no red error box or raw traceback | |
| SMK-03 | Inspect the integrations panel | Models, Slack, and JIRA show connected/not connected status; no key or token is displayed | |
| SMK-04 | Inspect public mode | Public deployment reports `dry run` | |
| SMK-05 | Run the keyless app smoke test locally | `tests/integration/test_app.py::test_app_renders_without_keys` passes | |
| SMK-06 | Confirm the tested commit | Streamlit Cloud's deployed commit matches the SHA in this record | |

## 4. Current repository verification

These checks correspond directly to tests already present in the repository and can be
run before the complete agent flow lands.

| ID | Repository check | Expected result | Status / evidence |
|---|---|---|---|
| REP-01 | Run `pytest tests/unit/test_config.py` | Defaults, precedence, validation, and secret-safe representations pass | |
| REP-02 | Run `pytest tests/unit/parsing/test_readers.py` | Plain text, JSONL, JSON, CSV, TSV, Parquet, gzip, detection, row/line, and size limits pass | |
| REP-03 | Run `pytest tests/unit/parsing/test_parser.py` | Supported log formats, fields, levels, timestamps, and column mapping pass | |
| REP-04 | Run `pytest tests/unit/parsing/test_grouping.py` | Pattern grouping, signatures, context, sorting, and caps pass | |
| REP-05 | Run `pytest tests/unit/parsing/test_redact.py tests/unit/parsing/test_readers.py` | Fake secrets are redacted before parsed output; counts and disabled types behave correctly | |
| REP-06 | Run `pytest tests/unit/test_security.py` | Allowed formats pass; disallowed formats, empty files, and upload caps fail clearly | |
| REP-07 | Run `pytest tests/unit/test_state.py` | State validation, evidence requirements, severity ordering, settings bounds, and result totals pass | |
| REP-08 | Run `pytest tests/integration/test_sample_logs.py` | All 17 sample logs match their format and labelled evidence, the files match `make_samples.py`, and every labelled issue finds its runbook | |
| REP-09 | Run `pytest tests/integration/test_app.py` | Streamlit renders without API keys or an exception | |
| REP-10 | Run `pytest tests/unit/agents tests/unit/rag tests/unit/prompts tests/unit/test_telemetry.py` | Log Reader, Remediation, Cookbook, runbook search, prompts and cost tracking pass with a fake model | |
| REP-11 | Run `pytest` | The whole suite passes without an API key | |
| REP-12 | Run `pytest -m live -v` (local only, needs `OPENROUTER_API_KEY` in `.env`) | 4 passed: two chat models, embeddings, and one real classification of `payment_db_conn_refused.log` | |

## 5. Upload and parsing acceptance tests

Run these in the app after the upload flow is available. Use harmless synthetic fixtures
and the examples supplied by the data owner.

| ID | Test | Expected result | Status / evidence |
|---|---|---|---|
| UPL-01 | Upload `.log` and `.txt` plain text | Format is detected and line numbers start at one | |
| UPL-02 | Upload `.jsonl` / `.ndjson` | Records parse into common fields; malformed text is retained or reported clearly | |
| UPL-03 | Upload JSON array and `{"logs": [...]}` | Structured records parse; non-object items do not crash the app | |
| UPL-04 | Upload `.csv` and `.tsv` | Column mapping is shown and evidence uses row numbers | |
| UPL-05 | Upload `.parquet` | Rows parse and the mapping is shown | |
| UPL-06 | Upload `.gz` | Inner format is detected; nested gzip is refused clearly | |
| UPL-07 | Rename a CSV to `.log` | Content detection identifies it as CSV and explains the detection | |
| UPL-08 | Upload a file larger than `MAX_UPLOAD_MB` (default 5 MB) | Upload is refused with a clear message | |
| UPL-09 | Upload `.zip`, `.tar`, `.exe`, `.xlsx`, an extensionless file, and bare `.gz` | Each is refused with a clear message | |
| UPL-10 | Upload an empty or whitespace-only file | Clear validation error; no crash or traceback | |
| UPL-11 | Upload a 100 KB single line | File is accepted and the line is truncated safely | |
| UPL-12 | Upload gzip content over the 50 MB decompressed cap | Read is refused before excessive memory use | |
| UPL-13 | Upload data over the 200,000-row/line or 200-column cap | Read is refused with a clear limit message | |
| UPL-14 | Upload a 50,000-row sample | Run shows line/row count, pattern count, tokens, and cost without a timeout | |

## 6. Redaction and privacy tests

Create `planted_secrets.log` locally with fake values only. Never commit real credentials,
and never paste the fixture into a public issue. The fixture should cover the same cases
as `tests/unit/parsing/test_redact.py`: email, JWT, bearer token, AWS access key,
credential key/value, named secret columns, and a private-key block.

| ID | Test | Expected result | Status / evidence |
|---|---|---|---|
| SEC-01 | Upload `planted_secrets.log` | Every planted value becomes `[REDACTED:<type>]` before display or model processing | |
| SEC-02 | Inspect the parsed table and evidence | No original planted value is visible; line/row evidence remains usable | |
| SEC-03 | Inspect Slack/JIRA previews in dry run | No original planted value appears in either preview | |
| SEC-04 | Inspect the redaction summary | Count by type is shown, for example `3 emails, 1 AWS key redacted` | |
| SEC-05 | Search the page with Ctrl+F | Each planted secret is absent from the rendered page | |
| SEC-06 | Inspect logs, errors, and screenshots | No API key, token, `.env` content, or private key is exposed | |
| SEC-07 | Run the redaction and reader unit tests | `test_redact.py` and `test_readers.py` pass | |

## 7. End-to-end flow and approval gate

These tests are `NOT LANDED` until the final graph and UI are available.

| ID | Test | Expected result | Status / evidence |
|---|---|---|---|
| E2E-01 | Run critical sample `payment_db_conn_refused.log` (#1) | Graph nodes light up in order; parsed issues include evidence; JIRA path is selected | |
| E2E-02 | Run the healthy sample `healthy_service.csv` (#13) | No JIRA ticket is planned and the app says nothing would be sent to Slack | |
| E2E-03 | Observe an uncertain remediation | Remediation requests more context from Log Reader and then continues | |
| E2E-04 | Run in dry run | Slack/JIRA previews are visible; nothing is sent; run stops at approval | |
| E2E-05 | Reject a prepared live run (local/demo only) | Nothing is sent and rejection is recorded in the run log | |
| E2E-06 | Approve a prepared live run (local/demo only) | Each planned action sends once; result links are shown | |
| E2E-07 | Refresh during approval | Paused run remains recoverable after refresh | |
| E2E-08 | Inspect results | Incident cards show severity, service, run ID, root cause, fix, confidence, evidence, status, cost, and sources | |
| E2E-09 | Check failure handling | User sees a clear message, not a Python traceback or success-shaped fallback | |
| E2E-10 | Resize to phone width | Layout remains usable and does not hide critical controls | |

## 8. Slack and JIRA integration tests

Run only with Raphael and a disposable test channel/project. Keep live integrations off
after the test and remove or label test tickets.

| ID | Test | Expected result | Status / evidence |
|---|---|---|---|
| INT-01 | New critical issue | One JIRA ticket is created; Slack summary has a `View ticket` button | |
| INT-02 | Run the same log again | No duplicate ticket; open ticket receives a comment; Slack reports updated count | |
| INT-03 | Close the original ticket and rerun | New ticket is labelled `regression` and linked to the old ticket | |
| INT-04 | Assign or resolve a JIRA ticket | Automation posts the expected message in `#incident-alerts` | |
| INT-05 | Inspect Slack thread and JIRA ticket | Checklist, fixes, evidence, Slack link, and trace link are present | |
| INT-06 | Use an invalid JIRA token | Slack summary still posts and clearly warns that ticket creation failed | |
| INT-07 | Capture integration evidence | Screenshots contain no keys, personal DMs, or unrelated customer data | |

## 9. Windows fresh-clone and release checks

Run on a machine that has not previously installed this repository.

| ID | Test | Expected result | Status / evidence |
|---|---|---|---|
| WIN-01 | Clone the repository and follow README setup verbatim in PowerShell | No undocumented step is required | |
| WIN-02 | Create and activate `.venv` | Activation and dependency installation succeed | |
| WIN-03 | Run the documented test command | All non-live tests pass without an API key | |
| WIN-04 | Start the app | Streamlit starts and opens the documented local URL | |
| WIN-05 | Run the README's developer commands (`pip install -c requirements.txt -e ".[dev]"`, `pre-commit install`, `pytest`) | Each works in PowerShell as written | |
| WIN-06 | Record friction | Every guess, missing dependency, platform mismatch, or unclear README step is documented | |
| WIN-07 | Run final CI checks | CI is green; no lockfile, lint, type, security, test, or secret-scan failure remains | |

## 10. Bug report template

Create one issue per failure. Do not combine unrelated failures.

```text
Title: [bug] <short user-visible failure>

Deploy / commit: <SHA>
Environment: <local/deployed, browser, OS>
Test ID: <for example UPL-08>
Severity: blocks demo / wrong output / security / cosmetic

Steps:
1. ...
2. ...
3. ...

Expected:
...

Actual:
...

Evidence:
<redacted screenshot, console output, or link>

Workaround:
...
```

Severity guidance:

- **blocks demo:** prevents the primary recording or submission flow
- **security:** exposes a secret, PII, token, or unsafe payload
- **wrong output:** produces an incorrect classification, fix, ticket, or notification
- **cosmetic:** does not affect correctness or demo completion

## 11. Final demo-day checklist

Complete this on submission day, after the final deploy.

- [ ] Confirm the final commit and CI status
- [ ] Wake the deployed app and verify the P0 smoke tests
- [ ] Run the exact samples used in the recording
- [ ] Confirm public mode is dry run after testing
- [ ] Confirm Slack/JIRA test tickets and messages are labelled or cleaned up
- [ ] Confirm README has live-demo, video, architecture, screenshots, setup, tests, and results links
- [ ] Confirm video is 2:30–2:55, captions are readable, and no secrets appear
- [ ] Confirm all blockers have an owner or an explicit fallback
- [ ] Complete the 6:00 PM final links, video, and CI check before the 6:30 PM submission target
