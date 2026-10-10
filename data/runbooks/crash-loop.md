---
title: Pod in CrashLoopBackOff
id: crash-loop
categories: [crash_loop]
typical_severity: critical
services: [worker, any Kubernetes workload]
signals:
  - "Back-off 10s restarting failed container (CrashLoopBackOff)"
  - "state=Terminated reason=Error exitCode=1"
  - "Cannot connect to redis://cache.internal:6379: connection refused"
  - "Rollout finished: worker v1.9.0 (redis host moved to cache.internal)"
  - "restartCount=1"
---

# Pod in CrashLoopBackOff

## Symptoms

- Kubelet: `Back-off <n>s restarting failed container (CrashLoopBackOff)` with a rising `restartCount`.
- `state=Terminated reason=Error exitCode=1` seconds after each start.
- A FATAL line at the end of each start attempt, for example `Cannot connect to redis://<host>:6379: connection refused`.
- It began right after a rollout.

## Likely causes

1. A release changed config so the process can't reach a dependency at startup (a moved Redis or database host, a wrong URL, a missing secret).
2. A bug in the new version that crashes on startup.
3. The dependency itself is down.
4. A liveness probe killing a slow starter.

| Exit code | Meaning |
|---|---|
| 1 or 2 | The application exited with an error |
| 137 | Killed, usually out of memory. See `oom-killed` |
| 143 | Terminated, often by a failing liveness probe |

## Diagnose

```bash
kubectl -n jobs logs worker-<id> --previous      # the crashed attempt, not the current one
kubectl -n jobs describe pod worker-<id>
kubectl -n jobs rollout history deploy/worker
```

- The last FATAL line before each exit says what failed. Read it first.
- Check what the rollout changed. A dependency host in the rollout message is the first suspect.
- Test the dependency from a debug pod: `redis-cli -h cache.internal -p 6379 ping`.

## Fix now

1. If it started with a rollout, roll back: `kubectl -n jobs rollout undo deploy/worker`.
2. If rolling back isn't possible, correct the config: point the redis host back at the address that works, then restart the pods.
3. If the dependency (redis) is really down, bring it back first; the pods will then start on their own.
4. Check that pods are `Running` and `Ready`, `restartCount` has stopped rising and jobs are being processed.

## Prevent

- Roll out with `maxUnavailable: 0`, so a version that can't start never replaces healthy pods.
- Change the dependency and the config that points at it in separate, ordered steps.
- Add a startup check in CI that boots the container against production-like config.
- Alert on restart count and on rollouts that haven't finished in 10 minutes.

## References

- Kubernetes docs: debugging pods, `CrashLoopBackOff`
- Runbooks: `deploy-rollback`, `bad-config-startup`, `oom-killed`
