---
title: Kafka consumer lag
id: kafka-consumer-lag
categories: [queue]
typical_severity: medium
services: [order-consumer, orders, any Kafka consumer group]
signals:
  - "Consumer lag on orders-events partition 4 is 50203 messages (threshold 50000)"
  - "Scaled down order-consumer to 2 replicas"
  - "Consumer lag"
---

# Kafka consumer lag

## Symptoms

- `Consumer lag on <topic> partition <n> is <N> messages (threshold 50000)`, with N rising.
- Lag grows on all partitions (not enough capacity) or on one partition (a hot key or a stuck message).
- Downstream data gets stale: order status, stock levels, notifications.
- It often follows a change in the number of consumers, such as `Scaled down order-consumer to 2 replicas`.

## Likely causes

1. Too few consumers for the load, after a scale-down or an autoscaler decision.
2. Producers got faster: a traffic spike, a bulk import or a replay.
3. Processing got slower: a slow database or API call inside the consumer, or a recent release.
4. Batches take longer than `max.poll.interval.ms`, so consumers are removed and the group keeps rebalancing.
5. A poison message that fails and is retried forever, blocking one partition.

## Diagnose

```bash
kafka-consumer-groups.sh --bootstrap-server kafka.internal:9092 \
  --describe --group order-consumer
kubectl get deploy order-consumer
```

- Look at lag per partition and how many members the group has.
- Compare produce rate with consume rate. If produce went up, it's load. If consume went down, it's the consumers.
- Compare the number of consumers with the number of partitions.
- If one partition is stuck, find the offset it's stuck on and inspect that message.

## Fix now

1. Scale the consumers back up: `kubectl scale deploy/order-consumer --replicas=<n>`. Go up to the number of partitions; more consumers than partitions doesn't help.
2. If the consumers are already at the partition count, add partitions to the topic (this changes key ordering for new messages).
3. If batches are too slow, lower `max.poll.records` so each batch finishes inside the poll interval.
4. If a poison message blocks a partition, move it to a dead-letter topic.
5. Watch the lag fall. It should drop steadily once consume rate is above produce rate.

## Prevent

- Set a minimum replica count for consumers, and review any scale-down against peak produce rate.
- Autoscale consumers on lag (for example with KEDA), not on CPU.
- Alert on lag growth rate and on time-behind, not just on an absolute number.
- Add a dead-letter topic and a retry limit.
- Load-test consumers at peak produce rate.

## References

- Kafka docs: consumer groups, `max.poll.interval.ms`, `max.poll.records`
