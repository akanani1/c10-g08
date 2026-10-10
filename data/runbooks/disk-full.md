---
title: Disk full (no space left on device)
id: disk-full
categories: [disk]
typical_severity: critical
services: [api, postgres, any host that writes to local disk]
signals:
  - "No space left on device"
  - "Disk usage on /var/data at 90%"
  - "error writing /var/log/api/app.log.1: No space left on device"
  - "could not extend file: No space left on device"
  - "Failed to write /var/data/uploads/tmp-4572.part: No space left on device"
  - "Log cleanup job disabled"
---

# Disk full (no space left on device)

## Symptoms

- `No space left on device` from several programs on the same host: the application, `logrotate`, the database.
- Warnings such as `Disk usage on /var/data at 90%` climbing before the errors start.
- Uploads and other writes fail; the database reports `could not extend file`.
- Log rotation fails, so logs keep growing and the problem gets worse.

## Likely causes

1. A cleanup or log rotation job was disabled or is failing (look for `Log cleanup job disabled`).
2. Temp or upload files that are never removed.
3. Deleted files still held open by a running process, so the space isn't released.
4. Old backups, core dumps or container images left on the volume.
5. Out of inodes: many small files, even though bytes are free.

## Diagnose

```bash
df -h            # which filesystem is full
df -i            # inodes
du -xh /var/data --max-depth=2 | sort -h | tail -20
lsof +L1         # deleted files still held open
```

- Find what changed: a cron or config change shortly before usage started to climb.
- From the usage warnings, work out how fast the disk is filling and how long you have.

## Fix now

1. Clean up to free disk space, in this order: old rotated logs (`*.1`, `*.gz`), stale temp and partial upload files, old core dumps and backups.
2. Turn the cleanup job back on and run log rotation by hand: `logrotate -f /etc/logrotate.conf`.
3. If a deleted file is held open, restart the process that holds it.
4. Truncate a huge live log, don't delete it: `truncate -s 0 /var/log/api/app.log`.
5. If nothing can be removed, extend the volume and grow the filesystem.
6. Check writes work again, then restart anything that failed while the disk was full, starting with the database.

## Prevent

- Alert at 80% (warning) and 90% (page), on inodes too, and on fill rate ("full in under 4 hours").
- Size-based log rotation with compression and a retention limit. Alert if rotation fails.
- Treat disabling a cleanup job as a reviewed change with an expiry date.
- Keep data, logs and uploads on separate volumes so one can't starve the others.

## References

- `logrotate(8)`, `df(1)`, `du(1)`
- PostgreSQL docs: disk full failure
