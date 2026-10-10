---
title: Container OOMKilled
id: oom-killed
categories: [memory]
typical_severity: critical
services: [orders, any containerised service with a memory limit]
signals:
  - "state=Terminated reason=OOMKilled exitCode=137"
  - "Memory cgroup out of memory: Killed process 4410 (java)"
  - "memory_working_set=720Mi limit=768Mi"
  - "cache: product cache size 1.2M entries, eviction disabled"
  - "Rollout finished: orders v3.8.0 (cache.eviction=false)"
---

# Container OOMKilled

## Symptoms

- Kubelet reports `state=Terminated reason=OOMKilled exitCode=137` and restarts the container.
- Kernel log: `Memory cgroup out of memory: Killed process <pid> (java)`.
- Kubelet warnings show `memory_working_set` climbing towards `limit` before each kill.
- Application warnings about a growing cache, for example `eviction disabled`.
- The same pod is killed again at a regular interval after each restart.

## Likely causes

1. A cache with no size limit or with eviction turned off (`cache.eviction=false`), often changed by a release.
2. A memory leak in new code.
3. The memory limit is too low for the current load, or the JVM heap is set too close to the limit.
4. One large request or batch loads too much into memory at once.

## Diagnose

```bash
kubectl -n shop describe pod orders-<id> | grep -A5 "Last State"
kubectl -n shop top pod orders-<id> --containers
kubectl -n shop get deploy orders -o jsonpath='{.spec.template.spec.containers[*].resources}'
```

- A steady climb that resets at each restart means a leak or an unbounded cache. A sudden jump means one big request.
- Check what the last rollout changed. A cache or memory setting in the rollout message is the first suspect.
- Compare the heap size (`-Xmx`) with the container memory limit. The heap should be about 75% of it.

## Fix now

1. If a rollout turned cache eviction off, turn it back on (`cache.eviction=true`) or roll back the release.
2. Raise the container memory limit (for example from 768Mi to 1Gi) to stop the kills while you investigate. This buys time; it does not fix a leak.
3. Cap the cache size in config so it can't grow without limit.
4. Check that `memory_working_set` levels off below the limit and the restarts stop.

## Prevent

- Give every in-memory cache a maximum size and an eviction policy, and reject configs that disable eviction.
- Alert when memory stays above 85% of the limit for 10 minutes, and on any OOMKilled restart.
- Use `-XX:MaxRAMPercentage=75` so the heap follows the memory limit.
- Run a soak test before release and watch for memory that only goes up.

## References

- Kubernetes docs: resource limits, exit code 137
- Runbook: `crash-loop`, `deploy-rollback`
