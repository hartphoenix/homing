# Backup and restore runbook

Backups contain user profiles, prompts, comments, project membership, leads,
tokens (only token digests), and audit data. Treat the encrypted artifact as
confidential even though password/token secrets are not recoverable from it.

## Nightly encrypted backup

Install `age` and `rclone` on the host. Keep the age identity file off-host and
offline where practical; only its public recipient belongs in `.env`. Configure
the rclone remote with least-privilege write access and retention/lifecycle
policy. Then schedule, for example, a systemd timer or cron entry as the deploy
user:

```cron
17 2 * * * cd /srv/sublet-tracker && ./docker/backup.sh >>/var/log/sublet-tracker-backup.log 2>&1
```

The script uses `pg_dump --format=custom --no-owner --no-privileges`, encrypts
the stream with `age`, uploads only the `.dump.age` artifact, and removes local
artifacts older than `BACKUP_RETENTION_DAYS` (35 by default). Do not put the age
identity, rclone config, or unencrypted dump under the repository. The cron
account must not log the contents of `.env`.

The remote policy should retain at least 35 daily copies plus four weekly and
12 monthly copies. Alert if no new remote artifact appears within 26 hours, if
the encrypted artifact is unexpectedly small, or if upload exits non-zero.

## Restore procedure

Restores are destructive and require an explicit operator decision. First
identify the exact artifact and verify its checksum/remote object metadata. Put
the application in a maintenance window; users and agents must not write while
the database is replaced.

```sh
cd /srv/sublet-tracker
export AGE_IDENTITY_FILE=/secure/off-host/sublet-tracker.agekey
export RESTORE_CONFIRM=YES
./docker/restore.sh /secure/backup/sublet-tracker-20260815T021700Z.dump.age
./docker/smoke.sh https://sublets.example.com
```

`restore.sh` stops web/Caddy, decrypts without writing a plaintext dump, uses
`pg_restore --clean --if-exists`, reapplies migrations, and starts the services.
If restore fails, leave web stopped, inspect the database logs, and do not run
an unreviewed destructive SQL command. Keep the previous encrypted backup
available until smoke checks and user verification succeed.

## Restore drill and evidence

At least quarterly, restore the newest backup into a separate temporary Compose
project/VM with an isolated domain and a copy of `.env` containing new secrets.
Verify:

1. decryption succeeds with the recovery identity;
2. project/user counts and a sample of leads, comments, interest, trash, and
   audit records match expected counts;
3. login, invitation authorization, token revocation, API reads/writes, and
   `/health/ready` work;
4. no source database or backup secret appears in logs; and
5. the recovered instance can be stopped and its temporary volume destroyed.

Record artifact ID, restore start/end, result, checks performed, operator, and
any remediation. Never paste credentials or the decrypted database into the
drill report. A failed drill is an incident: preserve the failed copy for
diagnosis, fix the issue, and repeat before declaring recovery ready.

