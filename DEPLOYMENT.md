# OptiLib — Production Deployment Guide

This guide provides instructions for hosting the **OptiLib Drug Library Optimization** platform on a production server.

---

## 1. System Requirements

* **Operating System:** Linux (Ubuntu 22.04 / 24.04 LTS or Debian 12 recommended)
* **CPU:** Minimum 4 vCPUs (8+ vCPUs recommended for multi-user NSGA-II optimization)
* **RAM:** Minimum 8 GB (16 GB recommended)
* **Storage:** Minimum 60 GB SSD / NVMe (to host `chembl_37.db` ~30 GB, `molport.db` ~1.6 GB, plus temporary calculation caches and Docker images)
* **Software:** Docker Engine (24.0+) and Docker Compose (2.20+)

---

## 2. Directory Structure Setup

Clone the repository onto the server:

```bash
git clone https://github.com/your-org/OptiDrug.git /opt/optilib
cd /opt/optilib
```

Ensure the `database/` directory contains the SQLite databases:

```bash
mkdir -p database webapp/output
# Place your databases here:
# /opt/optilib/database/chembl_37.db
# /opt/optilib/database/molport.db
```

---

## 3. Environment Configuration

Copy the example environment file:

```bash
cp .env.example .env
```

Generate a secure random secret key:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Edit `.env` and set:

```env
# Required: Secure session secret
SECRET_KEY=your_generated_64_character_hex_key_here

# Enable secure cookies when running behind HTTPS
SESSION_COOKIE_SECURE=true

# Logging level
LOG_LEVEL=INFO

# Default rate limiting
RATE_LIMIT_DEFAULT=120 per minute

# Maximum simultaneous pipelines and optimizations across all sessions
MAX_CONCURRENT_JOBS=2

# Optional: MolPort FTP credentials for automatic monthly database updates
# (Leave empty if not using automated FTP downloads)
MOLPORT_FTP_USER=your_molport_ftp_user
MOLPORT_FTP_PASS=your_molport_ftp_password
MOLPORT_FTP_HOST=ftp.molport.com
MOLPORT_FTP_PORT=21
```

`MAX_CONCURRENT_JOBS` defaults to `2`. When both computation slots are occupied,
new pipeline or optimization requests receive HTTP `503` with a busy message;
their existing results remain available. Increase this limit only after checking
the memory and CPU available for concurrent jobs. Gunicorn must still use one
worker because sessions and job admission are held in process memory.

---

## 4. Deploying with Docker Compose (Option A: Web App + Auto Updater)

Run the full stack with Docker Compose:

```bash
# Build the container images
docker compose build

# Start services in the background
docker compose up -d
```

### What runs:
1. **`optilib` service:** The main web platform on port `5000` running Gunicorn with 4 threads and SQLite WAL mode enabled.
2. **`optilib_molport_updater` service:** An automated sidecar that wakes up on the **1st of every month at 03:00 AM UTC** to download and apply the latest compound additions and removals from MolPort's FTP server.

Check running containers:

```bash
docker compose ps
```

Check logs:

```bash
# Web application logs
docker compose logs -f optilib

# MolPort automated updater logs
docker compose logs -f molport-updater
```

---

## 5. Reverse Proxy & SSL Setup (Nginx + Let's Encrypt)

### Step A: Install Nginx and Certbot on the Host

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

### Step B: Obtain SSL Certificates

```bash
sudo certbot certonly --nginx -d optilib.aittokallio.group
```

### Step C: Configure Nginx

Copy the production Nginx config template:

```bash
# This template includes the top-level events/http blocks.
sudo cp nginx/nginx.conf /etc/nginx/nginx.conf
sudo install -d -m 755 /var/www/optilib/static
sudo cp -a webapp/static/. /var/www/optilib/static/
```

Edit `/etc/nginx/nginx.conf` to replace `optilib.aittokallio.group` with your actual domain and point upstream to `127.0.0.1:5000`:

```nginx
upstream optilib_app {
    server 127.0.0.1:5000;
}
```

Validate the configuration and reload Nginx:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

---

## 6. Health & Status Monitoring

* **Health Endpoint:** `GET /health` or `GET /api/health`
  ```bash
  curl -i https://optilib.aittokallio.group/health
  ```
  Returns `200 OK` JSON:
  ```json
  {
    "status": "healthy",
    "timestamp": 1771675200.0,
    "databases": {
      "chembl": true,
      "chembl_37": true,
      "molport": true
    }
  }
  ```

---

## 7. Manual Database Updates (Admin Ad-Hoc)

