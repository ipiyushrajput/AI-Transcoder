# PostgreSQL setup on Ubuntu

End-to-end steps to get the AI-Transcoder database running on an Ubuntu server
(20.04 / 22.04 / 24.04). Commands assume a sudo-capable user.

Nothing here needs to be run twice — the API creates its own tables on first
start, and will create the database too if the role has `CREATEDB`.

---

## 1. Install the server

```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib
```

Check the version and that the service came up:

```bash
psql --version
sudo systemctl status postgresql --no-pager
```

If it is not running:

```bash
sudo systemctl enable --now postgresql
```

---

## 2. Create the role and database

```bash
sudo -u postgres psql
```

At the `postgres=#` prompt — **change the password**:

```sql
CREATE ROLE transcoder WITH LOGIN PASSWORD 'ChangeMe_StrongPassword';
ALTER  ROLE transcoder CREATEDB;
CREATE DATABASE ai_transcoder OWNER transcoder;
GRANT ALL PRIVILEGES ON DATABASE ai_transcoder TO transcoder;
\q
```

On PostgreSQL 15 and newer the `public` schema is locked down by default. Since
`transcoder` owns the database this is already handled, but if you created the
database under a different owner, also run:

```bash
sudo -u postgres psql -d ai_transcoder -c "GRANT ALL ON SCHEMA public TO transcoder;"
```

Verify the login works over TCP (this is how the app connects):

```bash
PGPASSWORD='ChangeMe_StrongPassword' psql -h 127.0.0.1 -U transcoder -d ai_transcoder -c '\conninfo'
```

---

## 3. Authentication

The default `scram-sha-256` local rules are fine when the API runs on the same
host as PostgreSQL. Confirm the host rules exist:

```bash
sudo grep -vE '^\s*#|^\s*$' /etc/postgresql/*/main/pg_hba.conf
```

You want lines like:

```
local   all   all                 peer
host    all   all   127.0.0.1/32  scram-sha-256
host    all   all   ::1/128       scram-sha-256
```

After any edit:

```bash
sudo systemctl reload postgresql
```

### If the API runs on a different host

Only then open the listener — otherwise leave it on localhost.

```bash
sudo nano /etc/postgresql/*/main/postgresql.conf
#   listen_addresses = 'localhost,10.0.0.5'      # this server's private IP

sudo nano /etc/postgresql/*/main/pg_hba.conf
#   host  ai_transcoder  transcoder  10.0.0.0/24  scram-sha-256

sudo systemctl restart postgresql
sudo ufw allow from 10.0.0.0/24 to any port 5432 proto tcp
```

Never expose 5432 to `0.0.0.0/0`.

---

## 4. Point the application at it

The API reads its connection settings from the environment. Create
`/etc/ai-transcoder.env` (root-owned, mode 600 — it holds a password):

```bash
sudo tee /etc/ai-transcoder.env >/dev/null <<'EOF'
DB_HOST=127.0.0.1
DB_PORT=5432
DB_USER=transcoder
DB_PASSWORD=ChangeMe_StrongPassword
DB_NAME=ai_transcoder

# Or, instead of the five above, one full URL:
# DATABASE_URL=postgresql+psycopg2://transcoder:ChangeMe_StrongPassword@127.0.0.1:5432/ai_transcoder

TRANSCODER_CONFIG=/opt/ai-transcoder/config.json
LOG_ROOT=/var/log/ai-transcoder
WORK_ROOT=/var/tmp/ai-transcoder
MAX_CONCURRENT_JOBS=2
PORT=8000
EOF

sudo chmod 600 /etc/ai-transcoder.env
```

A password with `@`, `:` or `/` in it is URL-encoded automatically when the
individual `DB_*` variables are used. If you set `DATABASE_URL` by hand, encode
those characters yourself (`@` → `%40`).

---

## 5. Create the tables

Two options — either is fine.

**Automatic (recommended).** Just start the API; it runs `create_all()` on boot:

