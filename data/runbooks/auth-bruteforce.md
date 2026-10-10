---
title: Brute-force login attempts
id: auth-bruteforce
categories: [auth]
typical_severity: high
services: [auth-service, auth, any login endpoint]
signals:
  - "Login failed for user admin from 203.0.113.45: invalid password"
  - "Account locked after 5 failed attempts: user root"
  - "event: login_failed"
  - "event: account_locked"
---

# Brute-force login attempts

## Symptoms

- Many `Login failed for user <name> from <ip>: invalid password` lines in a short time.
- The same few source IPs are behind most of the failures.
- Well-known usernames are targeted: `admin`, `root`, `administrator`, `support`.
- `Account locked after 5 failed attempts`, and real users reporting lockouts.

## Likely causes

1. A brute-force or credential-stuffing attack from a few IPs (many usernames per IP).
2. A misconfigured client or script retrying an old password (one IP, one username, regular interval).
3. An expired or rotated service-account password.
4. An identity provider problem that makes valid logins fail (many users, many IPs, no pattern).

## Diagnose

```bash
# Failures per source IP and per user, from the JSON log
jq -r 'select(.event=="login_failed") | .source_ip' auth.json | sort | uniq -c | sort -rn | head
jq -r 'select(.event=="login_failed") | .user' auth.json | sort | uniq -c | sort -rn | head
```

- A few IPs across many usernames is an attack. One IP with one username is usually a broken client.
- Check whether any targeted account had a successful login from one of those IPs after the failures. If so, treat it as a compromise.
- Check whether the IPs are internal (a broken job) or external.

## Fix now

1. Block the attacking IPs at the WAF, load balancer or firewall.
2. Turn on or tighten the rate limit on the login endpoint, per IP and per username.
3. Keep the account lockout in place for targeted accounts. Unlock real users only after verifying them.
4. If an account shows a successful login from an attacking IP: force a password reset, revoke its sessions and tokens, and start the security incident process.
5. If it's a broken internal client, stop it and rotate the credential.

## Prevent

- Require multi-factor authentication for admin and service accounts.
- Add progressive delays or a CAPTCHA after repeated failures.
- Remove or rename default accounts such as `admin` and `root`.
- Alert on failed-login rate per IP and on lockout counts.
- Make sure logs never contain the password that was tried.

## References

- OWASP: Blocking Brute Force Attacks, Credential Stuffing Prevention Cheat Sheet
