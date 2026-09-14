"""Validated deployment and resource policy; no scientific imports."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MiB = 1024 ** 2
GiB = 1024 ** 3

# Docker injects configuration; local WSGI and supervisor commands also read .env.
from dotenv import load_dotenv
load_dotenv(ROOT / '.env')


def positive(name, default):
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f'{name} must be positive')
    return value


class Policy:
    def __init__(self, runtime=None):
        self.root = Path(runtime or os.environ.get('OPTILIB_RUNTIME', ROOT / 'runtime')).resolve()
        self.database = Path(os.environ.get('OPTILIB_DATABASE', ROOT / 'database')).resolve()
        self.chembl = self.database / os.environ.get('CHEMBL_DB_NAME', 'chembl_37.db')
        self.compounds = positive('MAX_COMPOUNDS', 100000)
        self.targets = positive('MAX_TARGETS', 1000)
        self.cells = positive('MAX_MATRIX_CELLS', 100000000)
        self.upload_rows = positive('MAX_AFFINITY_ROWS', 1000000)
        self.upload_bytes = positive('MAX_SESSION_UPLOAD_BYTES', 256 * MiB)
        self.session_bytes = positive('MAX_SESSION_STORAGE_BYTES', 6 * GiB)
        self.total_bytes = positive('MAX_RUNTIME_STORAGE_BYTES', 24 * GiB)
        self.free_bytes = positive('MIN_FREE_STORAGE_BYTES', 4 * GiB)
        self.sessions = positive('MAX_SESSIONS', 100)
        self.ttl = positive('SESSION_TTL_SECONDS', 7200)
        self.cleanup_seconds = positive('CLEANUP_INTERVAL_SECONDS', 30)
        self.optimization_seconds = positive('OPTIMIZATION_TIMEOUT_SECONDS', 14400)
        self.stage_seconds = positive('STAGE_TIMEOUT_SECONDS', 3600)
        self.stop_grace = positive('JOB_STOP_GRACE_SECONDS', 60)
        self.xlsx_bytes = positive('MAX_XLSX_EXPANDED_BYTES', 128 * MiB)
        self.files = positive('MAX_SESSION_FILES', 100)
        self.starts = positive('MAX_COMPUTATION_STARTS_PER_HOUR', 4)
        self.small_cells = positive('SMALL_JOB_MATRIX_CELLS', 10000000)

    def dimensions(self, rows, cols):
        if rows > self.compounds or cols > self.targets or rows * cols > self.cells:
            raise ValueError(f'Dataset exceeds limits: {self.compounds:,} compounds, '
                             f'{self.targets:,} targets, {self.cells:,} matrix cells.')


def configure_security(app):
    mode = os.environ.get('OPTILIB_ENV', 'development')
    if mode not in ('production', 'development'):
        raise ValueError('OPTILIB_ENV must be production or development')
    production = mode == 'production'
    secret = os.environ.get('SECRET_KEY', '')
    secure = os.environ.get('SESSION_COOKIE_SECURE', 'true' if production else 'false').lower() in ('true', '1', 'yes')
    hosts = [host.strip() for host in os.environ.get('TRUSTED_HOSTS', '').split(',') if host.strip()]
    if production:
        if len(secret.encode()) < 32 or any(s in secret.lower() for s in ('replace_', 'your_', 'change_me', 'changeme')):
            raise ValueError('Production requires a random SECRET_KEY of at least 32 bytes')
        if not secure or not hosts:
            raise ValueError('Production requires secure cookies and TRUSTED_HOSTS')
    app.config.update(SECRET_KEY=secret or os.urandom(32).hex(), DEBUG=False,
                      TEMPLATES_AUTO_RELOAD=not production, SESSION_COOKIE_SECURE=secure,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      TRUSTED_HOSTS=hosts or None, MAX_CONTENT_LENGTH=16 * MiB,
                      MAX_FORM_PARTS=25, MAX_FORM_MEMORY_SIZE=256 * 1024,
                      WTF_CSRF_TIME_LIMIT=18000)
    if production:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
