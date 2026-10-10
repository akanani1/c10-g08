---
title: Database connection pool exhausted
id: db-pool-exhausted
categories: [database]
typical_severity: critical
services: [payment-api, checkout, any service with a SQL connection pool]
signals:
  - "Connection refused to payments-db.internal:5432 (attempt 1/3)"
  - "Connection refused to orders-db.internal:5432"
  - "DB pool: 5/5 connections in use, 8 waiting"
  - "503 error_code=DB_UNAVAILABLE"
  - "Health check failed 3 times in a row: database unreachable"
  - "Config loaded: db.pool.max_size=5"
---

# Database connection pool exhausted

## Symptoms

- `Connection refused to <db-host>:5432`, usually with retry attempts (`attempt 1/3`).
- `DB pool: N/N connections in use, M waiting`: every connection is busy and requests queue.
- Requests fail with 503 and `error_code=DB_UNAVAILABLE`.
- `Health check failed ... database unreachable`, so pods are marked unhealthy.
- It often starts within a minute of a deploy or a config reload.

## Likely causes

1. A release lowered the pool size. Look for `Config loaded: db.pool.max_size=<n>` just before the first error.
2. More pods than before are connecting, so the database hit its own `max_connections`.
3. Slow queries or long transactions hold connections, so the pool drains.
4. The database is down or the network path to port 5432 is blocked.

## Diagnose

```bash
# Pool settings the app loaded, and when
kubectl logs deploy/payment-api | grep "db.pool"
# Connections on the database side
psql -h payments-db.internal -c "select count(*), state from pg_stat_activity group by state;"
psql -h payments-db.internal -c "show max_connections;"
```

- Compare the time of the first error with the last deploy or config change.
- If the pool warning (`N/N in use`) comes first, the pool is too small. If `Connection refused` comes with no pool warning, check the database and the network.
- From a debug pod: `pg_isready -h payments-db.internal -p 5432`.

## Fix now

1. If a deploy changed the pool size, roll back: `kubectl rollout undo deploy/payment-api`.
2. Otherwise set `db.pool.max_size` back to its previous value and restart the pods one at a time.
3. If the database is at `max_connections`, scale down the noisiest client or end idle sessions on the database.
4. Check that the pool shows free connections, health checks pass and the 503 rate drops within 5 minutes.

## Prevent

- Alert when the pool is above 80% in use for 5 minutes, and when requests are waiting.
- Add a CI check that rejects a `db.pool.max_size` below the agreed minimum.
- Keep `pods x max_size` for all services under the database `max_connections`, with headroom.
- Load-test pool settings in staging before release.

## References

- PostgreSQL docs: `max_connections`, `pg_stat_activity`
- Runbook: `deploy-rollback`
