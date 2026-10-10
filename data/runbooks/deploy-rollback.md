---
title: Errors start right after a deploy (roll back)
id: deploy-rollback
categories: [database, http_5xx, crash_loop, config, memory]
typical_severity: high
services: [any service that was just deployed or reconfigured]
signals:
  - "Deploy started: payment-api v2.14.0 -> v2.15.0 (by ci-bot)"
  - "Deploy finished: payment-api v2.15.0, 3/3 pods ready"
  - "Deploy finished: checkout v5.2.0"
  - "Rollout finished: worker v1.9.0"
  - "Config reloaded from git commit"
  - "Config loaded: db.pool.max_size=5"
---

# Errors start right after a deploy (roll back)

## Symptoms

- A `Deploy finished`, `Rollout finished` or `Config reloaded` line, and errors beginning within a minute or two.
- The service was healthy before, with no change in traffic.
- The errors are new, or a known error at a much higher rate.
- Only instances running the new version are affected.

## Likely causes

1. A config value changed with the release (pool size, cache setting, a dependency's host, a timeout).
2. A bug in the new code.
3. A dependency the new version needs that this environment doesn't have.
4. A database migration the code doesn't match.
5. More replicas than a shared resource can serve (database connections, API quotas).

## Diagnose

```bash
kubectl rollout history deploy/<name>
git log --oneline <previous-tag>..<new-tag>
git diff <previous-tag>..<new-tag> -- config/
```

- Line up the times: the deploy line and the first error. Seconds or a few minutes apart is strong evidence; hours apart is weak.
- Read exactly what changed. Deploy and config lines often say it outright, for example a new `db.pool.max_size` or a moved host.
- Check whether the errors come only from new pods.
- Check whether the release included a database migration. That decides whether rolling back is safe.

## Fix now

1. Roll back. Don't debug in production while users are affected.
   ```bash
   kubectl rollout undo deploy/<name>
   kubectl rollout status deploy/<name>
   ```
2. If only config changed, revert the config commit and reload.
3. If a feature flag was turned on, turn it off.
4. If a migration makes rolling back unsafe, fix forward with the smallest change, or restore the old config values on the new version.
5. Confirm the error rate returns to its level from before the deploy. If it doesn't, the deploy wasn't the cause; keep looking.

## Prevent

- Canary every release and compare its error rate with the old version before continuing.
- Roll back automatically when error rate or latency crosses a threshold during a rollout.
- Ship config changes separately from code changes, so the cause is obvious.
- Keep database migrations backwards compatible, so a rollback is always possible.
- Print changed config values at startup, as these logs do.

## References

- Kubernetes docs: `kubectl rollout undo`
- Runbooks: `db-pool-exhausted`, `crash-loop`, `bad-config-startup`, `oom-killed`
