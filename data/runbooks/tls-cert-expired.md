---
title: TLS certificate expired
id: tls-cert-expired
categories: [tls]
typical_severity: high
services: [edge-gateway, cert-manager, any TLS endpoint]
signals:
  - "TLS handshake failed: certificate has expired"
  - "Certificate for api.shop.example expires in 30 minutes"
  - "Renewal failed for api.shop.example certificate: ACME challenge timed out"
  - "ACME_TIMEOUT"
---

# TLS certificate expired

## Symptoms

- Warnings counting down: `Certificate for <host> expires in N minutes`.
- `Renewal failed for <host> certificate: ACME challenge timed out` from cert-manager, before the expiry.
- After the expiry time: `TLS handshake failed: certificate has expired` for every client.
- Failures start at an exact time and affect all traffic to one hostname.

## Likely causes

1. Automatic renewal failed (ACME challenge timed out, DNS or HTTP challenge blocked, rate limit) and nobody acted on the warnings.
2. The certificate was renewed but the gateway was never reloaded, so it still serves the old one.
3. An intermediate certificate in the chain expired.
4. A wrong clock on the server or client.

## Diagnose

```bash
# What the server is serving right now
echo | openssl s_client -connect api.shop.example:443 -servername api.shop.example 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates
# Renewal state in cert-manager
kubectl describe certificate api-shop-example
kubectl get challenges,orders -A
```

- If the certificate on disk or in the secret is newer than the one being served, a reload is enough.
- Read the renewal error. A challenge timeout usually means the ACME server couldn't reach the challenge URL or DNS record.
- Check the time on the host: `timedatectl`.

## Fix now

1. Renew the certificate now. With cert-manager, delete the failed order so it retries: `kubectl delete order <name>`, or run `cmctl renew api-shop-example`.
2. If the ACME challenge keeps failing, fix what blocks it (firewall rule, ingress path, DNS record). If that takes time, issue a certificate by hand from your CA and install it.
3. Reload the gateway so it picks up the new certificate.
4. Verify with the `openssl s_client` command and confirm the handshake errors have stopped.
5. Do not turn off certificate verification in clients as a workaround.

## Prevent

- Alert at 30, 14 and 7 days before expiry, checked from outside the server.
- Alert on every failed renewal, not only on expiry.
- Renew at two thirds of the certificate lifetime and reload the gateway automatically afterwards.
- Keep a list of every certificate with its owner and expiry date.

## References

- cert-manager docs: troubleshooting ACME challenges
- `openssl s_client(1)`
