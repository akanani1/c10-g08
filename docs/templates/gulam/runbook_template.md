---
title: Database connection pool exhausted
id: db-pool-exhausted
categories: [database]
typical_severity: critical
services: [any service with a SQL connection pool]
signals:
  - "Connection refused to <db-host>:<port>"
  - "pool: N/N connections in use, M waiting"
  - "too many connections"
  - "503 ... error_code=DB_UNAVAILABLE"
---

# Database connection pool exhausted

<!--
HOW TO USE THIS TEMPLATE (delete this comment in the real runbook)
- One problem per runbook. File name = id, e.g. data/runbooks/db-pool-exhausted.md
- Keep the "##" headings exactly as below: the retriever splits on them, so each section
  is searched on its own and cited as "db-pool-exhausted > Fix now".
- 250 to 600 words. Plain, imperative steps. Real commands in code blocks.
- Put the exact error phrases from your sample logs under `signals`. That is what makes
  search find this runbook for those logs.
- No real hostnames, IPs, emails, tokens or company names. Use example.com / .internal.
-->

## Symptoms

- Requests fail with 503 and `DB_UNAVAILABLE`, while the database itself may look healthy.
- Logs show `Connection refused` or `pool exhausted`, usually alongside a rising count of
  waiting requests.
- It often starts right after a deploy or a config change.

## Likely causes

1. The pool size was lowered by a config change or a new release (check `db.pool.max_size`).
2. Slow queries hold connections longer, so the pool drains.
3. The database hit its own `max_connections` because too many pods are connecting.
4. A network policy or security group change blocks the DB port.

## Diagnose

```bash
# Current pool settings the app loaded
kubectl logs deploy/payment-api | grep "db.pool"
# Connections on the database side
psql -c "select count(*), state from pg_stat_activity group by state;"
```

- Compare the time of the first error with the last deploy or config change.
- If the database refuses even from a debug pod, it's the network or `max_connections`,
  not the pool.

## Fix now

1. If a deploy changed the pool size, roll back: `kubectl rollout undo deploy/payment-api`.
2. Otherwise raise `db.pool.max_size` to its previous value and restart the pods one at a time.
3. Check that the error rate drops within 5 minutes.

## Prevent

- Alert when the pool is above 80% in use for 5 minutes.
- Add a config check in CI that rejects pool sizes below the agreed minimum.
- Load-test pool settings in staging before release.

## References

- Internal: service config conventions (example)
- PostgreSQL docs: `max_connections`
