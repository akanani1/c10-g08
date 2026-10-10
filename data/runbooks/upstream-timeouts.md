---
title: Upstream timeouts
id: upstream-timeouts
categories: [timeout]
typical_severity: medium
services: [search-svc, inventory-svc, checkout, any service that calls another]
signals:
  - "upstream timed out (110: Connection timed out) while reading response header from upstream search-svc:9200"
  - "Call to inventory-svc timed out after 5000ms"
  - "timed out after 5000ms"
---

# Upstream timeouts

## Symptoms

- `upstream timed out (110: Connection timed out) while reading response header from upstream <host>:<port>`.
- `Call to <service> timed out after 5000ms`, sometimes with a 504 to the client.
- Request durations stuck at exactly the timeout value (5000 ms).
- Only requests that use one upstream are affected; the rest stay fast.

## Likely causes

1. The upstream is overloaded: CPU saturated, or its thread or connection pool is full. It needs more capacity.
2. The upstream's own dependency is slow (a database, a search cluster, a third-party API).
3. A heavy query or a missing index after a data or code change.
4. Retries piling up and multiplying the load on a service that is already behind.
5. A network problem between the two services.

## Diagnose

```bash
kubectl top pods -l app=search-svc
kubectl logs deploy/search-svc --since=15m | grep -iE "slow|took|timeout|queue"
```

- Identify which upstream is timing out (it is named in the message) and what share of requests is affected.
- Compare the caller's timeout with the upstream's normal latency. A timeout below the p99 fails requests even when nothing is wrong.
- Count retries per original request to see whether they are adding load.
- For a database or search backend, look at slow-query logs and cluster health.

## Fix now

1. Add capacity to the slow upstream named in the error (for example scale out search-svc or inventory-svc), or restart instances that are stuck.
2. Reduce load on it: lower the caller's concurrency, cut retries to one with backoff, turn off expensive features.
3. If one query is responsible, stop it and cache or block that request.
4. If it started with a deploy on either side, roll back.
5. Raising the timeout is a last resort. It only helps if the upstream is slow but still finishing.

## Prevent

- Set each timeout a little above the measured p99, and shorter at each hop down the call chain.
- Add a circuit breaker and a fallback (cached or partial results) for the slow dependency.
- Use retry budgets and exponential backoff with jitter.
- Alert on p95 and p99 latency per upstream, before the timeouts start.
- Load-test upstreams at expected peak traffic.

## References

- nginx docs: `proxy_read_timeout`, `proxy_connect_timeout`
- Runbooks: `http-5xx-spike`, `db-pool-exhausted`
