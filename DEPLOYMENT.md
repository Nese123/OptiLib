# Public deployment

OptiLib's public service supports anonymous, temporary sessions and matrices up to
100,000 compounds × 1,000 targets. A matrix of this size contains 100 million
float64 cells (800 MB before working buffers). The configured four-hour runtime
is a deadline, not a guarantee of optimizer convergence.

## Architecture and requirements

Use Linux, Python 3.12, Docker Engine with Compose 2.24 or newer, 8 GB RAM and
4 CPUs. Provide **at least 32 GiB of runtime storage in addition to** the ChEMBL
and MolPort databases, application images and operational logs. An SSD with
100 GB or more is a practical starting point for the current databases.

The web service uses `webapp.wsgi:app`, one Gunicorn process and four threads,
limited to 768 MiB and one CPU. It stores summaries and artifact references.
The separate computation service has a 5 GiB memory limit and three CPUs. Its
supervisor spawns disposable processes, with one numerical-library thread each.
Large jobs reserve 4 GiB and run exclusively; small jobs reserve 2 GiB and at most
two run concurrently. Matrix builds conservatively reserve the large slot because
their final dimensions are not known at admission. There is no unbounded queue.

A shared runtime bind mount contains the SQLite job database and private session
artifacts. Source databases are mounted read-only. Both services run as UID/GID
10001, drop Linux capabilities, have read-only root filesystems, and use a bounded
temporary filesystem. The supervisor enforces deadlines and checks child RSS and
artifact usage; container limits isolate the web service if a child allocates
memory faster than a polling check can stop it.

The former in-process application remains for local numerical development and
regression tests. Do not deploy its development mode publicly. Even when using
`webapp.app:app`, production mode selects the new service.

## Configure and start

From the repository root:

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Set `SECRET_KEY` to the generated value, `OPTILIB_ENV=production`,
`SESSION_COOKIE_SECURE=true`, and `TRUSTED_HOSTS` to your public hostname (a
comma-separated list if needed, without URL schemes or ports). Production refuses
missing/placeholder/short secrets or insecure cookie configuration. The CSRF token
lifetime is five hours so a four-hour job can still be followed by a result action.

Place `chembl_37.db` and `molport.db` in `database/`. Ensure their contents and model
assets are readable by UID 10001. Complete any selectivity migration described
below before starting the public service. After offline maintenance, checkpoint
and close WAL databases so read-only containers do not require writable sidecars:

```bash
.venv/bin/python scripts/prepare_readonly_databases.py
```

This is an explicit maintenance command. Stop all database readers and writers
before running it. It checkpoints WAL and switches existing database files to
DELETE journaling; it does not change source records or rebuild scores.

```bash
sudo install -d -o 10001 -g 10001 -m 755 /var/lib/optilib/runtime
docker compose build
docker compose up -d computation optilib
docker compose ps
docker compose logs -f optilib computation
```

Only `127.0.0.1:5000` is published. The computation service has no published port.
Production traffic must go through Nginx. Completed exports use authenticated
`X-Accel-Redirect` delivery through its internal artifact location, freeing
Gunicorn threads immediately; the internal location is not publicly addressable. The container entrypoint uses
`requirements.lock`; update that lock only with regression and build validation.

For local testing of the same architecture, set `OPTILIB_ENV=development`,
`SESSION_COOKIE_SECURE=false`, and an absolute `OPTILIB_RUNTIME` directory in a
local `.env`. Start `.venv/bin/python -m webapp.public.worker` and
`.venv/bin/gunicorn --bind 127.0.0.1:5000 --workers 1 --threads 4 webapp.wsgi:app`
in separate terminals. Set `OPTILIB_ACCEL_REDIRECT=false` for direct local HTTP
without Nginx. Both load `.env`. Local source databases should still be
prepared for read-only use. Do not run multiple web masters or supervisors against
the same runtime volume.

## Nginx and TLS

Install host Nginx and Certbot, configure DNS, and obtain a certificate for the
public hostname before enabling the supplied HTTPS configuration. The template
contains top-level `events` and `http` blocks; merge it carefully if the host runs
other sites. Set its server names and certificate paths to match `TRUSTED_HOSTS`.
Its upstream is already `127.0.0.1:5000`. Its internal export alias must match
`RUNTIME_HOST_PATH` (default `/var/lib/optilib/runtime`). Nginx needs read/traverse
access to that directory; the application UID owns writes. Never change the
internal artifact location into a public alias.

```bash
sudo install -d -m 755 /var/www/optilib/static
sudo cp -a webapp/static/. /var/www/optilib/static/
sudo cp nginx/nginx.conf /etc/nginx/nginx.conf
sudo nginx -t
sudo systemctl reload nginx
```

Copy static assets on every release; HTML and private APIs are not cached. Nginx
replaces forwarded IP, host and scheme headers. Flask trusts only this single
proxy. Configure certificate renewal and test it with `certbot renew --dry-run`.
Do not expose port 5000 through another Docker mapping or host firewall rule.

## Resource policy and public interfaces

Defaults are configurable in `.env.example`:

