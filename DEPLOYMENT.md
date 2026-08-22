# OptiLib — Production Deployment Guide

This guide provides instructions for hosting the **OptiLib Drug Library Optimization** platform on a production server.

---

## 1. System Requirements

* **Operating System:** Linux (Ubuntu 22.04 / 24.04 LTS or Debian 12 recommended)
* **CPU:** Minimum 4 vCPUs (8+ vCPUs recommended for multi-user NSGA-II optimization)
* **RAM:** Minimum 8 GB (16 GB recommended)
* **Storage:** Minimum 60 GB SSD / NVMe (to host `chembl_36.db` ~30 GB, `molport.db` ~1.6 GB, plus temporary calculation caches and Docker images)
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
# /opt/optilib/database/chembl_36.db
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

# Optional: MolPort FTP credentials for automatic monthly database updates
# (Leave empty if not using automated FTP downloads)
MOLPORT_FTP_USER=your_molport_ftp_user
MOLPORT_FTP_PASS=your_molport_ftp_password
MOLPORT_FTP_HOST=ftp.molport.com
MOLPORT_FTP_PORT=21
```

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
sudo certbot certonly --nginx -d yourdomain.com
```

### Step C: Configure Nginx

Copy the production Nginx config template:

```bash
sudo cp nginx/nginx.conf /etc/nginx/sites-available/optilib.conf
```

Edit `/etc/nginx/sites-available/optilib.conf` to replace `yourdomain.com` with your actual domain and point upstream to `127.0.0.1:5000`:

```nginx
upstream optilib_app {
    server 127.0.0.1:5000;
}
```

Enable the site and restart Nginx:

```bash
sudo ln -s /etc/nginx/sites-available/optilib.conf /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

---

## 6. Health & Status Monitoring

* **Health Endpoint:** `GET /health` or `GET /api/health`
  ```bash
  curl -i https://yourdomain.com/health
  ```
  Returns `200 OK` JSON:
  ```json
  {
    "status": "healthy",
    "timestamp": 1771675200.0,
    "databases": {
      "chembl_36": true,
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
