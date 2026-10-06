"""Signed-cookie and CSRF regressions for public optimization sessions."""
import json
import os
import re
import tempfile
import time
import unittest
from unittest.mock import patch

from itsdangerous import TimestampSigner

from webapp.public.config import Policy
from webapp.public.server import create_app


class PublicSessionTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            'OPTILIB_ENV': 'development',
            'SESSION_COOKIE_SECURE': 'false',
            'TRUSTED_HOSTS': '',
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy = Policy(self.tmp.name)
        self.policy.free_bytes = 1
        self.app = create_app(self.policy)
        self.app.config.update(TESTING=True)
        self.app.extensions['public_limiter'].enabled = False
        self.runtime = self.app.extensions['runtime']
        self.runtime.heartbeat(True)
        self.client = self.app.test_client()

        # Move only the signing clock; worker heartbeats and TTL use real time.
        self.signing_time = int(time.time())
        signing_clock = patch.object(TimestampSigner, 'get_timestamp',
                                     return_value=self.signing_time)
        self.signing_clock = signing_clock.start()
        self.addCleanup(signing_clock.stop)
        page = self.client.get('/optimize')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Set-Cookie', page.headers)
        self.sid = page.headers['X-Optilib-Session']
        self.csrf_token = re.search(r'name="csrf-token" content="([^"]+)"',
                                    page.text).group(1)
        self.cookie_name = self.app.config['SESSION_COOKIE_NAME']
        self.initial_cookie = self.client.get_cookie(self.cookie_name).value

    def prepare_dataset(self):
        # Admission needs an owned artifact reference, not a real matrix file.
        state = {
            'dataset': 'fixture/dataset',
            'dataset_info': {
                'ready': True, 'num_drugs': 3, 'num_targets': 2,
                'total_cost': 30,
            },
        }
        with self.runtime.connect() as db:
            db.execute('UPDATE sessions SET state=? WHERE sid=?',
                       (json.dumps(state), self.sid))

    def start_optimization(self):
        response = self.client.post('/api/run', json={
            'pop_size': 5, 'max_gen': 10,
        }, headers={'X-CSRFToken': self.csrf_token})
        self.assertEqual(response.status_code, 202, response.json)
        return response

    def test_optimization_survives_backward_signing_clock_without_cookie_refresh(self):
        self.prepare_dataset()
        admitted = self.start_optimization()
        job = admitted.json['job_id']
        progress = {
            'generation': 2, 'max_gen': 10,
            'history': [{'generation': 2, 'best_selectivity': 1,
                         'best_cost': 20}],
        }
        with self.runtime.connect() as db:
            db.execute("UPDATE jobs SET status='running',started=?,progress=? WHERE id=?",
                       (time.time(), json.dumps(progress), job))

        self.signing_clock.return_value = self.signing_time + 100
        forward = self.client.get('/api/status')
        self.signing_clock.return_value = self.signing_time + 99
        backward = self.client.get('/api/status')

        for response in (forward, backward):
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json['status'], 'running')
            self.assertEqual(response.json['generation'], 2)
            self.assertEqual(response.json['history'], progress['history'])
            self.assertEqual(response.headers['X-Optilib-Session'], self.sid)
        for response in (admitted, forward, backward):
            self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(self.client.get_cookie(self.cookie_name).value,
                         self.initial_cookie)
        self.assertEqual(self.runtime.job(job)['sid'], self.sid)
        self.assertEqual(self.runtime.job(job)['status'], 'running')

        self.assertTrue(self.runtime.complete(job, {}, {'status': 'complete'}))
        completed = self.client.get('/api/status')
        self.assertEqual(completed.json['status'], 'complete')
        self.assertEqual(completed.headers['X-Optilib-Session'], self.sid)
        self.assertNotIn('Set-Cookie', completed.headers)

        # The original page's CSRF token remains usable after the clock step.
        reset = self.client.post('/api/reset',
                                 headers={'X-CSRFToken': self.csrf_token})
        self.assertEqual(reset.status_code, 200, reset.json)
        self.assertNotEqual(reset.headers['X-Optilib-Session'], self.sid)
        self.assertIn('Set-Cookie', reset.headers)

    def test_idle_expiration_rotates_cookie_once(self):
        with self.runtime.connect() as db:
            db.execute('UPDATE sessions SET touched=? WHERE sid=?',
                       (time.time() - self.policy.ttl - 1, self.sid))
        expired = self.client.get('/api/dataset-info')
        self.assertEqual(expired.status_code, 200, expired.json)
        replacement_sid = expired.headers['X-Optilib-Session']
        self.assertNotEqual(replacement_sid, self.sid)
        self.assertIn('Set-Cookie', expired.headers)
        self.assertNotEqual(self.client.get_cookie(self.cookie_name).value,
                            self.initial_cookie)
        with self.runtime.connect() as db:
            retired = db.execute('SELECT retired FROM sessions WHERE sid=?',
                                 (self.sid,)).fetchone()[0]
        self.assertEqual(retired, 1)

        stable = self.client.get('/api/dataset-info')
        self.assertEqual(stable.headers['X-Optilib-Session'], replacement_sid)
        self.assertNotIn('Set-Cookie', stable.headers)

    def test_run_and_reset_require_csrf_and_reset_rotates_cookie(self):
        self.prepare_dataset()
        refused = self.client.post('/api/run', json={})
        self.assertEqual(refused.status_code, 400)
        with self.runtime.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(self.client.get_cookie(self.cookie_name).value,
                         self.initial_cookie)

        admitted = self.start_optimization()
        job = admitted.json['job_id']
        refused_reset = self.client.post('/api/reset')
        self.assertEqual(refused_reset.status_code, 400)
        self.assertEqual(self.runtime.job(job)['status'], 'reserved')

        reset = self.client.post('/api/reset',
                                 headers={'X-CSRFToken': self.csrf_token})
        self.assertEqual(reset.status_code, 200, reset.json)
        replacement_sid = reset.headers['X-Optilib-Session']
        self.assertNotEqual(replacement_sid, self.sid)
        self.assertIn('Set-Cookie', reset.headers)
        self.assertEqual(self.runtime.job(job)['status'], 'stopping')
        self.assertEqual(self.runtime.state(replacement_sid), {})

        status = self.client.get('/api/status')
        self.assertEqual(status.json['status'], 'idle')
        self.assertEqual(status.headers['X-Optilib-Session'], replacement_sid)
        self.assertNotIn('Set-Cookie', status.headers)


if __name__ == '__main__':
    unittest.main()
