---
title: Service fails to start because of bad config
id: bad-config-startup
categories: [config]
typical_severity: high
services: [inventory, any service that reads config at startup]
signals:
  - "Invalid YAML in /etc/inventory/config.yaml line 14: mapping values are not allowed here"
  - "Missing required env var INVENTORY_DB_URL referenced in config.yaml"
  - "Startup aborted: config.yaml is invalid"
  - "Config reloaded from git commit 9f3c2a1"
---

# Service fails to start because of bad config

## Symptoms

- `Invalid YAML in <file> line N: mapping values are not allowed here`.
- `Missing required env var <NAME> referenced in config.yaml`.
- `Startup aborted: config.yaml is invalid`, straight after `Config loaded from ...`.
- The service worked until a config reload or a deploy, and now fails the same way on every restart.

## Likely causes

1. A YAML syntax error from a manual edit: wrong indentation, a missing quote, a tab.
2. An environment variable or secret that was removed, renamed or never added to this environment.
3. A new version that needs a setting the environment doesn't have yet.
4. A value of the wrong type (text where a number is expected, a malformed URL).

## Diagnose

```bash
# What changed
git log -3 --stat -- inventory/config.yaml
# Is the file the service reads valid YAML?
python -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))" /etc/inventory/config.yaml
# Which variables does the process actually get?
kubectl exec deploy/inventory -- env | sort | grep INVENTORY
```

- The first error after `Config loaded` names the file and line, or the missing variable.
- The `Config reloaded from git commit <sha>` line tells you which commit to look at.

## Fix now

1. Revert the config commit that broke it (`git revert <sha>`) and let config-sync reload, then restart the service.
2. If you must fix forward: correct the YAML at the line named in the error, and validate the file before reloading.
3. If the error is a missing variable such as `INVENTORY_DB_URL`, add it to the environment or secret and restart.
4. Check for a normal startup in the log and a passing health check.

Restarting without changing the config will fail the same way every time.

## Prevent

- Validate config in CI: lint the YAML and check it against a schema before merge.
- Keep an example config listing every required variable, and check it against what the code reads.
- Deliver config through the same pipeline as code: review, validation, canary, easy revert. No manual edits on servers.
- Make the service report every config error at once at startup, not only the first.

## References

- YAML spec: block mappings
- Runbooks: `deploy-rollback`, `crash-loop`
