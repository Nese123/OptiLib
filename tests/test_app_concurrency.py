"""Admission and session-isolation regressions without databases or optimizations."""

import importlib
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

with patch('sqlite3.connect'), patch('shutil.rmtree'), patch.object(Path, 'unlink'), \
        patch('threading.Thread.start'), patch('atexit.register'):
    app_module = importlib.import_module('webapp.app')


class AppConcurrencyTests(unittest.TestCase):
    start_routes = (
        ('/api/run', {}),
        ('/api/build-matrix', {'chembl_ids': ['CHEMBL1']}),
        ('/api/build-matrix-from-affinity', {}),
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for attribute, value in (
            ('PROJECT_ROOT', Path(self.tmp.name)),
            ('_sessions', {}),
            ('_job_slots', threading.BoundedSemaphore(2)),
        ):
            replacement = patch.object(app_module, attribute, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        app_module.limiter.enabled = False
        self.slots = app_module._job_slots
        self.client, self.sid, self.state = self.make_client()

    def make_client(self):
        client = app_module.app.test_client()
        client.get('/api/status')
        with client.session_transaction() as session:
            sid = session['sid']
        state = app_module._sessions[sid]
        state['dataset'].update(ready=True, matrix_file='previous.csv')
        state['opt_state'].update(status='complete', generation=42,
                                  history=[{'generation': 42}])
        state['opt_results'].update(pareto_front=[[1., 10.]], selected_idx=0,
                                    comparison={'previous': True})
        state['affinity_upload_state']['df'] = pd.DataFrame({'Target': ['A']})
        state.dataset_revision = 4
        state.run_revision = 5
        state.selection_revision = 6
        return client, sid, state

    def snapshot(self, state):
        keys = ('dataset', 'opt_results', 'pipeline_state', 'opt_state')
        return (
            {key: dict(state[key]) for key in keys},
            (state.dataset_revision, state.run_revision, state.selection_revision),
        )

    def assert_slots_free(self):
        acquired = 0
        try:
            for _ in range(2):
                self.assertTrue(self.slots.acquire(blocking=False))
                acquired += 1
            self.assertFalse(self.slots.acquire(blocking=False))
        finally:
            for _ in range(acquired):
                self.slots.release()

    def test_busy_capacity_rejects_all_start_routes_without_discarding_results(self):
        self.slots.acquire()
        self.slots.acquire()
        before = self.snapshot(self.state)
        try:
            with patch.object(app_module.threading, 'Thread') as thread:
                for route, body in self.start_routes:
                    with self.subTest(route=route):
                        response = self.client.post(route, json=body)
                        self.assertEqual(response.status_code, 503)
                        self.assertIn('busy', response.json['error'])
                        self.assertEqual(self.snapshot(self.state), before)
                thread.assert_not_called()
        finally:
            self.slots.release()
            self.slots.release()

    def test_thread_start_failure_restores_state_and_releases_capacity(self):
        before = self.snapshot(self.state)
        original_sections = {key: self.state[key] for key in before[0]}
        with patch.object(app_module.threading, 'Thread') as thread, \
                patch.object(app_module.logger, 'exception'):
            thread.return_value.start.side_effect = RuntimeError('cannot start thread')
            for route, body in self.start_routes:
                with self.subTest(route=route):
                    response = self.client.post(route, json=body)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(self.snapshot(self.state), before)
                    for key, section in original_sections.items():
                        self.assertIs(self.state[key], section)
                    self.assert_slots_free()

    def test_worker_failure_releases_capacity(self):
        with patch.object(app_module.threading, 'Thread') as thread, \
                patch.object(app_module, '_run_nsga2', side_effect=RuntimeError('worker failed')), \
                patch.object(app_module.logger, 'exception'):
            response = self.client.post('/api/run', json={})
            self.assertEqual(response.status_code, 200, response.json)
            work = thread.call_args.kwargs['target']
            work()
        self.assertEqual(self.state['opt_state']['status'], 'error')
        self.assertEqual(self.state['opt_state']['error'], 'worker failed')
        self.assert_slots_free()

    def test_retired_jobs_keep_capacity_until_their_workers_finish(self):
        def complete(sid, *args):
            app_module._sessions[sid]['opt_state']['status'] = 'complete'

        with patch.object(app_module.threading, 'Thread') as thread, \
                patch.object(app_module, '_run_nsga2', side_effect=complete):
            self.assertEqual(self.client.post('/api/run', json={}).status_code, 200)
            retired_work = thread.call_args.kwargs['target']
            self.assertEqual(self.client.post('/api/reset').status_code, 200)
            self.assertTrue(self.state['opt_state']['stop_requested'])
            with self.client.session_transaction() as session:
                current_state = app_module._sessions[session['sid']]
            current_state['dataset']['ready'] = True
            self.assertEqual(self.client.post('/api/run', json={}).status_code, 200)
            current_work = thread.call_args.kwargs['target']

            third_client, _, third_state = self.make_client()
            third_before = self.snapshot(third_state)
            self.assertEqual(third_client.post('/api/run', json={}).status_code, 503)
            self.assertEqual(self.snapshot(third_state), third_before)
            retired_work()
            self.assertEqual(third_client.post('/api/run', json={}).status_code, 200)
            third_work = thread.call_args.kwargs['target']
            current_work()
            third_work()
        self.assert_slots_free()

    def test_pipeline_and_optimization_share_the_same_capacity(self):
        second_client, _, _ = self.make_client()
        third_client, _, _ = self.make_client()
        with patch.object(app_module.threading, 'Thread') as thread, \
                patch.object(app_module, '_run_nsga2'), \
                patch.object(app_module, '_run_affinity_pipeline'):
            self.assertEqual(self.client.post('/api/run', json={}).status_code, 200)
            first_work = thread.call_args.kwargs['target']
            self.assertEqual(second_client.post('/api/build-matrix-from-affinity', json={}).status_code, 200)
            second_work = thread.call_args.kwargs['target']
            self.assertEqual(third_client.post('/api/build-matrix', json={'chembl_ids': ['CHEMBL1']}).status_code, 503)
            first_work()
            second_work()
        self.assert_slots_free()

    def test_invalid_numeric_limits_preserve_results_without_starting_workers(self):
        before = self.snapshot(self.state)
        invalid = (
            {'pop_size': 501}, {'max_gen': 5001}, {'term_period': 501},
            {'pop_size': 10.5}, {'pop_size': True}, {'max_gen': None},
            {'weight_mean': 'nan'}, {'ftol': 'inf'}, {'mutation_multiplier': -1},
        )
        with patch.object(app_module.threading, 'Thread') as thread:
            for body in invalid:
                with self.subTest(body=body):
                    self.assertEqual(self.client.post('/api/run', json=body).status_code, 400)
                    self.assertEqual(self.snapshot(self.state), before)
            thread.assert_not_called()
        self.assert_slots_free()

    def test_waiting_session_request_does_not_block_other_sessions(self):
        second_client, _, _ = self.make_client()
        attempted = threading.Event()
        first_finished = threading.Event()
        second_finished = threading.Event()
        responses = {}

        def query(client, name, done):
            try:
                if name == 'first':
                    attempted.set()
                responses[name] = client.get('/api/status').status_code
            except Exception as exc:
                responses[name] = exc
            finally:
                done.set()

        first = threading.Thread(target=query, args=(self.client, 'first', first_finished), daemon=True)
        second = threading.Thread(target=query, args=(second_client, 'second', second_finished), daemon=True)
        with self.state.lock:
            first.start()
            self.assertTrue(attempted.wait(2))
            second.start()
            independent = second_finished.wait(2)
            first_was_blocked = not first_finished.is_set()
        first.join(2)
        second.join(2)
        self.assertTrue(independent, 'A request waiting for one session blocked another session')
        self.assertTrue(first_was_blocked)
        self.assertEqual(responses, {'first': 200, 'second': 200})

    def selection_fixture(self):
        frame = pd.DataFrame({
            'SMILES': ['CC', 'CCC'], 'Compound_Name': ['A', 'B'],
            'Price_USD_per_mg': [10., 20.], 'T': [2., 1.], 'U': [1., 3.],
        })
        matrix_file = Path(self.tmp.name) / 'matrix.csv'
        frame.to_csv(matrix_file, index=False)
        old_file = Path(self.tmp.name) / 'previous.xlsx'
        frame.iloc[[0]].to_excel(old_file, index=False)
        matrix = frame[['T', 'U']].to_numpy()
        prices = frame['Price_USD_per_mg'].to_numpy()
        problem = app_module.DrugLibraryProblem(matrix, prices)
        self.state['dataset'].update(
            matrix_file=str(matrix_file), selectivities=matrix, prices=prices,
        )
        self.state['opt_results'].update(
            res_X=np.eye(2, dtype=bool), res_F=np.array([[-1., .3], [-.8, .6]]),
            problem=problem, winning_file=str(old_file), winning_matrix_df=frame.iloc[[0]],
            heatmap_cache={'previous': True},
        )
        target_info = patch.object(app_module, '_get_target_info', side_effect=lambda names: (names, names))
        target_info.start()
        self.addCleanup(target_info.stop)
        return old_file

    def same_session_client(self):
        client = app_module.app.test_client()
        with client.session_transaction() as session:
            session['sid'] = self.sid
        return client

    def request_in_thread(self, client, route, body=None):
        finished = threading.Event()
        result = {}

        def request():
            try:
                result['response'] = client.get(route) if body is None else client.post(route, json=body)
            except Exception as exc:
                result['exception'] = exc
            finally:
                finished.set()

        thread = threading.Thread(target=request, daemon=True)
        thread.start()
        return thread, finished, result

    def test_newest_selection_wins_when_older_heatmap_finishes_last(self):
        old_file = self.selection_fixture()
        preparing = threading.Event()
        release = threading.Event()
        heatmap = app_module._build_heatmap_cache

        def slow_first_heatmap(frame):
            if frame['Compound_Name'].tolist() == ['A']:
                preparing.set()
                if not release.wait(5):
                    raise RuntimeError('Timed out waiting to finish the older selection')
            return heatmap(frame)

        with patch.object(app_module, '_build_heatmap_cache', side_effect=slow_first_heatmap):
            first, _, first_result = self.request_in_thread(self.client, '/api/select-solution', {'index': 0})
            second = None
            try:
                self.assertTrue(preparing.wait(2))
                second, done, second_result = self.request_in_thread(
                    self.same_session_client(), '/api/select-solution', {'index': 1},
                )
                self.assertTrue(done.wait(2), 'A slow selection blocked a newer selection')
                self.assertEqual(second_result['response'].status_code, 200)
                winning_file = Path(self.state['opt_results']['winning_file'])
                self.assertFalse(winning_file.exists())
                download = self.same_session_client().get('/api/download/library')
                self.assertEqual(download.status_code, 200)
                download.close()
                self.assertEqual(pd.read_excel(winning_file)['Compound_Name'].tolist(), ['B'])
            finally:
                release.set()
                first.join(2)
                if second is not None:
                    second.join(2)
        self.assertEqual(first_result['response'].status_code, 409)
        self.assertEqual(self.state['opt_results']['selected_idx'], 1)
        self.assertEqual(self.state['opt_results']['winning_file'], str(winning_file))
        self.assertTrue(old_file.is_file())
        self.assertEqual(list(winning_file.parent.glob('optimized_library_*.xlsx')), [winning_file])

    def test_status_remains_available_during_solution_preparation_and_heatmap_work(self):
        self.selection_fixture()
        for helper in ('_prepare_solution', '_build_heatmap_cache'):
            with self.subTest(helper=helper):
                preparing = threading.Event()
                release = threading.Event()
                original = getattr(app_module, helper)

                def slow_work(*args, **kwargs):
                    preparing.set()
                    if not release.wait(5):
                        raise RuntimeError('Timed out waiting for status request')
                    return original(*args, **kwargs)

                with patch.object(app_module, helper, side_effect=slow_work):
                    selection, _, selected = self.request_in_thread(
                        self.client, '/api/select-solution', {'index': 1},
                    )
                    status = None
                    try:
                        self.assertTrue(preparing.wait(2))
                        status, done, status_result = self.request_in_thread(self.same_session_client(), '/api/status')
                        self.assertTrue(done.wait(2), f'{helper} held the session lock')
                        self.assertEqual(status_result['response'].status_code, 200)
                    finally:
                        release.set()
                        selection.join(2)
                        if status is not None:
                            status.join(2)
                self.assertEqual(selected['response'].status_code, 200)

    def test_reset_invalidates_an_inflight_selection_and_removes_only_its_export(self):
        old_file = self.selection_fixture()
        preparing = threading.Event()
        release = threading.Event()
        prepare = app_module._prepare_solution
        prepared = {}

        def pause_before_publication(*args, **kwargs):
            prepared.update(prepare(*args, **kwargs))
            preparing.set()
            if not release.wait(5):
                raise RuntimeError('Timed out waiting for reset')
            return prepared

        with patch.object(app_module, '_prepare_solution', side_effect=pause_before_publication):
            selection, _, selected = self.request_in_thread(self.client, '/api/select-solution', {'index': 1})
            try:
                self.assertTrue(preparing.wait(2))
                self.assertEqual(self.same_session_client().post('/api/reset-opt').status_code, 200)
            finally:
                release.set()
                selection.join(2)
        self.assertEqual(selected['response'].status_code, 409)
        self.assertEqual(self.state['opt_state']['status'], 'idle')
        self.assertIsNone(self.state['opt_results']['res_X'])
        self.assertIsNone(self.state['opt_results']['winning_file'])
        self.assertFalse(Path(prepared['winning_file']).exists())
        self.assertTrue(old_file.is_file())

    def test_selection_after_publication_does_not_leave_finished_run_running(self):
        self.selection_fixture()
        results = self.state['opt_results']
        result = SimpleNamespace(X=results['res_X'], F=results['res_F'])
        callback = SimpleNamespace(
            last_pop_X=result.X, last_pop_F=result.F, last_pop_G=np.zeros((2, 1)),
        )

        def select_after_publication(*args, **kwargs):
            self.state.selection_revision += 1
            return True

        for stopped in (False, True):
            with self.subTest(stopped=stopped), \
                    patch.object(app_module, '_process_and_store_results', side_effect=select_after_publication), \
                    patch.object(app_module, 'run_optimization', return_value=(result, 0.)):
                self.state['opt_state']['status'] = 'running'
                if stopped:
                    app_module._process_stopped_results(self.sid, callback, results['problem'])
                else:
                    app_module._run_nsga2(self.sid, .5, .5, 1., 5, 10)
                self.assertEqual(self.state['opt_state']['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
