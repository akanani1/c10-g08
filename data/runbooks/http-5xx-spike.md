---
title: HTTP 5xx spike (502, 503, 504)
id: http-5xx-spike
categories: [http_5xx]
typical_severity: high
services: [checkout, api-gateway, nginx, any service behind a proxy]
signals:
  - "POST /api/checkout HTTP/1.1 502"
  - "POST /api/checkout HTTP/1.1 503"
  - "POST /api/checkout 503"
  - "Upstream checkout returned 503"
---

# HTTP 5xx spike (502, 503, 504)

## Symptoms

- A sudden rise in 502, 503 or 504 responses in the nginx or gateway access log.
- Often one route only, for example `POST /api/checkout`, while other routes stay at 200.
- Gateway errors such as `Upstream checkout returned 503`.
- Small, identical response sizes (the proxy's own error page).

## Likely causes

| Code | Meaning | Usual cause |
|---|---|---|
| 502 | Upstream gave an invalid response or closed the connection | Upstream crashed, restarting or mid-deploy |
| 503 | No upstream available | All instances unhealthy, overloaded, or a circuit breaker is open |
| 504 | Upstream didn't answer in time | A slow dependency, usually a database or another service |

A 5xx from a proxy is almost never the proxy's fault. It reports that the service behind it is down, overloaded or slow.

## Diagnose

```bash
# Which routes and codes
awk '$9 ~ /^5/ {print $7, $9}' access.log | sort | uniq -c | sort -rn | head
# Health of the upstream behind the failing route
kubectl get pods -l app=checkout
kubectl logs deploy/checkout --since=15m | grep -iE "error|fatal|refused|timeout"
```

- Find the scope: which route, which upstream, what share of requests.
- Find what changed just before the spike: a deploy, a scale-down, a config reload.
- If the upstream is running but failing, follow the chain to its own dependency (database, cache, another service). That is the real fault.

## Fix now

1. Check the health of the upstream behind the failing route (for checkout: the checkout pods and their readiness).
2. If the spike started with a deploy of that upstream, roll back.
3. If instances are crashing or unhealthy, restart them and add replicas.
4. If the upstream's dependency is failing, fix that first using its runbook (`db-pool-exhausted`, `upstream-timeouts`). The 5xx is the symptom.
5. Confirm the 5xx rate on the route is back to baseline.

## Prevent

- Alert on 5xx rate per route, so a broken checkout isn't hidden by healthy traffic.
- Readiness probes, so traffic only reaches instances that can serve it.
- Rolling deploys with `maxUnavailable: 0` and connection draining.
- A canary for each release, with automatic rollback if its error rate rises.

## References

- nginx docs: `ngx_http_upstream_module`
- Runbooks: `db-pool-exhausted`, `upstream-timeouts`, `deploy-rollback`