```bash
cd /opt/ai-transcoder
set -a && . /etc/ai-transcoder.env && set +a
python3 -m api.app
```

Look for `Database ready: postgresql+psycopg2://transcoder:***@...` in the log.

**Explicit.** Apply the checked-in schema:

```bash
PGPASSWORD='ChangeMe_StrongPassword' \
  psql -h 127.0.0.1 -U transcoder -d ai_transcoder -f docs/schema.sql
```

Confirm:

```bash
PGPASSWORD='ChangeMe_StrongPassword' \
  psql -h 127.0.0.1 -U transcoder -d ai_transcoder -c '\dt'
```

```
 Schema |     Name     | Type  |   Owner
--------+--------------+-------+------------
 public | job_clips    | table | transcoder
 public | job_variants | table | transcoder
 public | jobs         | table | transcoder
```

---

## 6. Run the API as a service

```bash
sudo cp deploy/ai-transcoder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-transcoder
sudo systemctl status ai-transcoder --no-pager
curl -s localhost:8000/health | python3 -m json.tool
```

`"database": "connected"` in that response means everything is wired up.

---

## 7. Useful queries

```sql
-- what is running right now
SELECT job_id, channel, status, stage, progress_pct
FROM jobs WHERE status = 'RUNNING' ORDER BY started_at DESC;

-- recent failures with the stage that broke
SELECT job_id, channel, error_stage, left(error_message, 120) AS error, completed_at
FROM jobs WHERE status = 'FAILED' ORDER BY completed_at DESC LIMIT 20;

-- throughput over the last day
SELECT status, count(*), round(avg(duration_seconds)::numeric, 1) AS avg_seconds
FROM jobs WHERE submitted_at > now() - interval '1 day' GROUP BY status;

-- the exact config a job ran with
SELECT config_snapshot FROM jobs WHERE job_id = '<uuid>';

-- the ladder a job used
SELECT name, width, height, codec, bitrate, crf, preset
FROM job_variants WHERE job_id = '<uuid>' ORDER BY variant_order;
```

---

## 8. Backup and retention

Nightly dump:

```bash
sudo -u postgres sh -c 'pg_dump -Fc ai_transcoder > /var/backups/ai_transcoder_$(date +%F).dump'
```

As a cron entry (02:30 daily, keeping 14 days):

```bash
sudo crontab -e
30 2 * * * sudo -u postgres pg_dump -Fc ai_transcoder > /var/backups/ai_transcoder_$(date +\%F).dump && find /var/backups -name 'ai_transcoder_*.dump' -mtime +14 -delete
```

Restore:

```bash
sudo -u postgres pg_restore -d ai_transcoder --clean /var/backups/ai_transcoder_2026-01-31.dump
```

Prune old job rows (log files on disk are not touched):

```sql
DELETE FROM jobs
WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED')
  AND completed_at < now() - interval '90 days';
```

`job_variants` and `job_clips` cascade automatically.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `connection to server at "127.0.0.1", port 5432 failed: Connection refused` | PostgreSQL is not running: `sudo systemctl start postgresql` |
| `FATAL: password authentication failed for user "transcoder"` | Password mismatch — `ALTER ROLE transcoder WITH PASSWORD '...'`, then update `/etc/ai-transcoder.env` |
| `FATAL: database "ai_transcoder" does not exist` | Create it (step 2), or give the role `CREATEDB` and let the API create it |
| `permission denied for schema public` | `GRANT ALL ON SCHEMA public TO transcoder;` on PG 15+ |
| `no pg_hba.conf entry for host ...` | Add a `host` line for the client's subnet, then `sudo systemctl reload postgresql` |
| `ModuleNotFoundError: No module named 'psycopg2'` | `pip install -r requirements.txt` |
| API logs `Running without persistence` | The DB is unreachable; jobs still transcode, but history and listings are unavailable. Check `/health` and the API log. |

Logs to check: `sudo journalctl -u postgresql -n 50` and
`sudo tail -f /var/log/postgresql/postgresql-*-main.log`.
