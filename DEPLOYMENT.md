# Deploy OptiLib on a server

This guide assumes a dedicated Ubuntu 24.04 server with sudo access and a domain
name. Docker runs the web app and computation service; Nginx handles HTTPS.
Python 3.12 and the application dependencies are installed inside the images.

Replace `optilib.example.com` with your domain and `YOUR_REPOSITORY_URL` with your
OptiLib repository URL. Run commands on the server, from the repository directory
after step 2.

## 1. Prepare the server

- Use at least 4 CPUs and 8 GB RAM.
- Allow at least 32 GiB of runtime storage **in addition to** the databases,
  Docker images, and logs. A 100 GB SSD is a starting point; check database sizes
  and allow extra space for backups and database maintenance.
- Point your domain's DNS A record to the server's public IPv4 address. If you
  publish an AAAA record, IPv6 must also reach this server.
- Allow inbound TCP ports 80 and 443 in the server and hosting-provider firewalls.
  Keep your SSH port accessible. Do not expose port 5000 publicly.

Install the host tools:

```bash
sudo apt update
sudo apt install -y git curl nginx certbot openssl nano
```

Install Docker Engine and its Compose plugin using the
[official Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/#install-using-the-apt-repository).
Then check the installation:

```bash
sudo systemctl enable --now docker nginx
sudo docker compose version
sudo docker run --rm hello-world
```

Use Compose 2.24 or newer. The commands below use `sudo docker`, so Docker group
membership is not required.

## 2. Copy the application and databases

```bash
git clone YOUR_REPOSITORY_URL OptiLib
cd OptiLib
mkdir -p database
```

Copy your prepared databases to these paths on the server:

```text
OptiLib/database/chembl_37.db
OptiLib/database/molport.db
```

These databases are not included in Git. Use consistent copies made while the
source databases are closed, or use SQLite backups. The ChEMBL database must
include OptiLib's selectivity table and scoring metadata; see the maintenance
section below if it needs rebuilding.

Check that the price prediction model is also present before building:

```bash
ls -lh database/chembl_37.db database/molport.db
ls -lh MolPrice/models/Numpy/MP_Morgan_hybrid.pkl
```

## 3. Configure the application

```bash
cp .env.example .env
chmod 600 .env
openssl rand -hex 32
nano .env
```

Paste the generated value into `SECRET_KEY` and set:

```dotenv
SECRET_KEY=PASTE_THE_GENERATED_VALUE_HERE
OPTILIB_ENV=production
SESSION_COOKIE_SECURE=true
TRUSTED_HOSTS=optilib.example.com
RUNTIME_HOST_PATH=/var/lib/optilib/runtime
OPTILIB_ACCEL_REDIRECT=true
```

Use only a hostname for `TRUSTED_HOSTS`, without `https://`, a port, or a path.
Leave the remaining limits at their defaults initially. MolPort FTP credentials
are needed only if you run the optional database updater. Keep `.env` private.

## 4. Build the images and prepare storage

```bash
sudo install -d -o 10001 -g 10001 -m 755 /var/lib/optilib/runtime
sudo chown -R 10001:10001 database
sudo chmod 755 database
sudo chmod 644 database/chembl_37.db database/molport.db
sudo docker compose build
```

The containers run as UID/GID 10001. They need read access to the source databases
and write access to runtime storage. The maintenance container also needs write
access to the database directory.

If ChEMBL needs a selectivity rebuild, run the maintenance command below now.
Then prepare the databases for read-only mounts:

```bash
sudo docker compose --profile maintenance run --rm --no-deps molport-updater \
  python scripts/prepare_readonly_databases.py
```

This checkpoints SQLite WAL files and switches to DELETE journaling. Run it only
while all database readers, writers, and maintenance jobs are stopped. It does
not rebuild selectivity scores.

## 5. Start the application

```bash
sudo docker compose up -d computation optilib
sudo docker compose ps
curl -i -H 'Host: optilib.example.com' http://127.0.0.1:5000/health
```

Allow a minute for startup. The health request should return `200 OK` with all
checks set to `true`. If it returns `503`, inspect the failed checks and logs:

```bash
sudo docker compose logs --tail=100 optilib computation
```

The app listens on `127.0.0.1:5000`. Complete HTTPS setup before using it in a
browser because production session cookies require HTTPS.

## 6. Obtain an HTTPS certificate

First, create a temporary HTTP site so Certbot can verify your domain:

```bash
sudo install -d -m 755 /var/www/certbot
sudo tee /etc/nginx/sites-available/optilib-acme > /dev/null <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name optilib.example.com;
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
    location / {
        return 404;
    }
}
EOF
sudo ln -s /etc/nginx/sites-available/optilib-acme /etc/nginx/sites-enabled/optilib-acme
sudo nginx -t
sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/certbot -d optilib.example.com
```

Follow Certbot's prompts. DNS must resolve to this server and port 80 must be
reachable. This uses Certbot's
[webroot verification](https://eff-certbot.readthedocs.io/en/stable/using.html#webroot),
which also supports renewal while Nginx stays running.

## 7. Enable the production Nginx configuration

The supplied file replaces the entire Nginx configuration. These commands assume
this server hosts only OptiLib. If it hosts other sites, merge the configuration
with the existing setup instead.

```bash
sudo install -d -m 755 /var/www/optilib/static
sudo cp -a webapp/static/. /var/www/optilib/static/
sudo cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.before-optilib
sudo cp nginx/nginx.conf /etc/nginx/nginx.conf
sudo nano /etc/nginx/nginx.conf
```

In the copied configuration:

- Replace every `optilib.example.com` with your domain, including both
  certificate paths. Use the paths Certbot printed if they differ.
- If you published an AAAA record, add IPv6 listeners alongside the HTTP and
  HTTPS listeners (`listen [::]:80;` and `listen [::]:443 ssl http2;`). Add
  `default_server` to the IPv6 listener in the default HTTP server block.
- If you changed `RUNTIME_HOST_PATH`, update the internal artifact alias to
  `<RUNTIME_HOST_PATH>/sessions/`. Nginx needs read and directory traversal access.
  Keep the `internal;` directive: exports require session authorization.

Test and load it:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Only reload after the configuration test succeeds. Set up automatic certificate
renewal and reload Nginx after successful renewals:

```bash
sudo install -d /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx > /dev/null <<'EOF'
#!/bin/sh
nginx -t && systemctl reload nginx
EOF
sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx
sudo systemctl enable --now certbot.timer
sudo certbot renew --dry-run
```

## 8. Check the public website

```bash
curl -I http://optilib.example.com
curl -i https://optilib.example.com/health
```

HTTP should redirect to HTTPS, and `/health` should return `200 OK`. Open
`https://optilib.example.com` in a browser and test an upload, matrix creation,
optimization, solution selection, and both CSV and XLSX downloads. Check from
another machine that port 5000 is inaccessible.

Monitor `/health`, Docker logs, and host disk/memory usage. Docker restarts exited
services, but an unhealthy status alone does not trigger a restart. Test your
expected job sizes on the server before opening it to public use.

## Updating the application

Tell users before restarting: sessions and their jobs do not survive a web
restart, so users should download results first. Keep a tested previous image and
matching database backup for rollback.

From the repository directory, deploy a tested release:

```bash
git pull --ff-only
sudo docker compose build
sudo docker compose stop optilib computation
sudo cp -a webapp/static/. /var/www/optilib/static/
sudo docker compose up -d computation optilib
curl -i https://optilib.example.com/health
```

Review release-specific database or configuration changes before restarting.
Keep one web process and one computation supervisor per runtime directory.

## Database maintenance (only when needed)

Back up the source databases and stop all readers and writers before maintenance:

```bash
sudo docker compose stop optilib computation
```

For a MolPort update, set the FTP credentials in `.env`, then run:

```bash
sudo docker compose --profile maintenance run --rm --no-deps molport-updater
```

For a legacy ChEMBL selectivity table, rebuild it using the current scoring
version, `blended_boundary_average_v2`. Missing scoring metadata also requires a
rebuild; do not fix it by relabeling old scores. The following row count applies
to the existing prepared ChEMBL 37 dataset, not arbitrary ChEMBL releases:

```bash
sudo docker compose --profile maintenance run --rm --no-deps molport-updater \
  python -u scripts/build_selectivity_table.py \
  --db-path database/chembl_37.db \
  --expected-row-count 2251098 \
  --batch-size 25000
```

Allow additional disk space for staging, indexes, and the retained previous table.
The builder validates row counts and compound-target keys before replacing the
active table. It prints the retained staging file and rollback table names; keep
them until the new build is accepted. A different source dataset needs separate
preparation and validation.

After successful maintenance, prepare the databases again and restart:

```bash
sudo docker compose --profile maintenance run --rm --no-deps molport-updater \
  python scripts/prepare_readonly_databases.py
sudo docker compose up -d computation optilib
curl -i https://optilib.example.com/health
```

Back up source databases and private configuration regularly. Session outputs in
`/var/lib/optilib/runtime` are temporary and do not need backups. Keep maintenance
logs outside that directory and configure host log rotation.