If you need to trigger a manual MolPort update at any time:

```bash
# Inside the container:
docker compose exec optilib python scripts/update_molport_db.py

# Or on the host (if running in virtual environment):
MOLPORT_FTP_USER="user" MOLPORT_FTP_PASS="pass" python scripts/update_molport_db.py
```

## 8. Versioned ChEMBL Selectivity Maintenance

The application requires the active ChEMBL selectivity table to use
`blended_boundary_average_v2`. This version averages equally distant neighbors
at the local-potency cutoff. The builder records the scoring version and a unique
build ID in `optilib_selectivity_metadata`; both identify cached matrices. A table
without this metadata is treated as legacy and must be rebuilt before the current
application can run a ChEMBL pipeline. Do not label old scores as the new version
by editing metadata alone.

Rebuilds are explicit maintenance commands; the web server never runs a migration
at startup. Run one maintenance job at a time. The current ChEMBL 37 installation
has **2,251,098 compound-target rows**, so its rebuild command is:

```bash
.venv/bin/python -u scripts/build_selectivity_table.py \
  --db-path database/chembl_37.db \
  --expected-row-count 2251098 \
  --batch-size 25000 \
  > database/selectivity-rebuild.log 2>&1
```

For a container installation, replace `.venv/bin/python` with
`docker compose exec -T optilib python`. The database and log paths above are
relative to the repository root. Monitor progress with:

```bash
tail -f database/selectivity-rebuild.log
```

The builder creates a separate staging SQLite database under `database/`, streams
activities in bounded chunks, and calculates exact medians on disk. It adds
indexes for target/assay and active-activity lookups; source activity, assay, and
target rows remain unchanged. Leave space for the staging file, replacement
selectivity table, indexes, and retained previous table. The log records the
staging path, row counts, query plans, score changes, peak memory, and duration.

The existing selectivity table remains active during the build. Before publication,
the builder checks the row count and compares every compound-target key in both
directions. A failure leaves the active table and its provenance intact. Successful
publication swaps the table and metadata in one transaction and retains the old
table under the rollback name printed in the log. The count/key checks target a
scoring migration of the same source dataset; a changed ChEMBL release requires
its own validated database preparation.

The successful staging file is retained for verification and can be removed after
the new build is accepted. Keep the retained rollback table until that recovery
option is no longer needed. Both staging files and maintenance logs belong in the
ignored `database/` directory.

Restore the most recently retained table and its matching provenance with:

```bash
.venv/bin/python scripts/build_selectivity_table.py \
  --db-path database/chembl_37.db --rollback
```

To select a particular retained table, add its name from the migration log. For
example, the 2026-09-09 local migration retained:

```bash
.venv/bin/python scripts/build_selectivity_table.py \
  --db-path database/chembl_37.db --rollback \
  --backup-table compound_target_selectivity_backup_0bc527c792ce
```

Rollback also retains the replaced build. Restoring a legacy scoring table restores
legacy provenance: deploy its compatible application version, or rebuild current
scores before starting new ChEMBL pipelines with the current application.

### Performance and static assets

Nginx serves `/static/` from `/var/www/optilib/static/` with a one-hour browser cache;
copy the public `webapp/static/` contents there on each deployment. Do not copy
session exports into that directory. For containerized Nginx, mount the static
directory read-only at the same path. JSON API responses use normal proxy buffering.
The optimizer page defers the pinned Plotly Cartesian bundle and app script in order.

Fitness evaluation prepares an additional score matrix only when it fits within
128 MiB per job (`DrugLibraryProblem(prepared_score_budget=...)` can override this
byte budget; zero disables it). With the default two admitted jobs this adds at
most 256 MiB, plus bounded preprocessing temporaries. Larger matrices use the
same objective calculation without the additional matrix. The prepared buffer
is released when optimization and result preparation finish.

Pareto selection retains only selected rows in memory and creates Excel on the
first download of that selection. Concurrent downloads share its atomic export.
Uploads are sent in batches of at most 20 files and 15 MiB. Progress requests use
`since_generation` and `run_revision`; clients omitting these still receive full
history. No database migration is needed for these medium-priority changes.

### Optimizer initialization reproducibility

Initial random population rows now use NumPy's direct Boolean sampling, and the
union seed is the Boolean OR of the cheapest and highest-selectivity seeds.
The same seed and inputs remain repeatable with the same NumPy version, but
random rows (and potentially the final Pareto front) differ from releases that
generated integer rows before casting them to Boolean. The five smart-seed
definitions, objective functions, mutation, and termination rules are unchanged.
