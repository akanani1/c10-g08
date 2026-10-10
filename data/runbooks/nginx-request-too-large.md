---
title: Nginx rejects uploads as too large (413)
id: nginx-request-too-large
categories: [config]
typical_severity: low
services: [nginx, edge, any nginx reverse proxy]
signals:
  - "client intended to send too large body: 13631488 bytes"
  - "client intended to send too large body"
  - "413 Request Entity Too Large"
---

# Nginx rejects uploads as too large (413)

## Symptoms

- nginx error log: `client intended to send too large body: <N> bytes`, with the client and the request (`POST /api/upload`).
- The client gets `413 Request Entity Too Large`.
- A steady, low rate from many different clients. Normal traffic is not affected.

## Likely causes

1. `client_max_body_size` is lower than the upload size the product allows. The nginx default is only 1 MB.
2. The limit is right, and some clients are trying to upload files that are too big.
3. The limit is set for one `location` and missing on another that also takes uploads.
4. A client bug that sends the wrong content or sends it twice.

## Diagnose

```bash
# The limit nginx is actually using
nginx -T 2>/dev/null | grep -n client_max_body_size
# Sizes being rejected, largest first
zcat error.log.gz | grep -o "too large body: [0-9]* bytes" | sort -k4 -n | tail
```

- Compare the rejected sizes with the documented upload limit. If the rejected files are within what users are told they can upload, the nginx limit is wrong.
- Check which endpoint is affected and whether the application behind it has its own, different limit.
- Check the rate. A jump after a release points at a client change.

## Fix now

Often no urgent action is needed. If legitimate uploads are being rejected:

1. Raise `client_max_body_size` for the upload location to match the product's limit:
   ```nginx
   location /api/upload {
       client_max_body_size 25m;
   }
   ```
2. Test and reload: `nginx -t && nginx -s reload`.
3. Make sure the application behind nginx accepts the same size.
4. Confirm the "too large body" lines stop for files within the limit.

If the rejected files are simply too big, leave the limit alone and show users the limit in the UI.

## Prevent

- Keep the upload limit in one place and use it for nginx, the application and the UI.
- Validate file size in the client before sending.
- Keep these lines out of paging alerts. Alert only on a sharp rise after a release.

## References

- nginx docs: `client_max_body_size`
- Runbook: `scanner-404-noise`
