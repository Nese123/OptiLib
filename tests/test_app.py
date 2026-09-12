"""Route and pipeline regressions without local databases or running workers."""
import importlib
import json
import hashlib
import sqlite3
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

# Importing the app normally initializes databases, removes output, and starts
# its cleaner. Suppress those process side effects in the test runner.
with patch('sqlite3.connect'), patch('shutil.rmtree'), patch.object(Path, 'unlink'), \
        patch('threading.Thread.start'), patch('atexit.register'):
    app_module = importlib.import_module('webapp.app')


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = patch.object(app_module, 'PROJECT_ROOT', Path(self.tmp.name))
        root.start()
        self.addCleanup(root.stop)
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        app_module.limiter.enabled = False
        slots = patch.object(app_module, '_job_slots', threading.BoundedSemaphore(2))
        slots.start()
        self.addCleanup(slots.stop)
        app_module._sessions.clear()
        self.client = app_module.app.test_client()
        self.client.get('/api/status')
        with self.client.session_transaction() as session:
            self.sid = session['sid']
        self.state = app_module._sessions[self.sid]

    def test_reset_isolates_active_jobs(self):
        self.state['opt_state']['status'] = 'running'
        response = self.client.post('/api/reset')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.state['opt_state']['stop_requested'])
        # Simulate the retired job publishing after reset.
        self.state['opt_state']['status'] = 'complete'
        self.state['dataset']['ready'] = True
        self.assertEqual(self.client.get('/api/status').json['status'], 'idle')
        self.assertFalse(self.client.get('/api/dataset-info').json['ready'])
        with self.client.session_transaction() as session:
            self.assertNotEqual(session['sid'], self.sid)

    def test_active_job_blocks_all_start_routes_and_opt_reset(self):
        self.state['dataset']['ready'] = True
        for state_key in ('opt_state', 'pipeline_state'):
            with self.subTest(state_key=state_key), patch.object(app_module.threading, 'Thread') as thread:
                self.state[state_key]['status'] = 'running'
                for endpoint, body in (
                    ('/api/run', {}),
                    ('/api/build-matrix', {'chembl_ids': ['CHEMBL1']}),
                    ('/api/build-matrix-from-affinity', {}),
                    ('/api/reset-opt', {}),
                ):
                    self.assertEqual(self.client.post(endpoint, json=body).status_code, 409)
                thread.assert_not_called()
                self.state[state_key]['status'] = 'idle'

    def test_start_claims_session_before_worker_starts(self):
        self.state['dataset']['ready'] = True
        with patch.object(app_module.threading, 'Thread'):
            self.assertEqual(self.client.post('/api/run', json={}).status_code, 200)
            self.assertEqual(self.client.post('/api/run', json={}).status_code, 409)

    def test_early_stop_rejects_infeasible_population(self):
        cb = SimpleNamespace(last_pop_G=np.array([[1.], [2.]]),
                             last_pop_X=np.array([[True, False], [False, True]]),
                             last_pop_F=np.array([[-.5, .5], [-.2, .7]]))
        with patch.object(app_module, '_process_and_store_results') as store:
            with self.assertRaisesRegex(ValueError, 'coverage'):
                app_module._process_stopped_results(self.sid, cb, None)
            store.assert_not_called()

    def test_early_stop_keeps_only_feasible_nondominated_solutions(self):
        cb = SimpleNamespace(last_pop_G=np.array([[0.], [0.], [0.], [1.]]),
                             last_pop_X=np.eye(4, dtype=bool),
                             last_pop_F=np.array([[-.8, .4], [-.5, .6], [-.9, .7], [-1., .1]]))
        problem = SimpleNamespace(pool_baseline_score=1., pool_total_cost=10.)
        with patch.object(app_module, '_process_and_store_results') as store:
            app_module._process_stopped_results(self.sid, cb, problem)
            np.testing.assert_array_equal(store.call_args.args[1], cb.last_pop_X[[0, 2]])
            np.testing.assert_array_equal(store.call_args.args[2], cb.last_pop_F[[0, 2]])
        self.assertEqual(self.state['opt_state']['status'], 'complete')

    def test_cache_tracks_custom_price_changes_and_removal(self):
        output = Path(self.tmp.name) / 'webapp/output' / self.sid
        output.mkdir(parents=True)
        database = Path(self.tmp.name) / 'chembl.db'
        sqlite3.connect(database).close()
        provenance = {'scoring_version': app_module.SELECTIVITY_SCORING_VERSION, 'build_id': 'test-build'}
        for price_map, price in (({}, 10.), ({'a': 999.}, 999.)):
            key = app_module._matrix_cache_key(['CHEMBL1'], .5, True, 1, price_map, provenance)
            pd.DataFrame({'SMILES': ['CC'], 'Price_USD_per_mg': [price], 'T': [2.]}).to_csv(
                output / f'selectivity_matrix_{key}.csv', index=False)
        for price_map, expected in (({}, 10.), ({'a': 999.}, 999.), ({}, 10.)):
            self.state['price_upload_state']['price_map'] = price_map
            with patch.object(app_module, 'get_chembl_db_path', return_value=database), \
                    patch.object(app_module, 'get_selectivity_provenance', return_value=provenance):
                app_module._run_pipeline(self.sid, ['CHEMBL1'], .5, True, 1)
            self.assertEqual(self.state['dataset']['prices'].tolist(), [expected])

    def test_cleaner_preserves_running_session_and_expires_idle_session(self):
        output = Path(self.tmp.name) / 'webapp/output' / self.sid
        output.mkdir(parents=True)
        self.state['last_activity'] = 0
        self.state['pipeline_state']['status'] = 'running'
        app_module._cleanup_stale_sessions()
        self.assertTrue(output.exists())
        self.assertIn(self.sid, app_module._sessions)
        self.state['pipeline_state']['status'] = 'complete'
        app_module._cleanup_stale_sessions()
        self.assertFalse(output.exists())
        self.assertNotIn(self.sid, app_module._sessions)
