# MySQL setup (Ubuntu)

The HTTP API stores every job — its configuration, rendition ladder, clip list,
status and timings — in MySQL. Transcoding itself does not need a database:
`python app.py --config ...` runs without one.

These steps assume Ubuntu 22.04 / 24.04 with MySQL 8, and the defaults in
`.env.example`:

| Setting | Value |
| --- | --- |
| Host | `localhost` |
| Port | `3306` |
| User | `root` |
| Database | `Visionular-Transcoder` |

---

## 1. Install and start MySQL

Skip this if MySQL is already running for your other projects.

```bash
sudo apt update
sudo apt install -y mysql-server
sudo systemctl enable --now mysql
sudo systemctl status mysql --no-pager      # should say "active (running)"
```

## 2. Make sure the user can log in with a password

**This is the step that most often goes wrong.** On Ubuntu, MySQL's `root` is
created with the `auth_socket` plugin: it logs in through the operating system
account and **refuses every password**, even the right one. An application such
as this API cannot use it.

Check which plugin your user has:

```bash
sudo mysql -e "SELECT user, host, plugin FROM mysql.user WHERE user='root';"
```

* `caching_sha2_password` or `mysql_native_password` — password login works.
  Nothing to do; go to step 3.
* `auth_socket` — switch it to a password:

  ```bash
  sudo mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED WITH caching_sha2_password BY 'your-password';"
  ```

  After this, `sudo mysql` without a password stops working for root; use
  `mysql -u root -p` instead.

Confirm a password login over TCP, which is how the API connects:

```bash
mysql -u root -p -h 127.0.0.1 -P 3306 -e "SELECT CURRENT_USER();"
```

## 3. Create the database (optional)

The API creates the database on first start if it is missing, so you can skip
this. To create it yourself:

```bash
mysql -u root -p -e "CREATE DATABASE IF NOT EXISTS \`Visionular-Transcoder\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
```

The backticks are required: the name contains a hyphen, which MySQL otherwise
reads as a minus sign. Use them in any SQL you write against this database:

```sql
USE `Visionular-Transcoder`;
```

The tables (`jobs`, `job_variants`, `job_clips`) are also created on first
start. `docs/schema.sql` holds the same DDL if you prefer to create them by
hand: `mysql -u root -p < docs/schema.sql`.

## 4. Give the API the credentials

```bash
cd /path/to/AI-Transcoder
cp .env.example .env
chmod 600 .env                 # it holds the password
nano .env                      # set DB_PASSWORD
```

Write the password exactly as it is — characters such as `@` need no quoting
or escaping in `.env`. `.env` is listed in `.gitignore`; never commit it.

For the systemd service, put the same lines in `/etc/ai-transcoder.env`
instead (see the README's *Deployment* section).

## 5. Install the driver and check the connection

```bash
. .venv/bin/activate
pip install -r requirements.txt          # installs PyMySQL
python app.py --check
```

The `MySQL` line should read `ok`. If it does not, the line says why and what
to change; the common cases are in *Troubleshooting* below.

## 6. Start the API and confirm jobs are stored

```bash
python -m api.app
curl -s localhost:8000/health        # "database": "connected"
```

Submit a job (see `docs/API.md`), then look at it in MySQL:

```bash
mysql -u root -p -e "SELECT job_id, channel, status, progress_pct, submitted_at FROM \`Visionular-Transcoder\`.jobs ORDER BY id DESC LIMIT 5;"
```

---

## Optional: a dedicated user instead of root

Using `root` works, but a user limited to this one database is safer: a bug or
a leaked `.env` then cannot touch your other projects' databases.

```bash
mysql -u root -p <<'SQL'
CREATE USER 'transcoder'@'localhost' IDENTIFIED BY 'choose-a-strong-password';
GRANT ALL PRIVILEGES ON `Visionular-Transcoder`.* TO 'transcoder'@'localhost';
SQL
```

Create the database first (step 3) — this user can use it but not create it.
Then set `DB_USER=transcoder` and its password in `.env`.

---

## Troubleshooting

`python app.py --check` recognises these and prints the fix.

| Error | Cause and fix |
| --- | --- |
| `(1698, "Access denied for user 'root'@'localhost'")` | The user is on `auth_socket`, which ignores passwords. See step 2. |
| `(1045, "Access denied for user ...")` | Wrong `DB_USER` or `DB_PASSWORD` in `.env`. |
| `(2003, "Can't connect to MySQL server ...")` | MySQL is not running, or not on `DB_HOST:DB_PORT`. `sudo systemctl status mysql`. |
| `(1044, "Access denied ... to database ...")` | The user may not create or use this database. Create it as root (step 3) and `GRANT` access. |
| `'cryptography' package is required for ... caching_sha2_password` | The server is not offering TLS, so the password must be RSA-encrypted instead. `pip install 'cryptography>=42'`, or re-enable TLS. MySQL 8 enables TLS at install, so this is unusual. |
| `DB_PASSWORD is not set` | No password reached the API. Set it in `.env`, or export it before starting. |
| `MySQL server has gone away` in long-running logs | Handled: idle connections are tested before use and recycled hourly. |
