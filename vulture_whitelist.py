"""Vulture whitelist: names Vulture can't see being used. Parsed by Vulture, never imported.

Pydantic fields and enum members are read through validation and serialisation
(``model_validate`` on model output, ``model_dump`` for Slack, JIRA, the UI and replays), which
Vulture can't follow. Only those belong here; anything else Vulture reports is real dead code.
Prune entries as the agents start reading them by name.
"""

# Enum members (values the models and readers produce)
AUTH
AWS_ACCESS_KEY
BEARER_TOKEN
COMMENT
CONFIG
CRASH_LOOP
CREATE
CREDENTIAL_PAIR
CSV
DEBUG
DISK
DNS
EMAIL
FATAL
HTTP_5XX
INFO
JSON_DOCUMENT
JSON_LINES
JWT
KUBERNETES
MEMORY
NGINX_ACCESS
NGINX_ERROR
PARQUET
PLAIN
PRIVATE_KEY
QUEUE
RATE_LIMIT
REGRESSION
SKIPPED_OVER_CAP
SYSLOG
TIMEOUT
TLS
TRACE
TSV
WARN

# Pydantic model fields and model_config
action
admin_passcode
affected_service
category
checklist
confidence
confidence_threshold
context_lines
count
do_now
error
error_code
errors
evidence_lines
example_line_nos
host
input_per_million
input_tokens
issue_ids
langsmith_api_key
langsmith_project
langsmith_tracing
line_count
line_no
log_fingerprint
log_format
log_name
message
min_severity
model_config
output_per_million
output_tokens
patterns
permalink
prevent
rationale
raw
redactions
related_key
root_cause
run_id
runbook
score
seconds
section
sent
service
signature
signature_hash
slack
sources
steps
text
this_week
tickets
timestamp
token_budget
trace_id
trace_url
url
