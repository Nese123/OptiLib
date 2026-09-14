"""Read-only container liveness check for the supervisor."""
import sqlite3
import time
from contextlib import closing
from .config import Policy

if __name__ == '__main__':
    try:
        path=Policy().root/'jobs.sqlite'
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            row=db.execute("SELECT value FROM meta WHERE key='worker_heartbeat'").fetchone()
        raise SystemExit(0 if row and time.time()-float(row[0])<10 else 1)
    except (sqlite3.Error,OSError):
        raise SystemExit(1)
