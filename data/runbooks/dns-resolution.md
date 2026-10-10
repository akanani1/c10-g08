---
title: DNS resolution failures
id: dns-resolution
categories: [dns]
typical_severity: high
services: [notify, systemd-resolved, any service that connects by hostname]
signals:
  - "DNS lookup failed for smtp.mail-provider.example: NXDOMAIN"
  - "cannot resolve smtp.mail-provider.example (NXDOMAIN)"
  - "Using degraded feature set UDP instead of UDP+EDNS0 for DNS server 10.0.9.53"
  - "Configuration updated: nameserver 10.0.0.53 -> 10.0.9.53"
---

# DNS resolution failures

## Symptoms

- `DNS lookup failed for <hostname>: NXDOMAIN`.
- Work failing with `cannot resolve <hostname> (NXDOMAIN)`, for example email batches.
- `systemd-resolved` warnings about the DNS server, such as `Using degraded feature set`.
- Failures are instant (no timeout) and affect every call to the hostname.
- It began right after a network or resolver change, such as `Configuration updated: nameserver A -> B`.

## Likely causes

1. The nameserver was changed to one that doesn't know the name (an internal-only server asked for a public name, or the wrong server altogether).
2. The DNS record was deleted or renamed.
3. The DNS server is down or overloaded (this gives SERVFAIL or timeouts, not NXDOMAIN).
4. A wrong search domain turns the name into one that doesn't exist.
5. A typo in the hostname after a config change.

## Diagnose

```bash
resolvectl status                 # which nameserver is in use
cat /etc/resolv.conf
dig smtp.mail-provider.example                # using the current nameserver
dig smtp.mail-provider.example @10.0.0.53     # using the previous one
```

- If the previous nameserver answers and the current one doesn't, the nameserver change is the cause.
- NXDOMAIN means "this name doesn't exist here". SERVFAIL or a timeout means the DNS server itself is in trouble.
- Check whether one hostname fails or all of them.

## Fix now

1. Revert the nameserver change: put the previous nameserver back in the network config (netplan) and apply it.
2. Confirm `/etc/resolv.conf` and `resolvectl status` show the working nameserver, then flush the cache: `resolvectl flush-caches`.
3. If the record itself is missing, restore it at the DNS provider.
4. Check lookups succeed with `dig`, then make sure the queued work (email batches) is retried.

## Prevent

- Test a new nameserver against your critical hostnames before switching to it.
- Configure two nameservers, so one bad server doesn't fail every lookup.
- Monitor resolution of critical hostnames from inside the network; alert on NXDOMAIN and SERVFAIL rates.
- Manage resolver settings and DNS records as reviewed code.
- Make clients retry DNS failures with backoff and keep the work queued.

## References

- `resolvectl(1)`, `resolv.conf(5)`, `dig(1)`
- Runbook: `deploy-rollback`
