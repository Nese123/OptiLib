"""Transactional admission, ownership and publication for disposable workers."""
import json
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import GiB

ACTIVE = ('reserved', 'running', 'stopping')


class Rejected(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def identifier(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError):
        raise Rejected('Not found', 404) from None


class Runtime:
    def __init__(self, policy):
        self.policy = policy
        self.root = policy.root
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'sessions').mkdir(exist_ok=True)
        self.path = self.root / 'jobs.sqlite'
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS sessions(
                    sid TEXT PRIMARY KEY, epoch TEXT, touched REAL, revision INTEGER DEFAULT 0,
                    state TEXT DEFAULT '{}', retired INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, sid TEXT, ip TEXT, kind TEXT, revision INTEGER,
                    status TEXT, created REAL, started REAL, stop_at REAL,
                    memory INTEGER, reserved_bytes INTEGER, spec TEXT,
                    progress TEXT DEFAULT '{}', result TEXT, error TEXT,
                    FOREIGN KEY(sid) REFERENCES sessions(sid));
                CREATE TABLE IF NOT EXISTS starts(ip TEXT,created REAL);
                CREATE INDEX IF NOT EXISTS starts_ip ON starts(ip,created);
                CREATE INDEX IF NOT EXISTS jobs_active ON jobs(status);
                CREATE INDEX IF NOT EXISTS jobs_session ON jobs(sid, created);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def start_epoch(self):
        epoch = str(uuid.uuid4())
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('epoch', epoch))
            db.execute('UPDATE sessions SET retired=1')
            db.execute("UPDATE jobs SET status='stopping',stop_at=? WHERE status IN ('reserved','running')", (time.time(),))
        return epoch

    def directory(self, sid, job=None):
        path = self.root / 'sessions' / identifier(sid)
        if job:
            path /= identifier(job)
        return path

    def artifact(self, sid, reference):
        # References are generated internally and still checked at every boundary.
        base = self.directory(sid).resolve()
        path = (base / reference).resolve()
        if not path.is_relative_to(base) or path == base:
            raise Rejected('Invalid artifact', 404)
        return path

    def session(self, sid, epoch):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM sessions WHERE sid=? AND epoch=? AND retired=0', (sid, epoch)).fetchone()
            if row and time.time() - row['touched'] > self.policy.ttl:
                active = db.execute("SELECT 1 FROM jobs WHERE sid=? AND status IN ('reserved','running','stopping')", (sid,)).fetchone()
                if not active:
                    db.execute('UPDATE sessions SET retired=1 WHERE sid=?', (sid,))
                    row = None
            if row is None:
                count = db.execute('SELECT count(*) FROM sessions WHERE retired=0').fetchone()[0]
                if count >= self.policy.sessions:
                    raise Rejected('The server has reached its session capacity. Try again later.')
                sid = str(uuid.uuid4())
                db.execute('INSERT INTO sessions(sid,epoch,touched) VALUES (?,?,?)', (sid, epoch, time.time()))
                row = db.execute('SELECT * FROM sessions WHERE sid=?', (sid,)).fetchone()
            db.execute('UPDATE sessions SET touched=? WHERE sid=?', (time.time(), sid))
            result = dict(row)
            result['state'] = json.loads(row['state'])
            return result

    def state(self, sid):
        with self.connect() as db:
            row = db.execute('SELECT * FROM sessions WHERE sid=?', (sid,)).fetchone()
            if not row:
                raise Rejected('Session expired', 404)
            return json.loads(row['state'])

    def retire(self, sid):
        with self.connect() as db:
            db.execute('UPDATE sessions SET retired=1 WHERE sid=?', (sid,))
            db.execute("UPDATE jobs SET status='stopping',stop_at=? WHERE sid=? AND status IN ('reserved','running')", (time.time(), sid))

    def usage(self, sid=None):
        base = self.directory(sid) if sid else self.root
        total = 0
        if base.exists():
            for root, dirs, files in os.walk(base, followlinks=False):
                for name in files:
                    try:
                        total += (Path(root) / name).lstat().st_size
                    except FileNotFoundError:
                        pass
        return total

    def admit(self, sid, ip, kind, payload, *, cells=0, reserve=None):
        memory = 4 if cells > self.policy.small_cells or kind in ('chembl', 'affinity') else 2
        reserve = reserve if reserve is not None else (4 * GiB if kind in ('chembl','affinity','export') else GiB)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            session = db.execute('SELECT * FROM sessions WHERE sid=? AND retired=0', (sid,)).fetchone()
            if session is None:
                raise Rejected('Session expired', 409)
            heartbeat = db.execute("SELECT value FROM meta WHERE key='worker_heartbeat'").fetchone()
            if not heartbeat or time.time() - float(heartbeat[0]) > 10:
                raise Rejected('Computation service is unavailable. Please try again shortly.')
            active = db.execute("SELECT * FROM jobs WHERE status IN ('reserved','running','stopping')").fetchall()
            if kind == 'export':
                existing = next((row for row in active if row['sid'] == sid and row['kind'] == 'export'
                                 and json.loads(row['spec'])['payload'].get('key') == payload.get('key')),None)
                if existing is not None:
                    self.directory(sid,existing['id']).mkdir(parents=True,exist_ok=True)
                    return existing['id']
            if any(row['sid'] == sid for row in active):
                raise Rejected('A computation is already running in this session', 409)
            if any(row['ip'] == ip for row in active):
                raise Rejected('A computation is already running for this network address', 429)
            if len(active) >= 2 or sum(row['memory'] for row in active) + memory > 4:
                raise Rejected('The server is busy. Please try again shortly.')
            # Export, selection and upload polling do not consume optimization starts.
            if kind in ('optimization', 'chembl', 'affinity'):
                count = db.execute("SELECT count(*) FROM starts WHERE ip=? AND created>?", (ip, time.time()-3600)).fetchone()[0]
                if count >= self.policy.starts:
                    raise Rejected('Hourly computation limit reached. Please try again later.', 429)
            if self.usage(sid) + reserve > self.policy.session_bytes:
                raise Rejected('Session storage limit reached. Reset the session or remove files.', 413)
            reservations = sum(row['reserved_bytes'] for row in active)
            if self.usage() + reservations + reserve > self.policy.total_bytes or shutil.disk_usage(self.root).free - reservations - reserve < self.policy.free_bytes:
                raise Rejected('Insufficient temporary storage. Please try again later.')
            if kind in ('optimization','chembl','affinity'):
                db.execute('INSERT INTO starts VALUES (?,?)',(ip,time.time()))
            job = str(uuid.uuid4())
            spec = {'payload': payload, 'snapshot': json.loads(session['state'])}
            db.execute('INSERT INTO jobs(id,sid,ip,kind,revision,status,created,memory,reserved_bytes,spec) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (job, sid, ip, kind, session['revision'], 'reserved', time.time(), memory, reserve, json.dumps(spec, allow_nan=False)))
        path = self.directory(sid, job)
        path.mkdir(parents=True, exist_ok=True)
        (path.parent / '.optilib-session').touch()
        return job

    def job(self, job, sid=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (identifier(job),)).fetchone()
            if row is None or (sid is not None and row['sid'] != sid):
                raise Rejected('Job not found', 404)
            result = dict(row)
            for key in ('spec', 'progress', 'result'):
                result[key] = json.loads(result[key]) if result[key] else None
            return result

    def progress(self, job, values):
        with self.connect() as db:
            db.execute('UPDATE jobs SET progress=? WHERE id=?', (json.dumps(values, allow_nan=False), job))

    def complete(self, job, changes, response):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT j.*,s.retired,s.revision AS current_revision,s.state FROM jobs j JOIN sessions s ON s.sid=j.sid WHERE j.id=?', (job,)).fetchone()
            if not row or row['retired'] or row['current_revision'] != row['revision'] or row['status'] not in ('running','stopping'):
                if row:
                    db.execute("UPDATE jobs SET status='cancelled',reserved_bytes=0 WHERE id=?", (job,))
                return False
            state = json.loads(row['state'])
            state.update(changes)
            db.execute('UPDATE sessions SET state=?, revision=revision+1,touched=? WHERE sid=?', (json.dumps(state, allow_nan=False), time.time(), row['sid']))
            db.execute("UPDATE jobs SET status='complete',result=?,reserved_bytes=0 WHERE id=?", (json.dumps(response, allow_nan=False), job))
            return True

    def fail(self, job, message='Computation failed. Please try again.', status='error'):
        with self.connect() as db:
            db.execute('UPDATE jobs SET status=?,error=?,reserved_bytes=0 WHERE id=? AND status != ?', (status, message, job, 'complete'))

    def stop(self, sid):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='stopping',stop_at=? WHERE sid=? AND status IN ('running','reserved')", (time.time(), sid))

    def heartbeat(self, model_ok=None):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('worker_heartbeat',?)", (str(time.time()),))
            if model_ok is not None:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('model_ok',?)", ('1' if model_ok else '0',))

    def cleanup(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM starts WHERE created<?',(time.time()-3600,))
            db.execute("UPDATE sessions SET retired=1 WHERE touched<? AND sid NOT IN (SELECT sid FROM jobs WHERE status IN ('reserved','running','stopping'))", (time.time()-self.policy.ttl,))
            rows = db.execute("SELECT sid FROM sessions WHERE retired=1 AND sid NOT IN (SELECT sid FROM jobs WHERE status IN ('reserved','running','stopping'))").fetchall()
            for row in rows:
                directory = self.directory(row['sid'])
                if directory.is_symlink():
                    continue
                if (directory / '.optilib-session').is_file():
                    shutil.rmtree(directory)
                db.execute('DELETE FROM jobs WHERE sid=?', (row['sid'],))
                db.execute('DELETE FROM sessions WHERE sid=?', (row['sid'],))

    def prune_artifacts(self):
        with self.connect() as db:
            sessions = db.execute('SELECT sid,state FROM sessions WHERE retired=0').fetchall()
            for session in sessions:
                active = db.execute("SELECT 1 FROM jobs WHERE sid=? AND status IN ('reserved','running','stopping')",(session['sid'],)).fetchone()
                if active:
                    continue
                state = json.loads(session['state'])
                references = [state.get(key) for key in ('uploads','dataset','solutions','selection')]
                references.extend(state.get('exports',{}).values())
                keep = {Path(ref).parts[0] for ref in references if isinstance(ref,str) and ref}
                directory = self.directory(session['sid'])
                if not (directory/'.optilib-session').is_file():
                    continue
                for candidate in directory.iterdir():
                    if candidate.is_symlink() or not candidate.is_dir() or candidate.name in keep:
                        continue
                    try:
                        identifier(candidate.name)
                    except Rejected:
                        continue
                    job = db.execute('SELECT status,created FROM jobs WHERE id=?',(candidate.name,)).fetchone()
                    if job and job['status'] not in ACTIVE and time.time()-candidate.stat().st_mtime>120:
                        shutil.rmtree(candidate)
                # Keep current artifact jobs and active tasks; old completed
                # status/history records must not grow forever in live sessions.
                old = db.execute("SELECT id FROM jobs WHERE sid=? AND created<? AND status NOT IN ('reserved','running','stopping')",(session['sid'],time.time()-self.policy.ttl)).fetchall()
                for row in old:
                    if row['id'] not in keep:
                        db.execute('DELETE FROM jobs WHERE id=?',(row['id'],))
