"""Disposable spawned processes supervised outside the HTTP container."""
import logging
import multiprocessing
import os
import signal
import shutil
import stat
import tempfile
import resource
import time
from pathlib import Path

# Set these before child imports of NumPy, RDKit or sklearn.
for key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[key] = '1'

from .config import Policy, GiB
from .runtime import Runtime

logger = logging.getLogger('optilib.worker')


def run_child(policy, job_id):
    os.setsid()
    runtime = Runtime(policy)
    from .tasks import execute, Cancelled
    job = runtime.job(job_id)
    directory = runtime.directory(job['sid'],job_id)
    directory.mkdir(parents=True,exist_ok=True)
    # SQLite sorts and Python temporary files must use quota-accounted disk,
    # not the container's small request-spooling tmpfs.
    os.environ['SQLITE_TMPDIR'] = str(directory)
    os.environ['TMPDIR'] = str(directory)
    tempfile.tempdir = None
    resource.setrlimit(resource.RLIMIT_FSIZE,(job['reserved_bytes'],job['reserved_bytes']))
    try:
        execute(runtime,job)
    except Cancelled:
        runtime.fail(job_id,'Computation cancelled.','cancelled')
    except ValueError as exc:
        logger.info('Rejected job %s: %s',job_id,exc)
        runtime.fail(job_id,str(exc))
    except BaseException:
        if runtime.job(job_id)['status'] == 'stopping':
            runtime.fail(job_id,'Computation cancelled.','cancelled')
        else:
            logger.exception('Job %s failed',job_id)
            runtime.fail(job_id)


def resident_bytes(pid):
    try:
        return int(Path(f'/proc/{pid}/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
    except (OSError,ValueError,IndexError):
        return 0


def unlinked_storage(pid, directory):
    """SQLite unlinks open scratch files; include their still-allocated space."""
    seen = set()
    total = 0
    try:
        descriptors = list(Path(f'/proc/{pid}/fd').iterdir())
    except OSError:
        return 0
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
            info = descriptor.stat()
            key = (info.st_dev,info.st_ino)
            if (target.startswith(str(directory)+'/') and target.endswith(' (deleted)')
                    and stat.S_ISREG(info.st_mode) and key not in seen):
                seen.add(key)
                total += info.st_size
        except OSError:
            continue
    return total


class Supervisor:
    def __init__(self, runtime):
        self.runtime = runtime
        self.children = {}
        self.context = multiprocessing.get_context('spawn')
        self.last_cleanup = 0

    def terminate(self, job_id, message, status='error'):
        process = self.children.pop(job_id,None)
        if process:
            if process.is_alive():
                try:
                    os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.join(timeout=1)
        self.runtime.fail(job_id,message,status)

    def tick(self):
        now = time.time()
        self.runtime.heartbeat()
        with self.runtime.connect() as db:
            pending = [dict(row) for row in db.execute("SELECT * FROM jobs WHERE status IN ('reserved','running','stopping')")]
        for job in pending:
            job_id = job['id']
            if job['status'] == 'reserved' and job_id not in self.children:
                held = sum(getattr(process,'optilib_memory',4) for process in self.children.values() if process.is_alive())
                if held + job['memory'] > 4:
                    continue
                # Upload requests reserve capacity before writing their body. The
                # ready marker prevents a worker seeing an incomplete request.
                if not (self.runtime.directory(job['sid'],job_id)/'.ready').exists():
                    if now-job['created'] > 300:
                        self.runtime.fail(job_id,'Upload timed out.')
                    continue
                with self.runtime.connect() as db:
                    changed = db.execute("UPDATE jobs SET status='running',started=? WHERE id=? AND status='reserved'",(now,job_id)).rowcount
                if changed:
                    process = self.context.Process(target=run_child,args=(self.runtime.policy,job_id))
                    try:
                        process.optilib_memory = job['memory']
                        process.start()
                        self.children[job_id] = process
                    except Exception:
                        logger.exception('Could not start job %s',job_id)
                        self.runtime.fail(job_id)
                continue
            process = self.children.get(job_id)
            if process is None:
                self.runtime.fail(job_id,'Computation interrupted by service restart.','cancelled')
                continue
            if not process.is_alive():
                process.join(); self.children.pop(job_id,None)
                self.runtime.fail(job_id,'Computation worker exited unexpectedly.')
                continue
            directory = self.runtime.directory(job['sid'],job_id)
            scratch = unlinked_storage(process.pid,directory)
            maximum = self.runtime.policy.optimization_seconds if job['kind']=='optimization' else self.runtime.policy.stage_seconds
            if job['status']=='running' and now-(job['started'] or now) >= maximum:
                with self.runtime.connect() as db:
                    db.execute("UPDATE jobs SET status='stopping',stop_at=? WHERE id=? AND status='running'",(now,job_id))
            if job['status']=='stopping' and now-(job['stop_at'] or now) >= self.runtime.policy.stop_grace:
                self.terminate(job_id,'Computation stopped at its time limit.','cancelled')
            elif resident_bytes(process.pid) > job['memory']*GiB:
                self.terminate(job_id,'Computation exceeded its memory allowance. Reduce the workload.')
            elif (self.runtime.usage(job['sid']) + scratch > self.runtime.policy.session_bytes
                  or self.runtime.usage() + scratch > self.runtime.policy.total_bytes
                  or shutil.disk_usage(self.runtime.root).free < self.runtime.policy.free_bytes
                  or sum(p.stat().st_size for p in directory.rglob('*') if p.is_file()) + scratch > job['reserved_bytes']):
                self.terminate(job_id,'Computation exceeded its temporary storage allowance.')
        # A completed child may still be exiting; retain its process until reaped.
        for job_id,process in list(self.children.items()):
            if not process.is_alive():
                process.join(); self.children.pop(job_id,None)
        if now-self.last_cleanup>self.runtime.policy.cleanup_seconds:
            self.runtime.cleanup(); self.runtime.prune_artifacts(); self.last_cleanup=now

    def close(self):
        for job_id in list(self.children):
            self.terminate(job_id,'Computation interrupted by service shutdown.','cancelled')


def main():
    logging.basicConfig(level=getattr(logging,os.environ.get('LOG_LEVEL','INFO').upper(),logging.INFO))
    runtime = Runtime(Policy())
    # One supervisor per runtime; a second instance must fail, not steal jobs.
    import fcntl
    lock = open(runtime.root/'worker.lock','w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    model_ok = False
    try:
        from webapp.core.pricing import NumpyFingerprints, MOLPRICE_DIR
        model = NumpyFingerprints(weights_path=str(MOLPRICE_DIR/'models/Numpy/MP_Morgan_hybrid.pkl'))
        result = model.predict_batch_from_smiles(['CC'],errors='raise')
        import numpy as np
        model_ok = bool(np.isfinite(result).all())
        del model
    except Exception:
        logger.exception('Model readiness validation failed')
    runtime.heartbeat(model_ok)
    supervisor = Supervisor(runtime)
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    try:
        while True:
            supervisor.tick()
            time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        supervisor.close()
        with runtime.connect() as db:
            db.execute("DELETE FROM meta WHERE key='worker_heartbeat'")
        lock.close()


if __name__ == '__main__':
    main()