| Resource | Default |
| --- | --- |
| Matrix | 100,000 compounds, 1,000 targets, 100 million cells |
| Upload measurements | 1 million affinity rows per session |
| Upload request | 16 MiB, 20 files; browser batches leave 1 MiB multipart headroom |
| Retained uploads | 100 files, 256 MiB of original uploaded content |
| XLSX expansion | 128 MiB per file; only CSV and XLSX are accepted |
| Runtime storage | 6 GiB/session, 24 GiB globally, including temporary artifacts |
| Free disk reserve | 4 GiB |
| Sessions | 100; two-hour inactivity expiry |
| Admission | One active job/session and IP; four matrix/optimization starts/IP/hour |
| Runtime | Four hours optimization; one hour other stages; 60-second stop grace |

Limits reject input without publishing partial changes. IP limits apply to users
sharing a NAT address. Resetting a session does not reset the hourly IP allowance.
An optimization deadline requests feasible partial results, then terminates the
process after the grace period. If no feasible result can be published in time,
the job ends with an error and previous results remain available.

Uploads, upload edits, matrix builds, optimizations, solution selection and export
preparation return `202` with `job_id` and `status_url`. Poll `/api/jobs/<id>` in the
same cookie session; completion includes `result`, and failures include `error`.
Existing pipeline and optimizer status routes remain available. Busy admission
returns `503`; IP limits return `429`, both with `Retry-After`. Invalid requests
return `400`, and request/storage quota rejections can return `413`. Validation
errors discovered asynchronously appear in job status.

`/api/heatmap-data` accepts `row_offset`, `column_offset`, `row_count` and
`column_count`. The default viewport is 20 × 40; each axis is capped at 100. It
returns total dimensions, window labels, global color bounds, full-selection
numeric distribution summaries, and a selection revision. Compound listings in
`/api/results` and `/api/uploads/affinity` or `/api/uploads/prices` use `offset`
and `limit` (default 100, maximum 500). The browser discards stale heatmap replies.

Downloads accept `format=xlsx` or `format=csv`. Preparation is asynchronous and
requires the session's `X-CSRFToken` header even on GET; fetching a completed
artifact is an ordinary download. Concurrent requests reuse one export job.
CSV is useful for large matrices: XLSX's temporary XML can consume substantial
storage and still remains subject to the session quota. CSV spreadsheet-control
strings are prefixed with an apostrophe; XLSX stores uploaded strings as literal
text. Neither export uses a full-matrix DataFrame.

Sessions intentionally do not survive a web-master restart. Their jobs are
cancelled and owned artifacts are cleaned up. Users must download results they
want to retain. Active jobs do not expire for inactivity. Unreferenced artifacts
are pruned after a two-minute reader grace period; operational files outside
owned session directories are never removed. Keep logs in Docker's logging driver
or a separate host log directory, not in the session volume. Configure host log
rotation and disk/CPU/memory monitoring.

## Readiness, maintenance and rollout checks

`/live` checks the web process; it is the web container's liveness check.
`/health` and `/api/health` return 200 only when required source schemas, scoring
provenance, runtime writes, supervisor heartbeat and startup model validation are
healthy. These checks are independent of normal job saturation. Database checks
are cached for 15 seconds. The supervisor healthcheck validates its heartbeat.
Docker does not automatically restart a merely unhealthy container: alert on
readiness failures and investigate logs.

```bash
curl -H 'Host: YOUR_PUBLIC_HOSTNAME' http://127.0.0.1:5000/live
curl -i https://YOUR_PUBLIC_HOSTNAME/health
.venv/bin/python -m unittest discover -s tests -v
node --test tests/test_frontend_admission.js tests/test_frontend_performance.js tests/test_frontend_public.js
```

The updater is a manual, disabled-by-default `maintenance` profile. Stop public
services, grant the maintenance UID write access to the database directory, run
updates, checkpoint databases, then restart. Store updater logs outside runtime.

```bash
docker compose stop optilib computation
docker compose --profile maintenance run --rm molport-updater
.venv/bin/python scripts/prepare_readonly_databases.py
docker compose up -d computation optilib
```

Back up source databases before maintenance. Session outputs are disposable and
should not be included in backups. Preserve a tested application image and its
matching database scoring version for rollback. The runtime schema is internal;
stop both services and replace the disposable runtime directory when rolling back
across incompatible versions. Do not mount runtime or exports under `/static/`.

Before public launch, validate the actual container build and TLS deployment,
verify that port 5000 is unreachable externally, and exercise uploads, polling,
selection and both download formats in a browser. During a large job, measure
status and viewport latency (95th percentile below two seconds). Test a second
user receiving a busy response and continuing to access existing results.

Run both density cases under the actual computation-container limits. These
commands require temporarily stopping the supervisor so the benchmark does not
compete with public jobs:

```bash
docker compose stop optilib computation
docker compose run --rm --no-deps --entrypoint python computation scripts/benchmark_public.py --directory /app/runtime --density .05
docker compose run --rm --no-deps --entrypoint python computation scripts/benchmark_public.py --directory /app/runtime --density 1
docker compose run --rm --no-deps --entrypoint python computation scripts/benchmark_public.py --directory /app/runtime --density .05 --soak-seconds 14400
```

The benchmark checks 100-million-cell storage, initialization and optimizer
execution, and records wall time and peak RSS. The soak mode repeats real searches
for at least four hours; it does not replace an API deadline/cancellation test.
Also run a four-hour API job on staging to verify deadline handling, and exercise
full-size ingestion, selection and exports. Do not claim those acceptance checks
passed merely because the numerical benchmark passed. Record observed results in
`PUBLIC_LAUNCH_VALIDATION.md`.

## ChEMBL selectivity maintenance

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
`docker compose run --rm --no-deps molport-updater python`. The database and log paths above are
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
