---
title: Rate limited by a third-party API (429)
id: third-party-rate-limit
categories: [rate_limit]
typical_severity: medium
services: [notify-svc, notify, any client of an external API]
signals:
  - "SMS provider returned 429 Too Many Requests, retrying in 1s"
  - "Giving up on SMS batch 21/60 after 3 retries"
  - "429 Too Many Requests"
---

# Rate limited by a third-party API (429)

## Symptoms

- `<provider> returned 429 Too Many Requests, retrying in <n>s`.
- `Giving up on <job> after 3 retries`, so some work is dropped.
- A feature that depends on an external API (SMS, email, shipping, maps) slows down or partly fails.
- The errors come in bursts during a batch job or a traffic peak.

## Likely causes

1. A batch job sends requests faster than the provider's quota allows.
2. Retries with little or no backoff, so each 429 creates more requests.
3. Traffic grew past the quota on the current plan.
4. Several services or environments share one API key and one quota.
5. No caching or batching, so more calls are made than needed.

## Diagnose

- Find the limit: read the response headers (`Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`) and the provider's docs.
- Measure your real request rate per minute to that API and compare it with the limit.
- Find the caller: which job or endpoint makes most of the requests, and whether its batch size or schedule changed recently.
- Check how retries behave: how long they wait, and whether they count against the same quota.

```bash
# 429s per minute from the CSV log
awk -F, '$5==429 {print substr($1,1,16)}' api.csv | sort | uniq -c
```

## Fix now

1. Slow the sender: lower the batch size and add a pause between batches, so the request rate stays under the quota.
2. Honour `Retry-After` and use exponential backoff with jitter. Never retry a 429 straight away.
3. Requeue the batches that were given up on, so they are sent once the rate is under control.
4. If the quota is too low for normal traffic, ask the provider for a higher quota.
5. Check that the 429 rate falls and no more batches are abandoned.

## Prevent

- Add a client-side limiter set a little below the provider's quota, so requests queue in your service.
- Send non-urgent messages through a queue that drains at a controlled rate.
- Use a separate API key per service and per environment.
- Alert on 429 rate and on quota use above 80%.
- Never drop work after retries: send it to a dead-letter queue for a later attempt.

## References

- RFC 6585: 429 Too Many Requests
- Provider docs: rate limits and quotas
