---
title: 404s from scanners probing for common paths
id: scanner-404-noise
categories: [other]
typical_severity: low
services: [nginx, edge, any public web server]
signals:
  - "open() \"/usr/share/nginx/html/admin.php\" failed (2: No such file or directory)"
  - "open() \"/usr/share/nginx/html/.env\" failed (2: No such file or directory)"
  - "GET /xmlrpc.php HTTP/1.1"
  - "GET /phpmyadmin/index.php HTTP/1.1"
  - "No such file or directory"
---

# 404s from scanners probing for common paths

## Symptoms

- nginx error log: `open() "/usr/share/nginx/html/<path>" failed (2: No such file or directory)`.
- The paths are ones your site never had: `admin.php`, `.env`, `xmlrpc.php`, `phpmyadmin/index.php`, `wp-login.php`.
- Many different client IPs, a steady low rate around the clock.
- Normal traffic is not affected.

## Likely causes

1. Automated scanners looking for known vulnerable software and leaked config files. Every public server sees this.
2. A broken link or a missing static file after a deploy. In that case the paths belong to your own application.
3. A targeted probe: one client walking through many paths quickly.

## Diagnose

```bash
# Which paths, and how often
zcat error.log.gz | grep -o 'html/[^"]*' | sort | uniq -c | sort -rn | head -20
# Which clients
zcat error.log.gz | grep -o 'client: [0-9.]*' | sort | uniq -c | sort -rn | head
```

- Are the paths ones your application serves? If not, it's scanner noise. If so, it's a real missing file.
- Confirm nothing sensitive is reachable: request `/.env` and similar paths yourself and expect a 404.
- Check the rate per client. A few clients making most of the requests are worth blocking.

## Fix now

Usually nothing is broken: nginx is correctly refusing these requests.

1. If one scanner is noisy, block its address at the edge firewall or with a `deny` rule.
2. Add a rate limit (`limit_req`) on the public server so a scanner can't flood it.
3. If the 404s are for your own paths, redeploy the static assets or fix the link.
4. If any probed path returns something other than a 404, treat it as a security issue and remove the file.

## Prevent

- Return 444 (close the connection) for known scanner paths and turn off their logging, to cut the noise:
  ```nginx
  location ~* (\.env|\.php|phpmyadmin) { access_log off; log_not_found off; return 444; }
  ```
- Keep these lines out of paging alerts. Alert on 404s for real application paths only.
- Never deploy `.env` files, backups or admin tools under the web root.
- Put a WAF or bot filter in front of public endpoints.

## References

- nginx docs: `log_not_found`, `limit_req`, `deny`
- Runbook: `nginx-request-too-large`
