"""Behavioral regressions for bounded evaluation, progress, uploads and lazy exports."""
import io
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

import numpy as np
import pandas as pd

import test_app
from test_app import app_module
from webapp.core.algorithm import DrugLibraryProblem, build_smart_init, run_optimization


class FitnessPreparationTests(unittest.TestCase):
    def test_prepared_and_bounded_fallback_match_with_missing_and_infinite_scores(self):
        for dtype in (np.float32, np.float64):
            matrix = np.array([[np.nan, -np.inf, np.inf, -2, 2],
                               [1, 0, 3, np.nan, 4], [2, -3, 5, -1, np.nan]], dtype=dtype)
            original = matrix.copy()
            prepared = DrugLibraryProblem(matrix, np.array([1., 2., 3.]))
            fallback = DrugLibraryProblem(matrix, np.array([1., 2., 3.]), prepared_score_budget=0)
            self.assertIsNone(fallback._scores)
            self.assertEqual(prepared._scores.nbytes, matrix.nbytes)
            for bits in range(8):
                x = np.array([bool(bits & (1 << i)) for i in range(3)])
                a, b = {}, {}
                with np.errstate(over='ignore', invalid='ignore'):
                    prepared._evaluate(x, a)
                    fallback._evaluate(x, b)
                for key in ('F', 'G'):
                    np.testing.assert_array_equal(a[key], b[key])
            np.testing.assert_array_equal(matrix, original)

    def test_seeded_search_and_evaluation_counts_match_fallback(self):
        rng = np.random.default_rng(91)
        matrix = rng.normal(1, 2, (60, 9))
        matrix[rng.random(matrix.shape) < .65] = np.nan
        prices = rng.uniform(1, 20, 60)
        seeds = build_smart_init(matrix, prices, pop_size=20)
        results = []
        for budget in (0, matrix.nbytes):
            problem = DrugLibraryProblem(matrix, prices, prepared_score_budget=budget)
            result, _ = run_optimization(problem, seeds, pop_size=20, max_gen=10)
            results.append(result)
        for key in ('X', 'F', 'G'):
            np.testing.assert_array_equal(getattr(results[0], key), getattr(results[1], key))
        self.assertEqual(results[0].algorithm.evaluator.n_eval, results[1].algorithm.evaluator.n_eval)


class MediumAppTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp

    def test_callback_copies_decisions_only_on_stop(self):
        values = {'X': np.eye(2, dtype=bool), 'F': np.array([[-1., 2.], [-2., 3.]]),
                  'G': np.array([[0.], [1.]])}
        pop = Mock()
        pop.get.side_effect = values.__getitem__
        algorithm = SimpleNamespace(pop=pop, n_gen=1)
        problem = SimpleNamespace(pool_baseline_score=2., pool_total_cost=10.)
        callback = app_module.WebappCallback(problem, self.state['opt_state'], self.state.lock)
        callback.notify(algorithm)
        self.assertNotIn(('X',), [call.args for call in pop.get.call_args_list])
        self.assertEqual(self.state['opt_state']['history'][0]['best_selectivity'], 2.)
        self.state['opt_state']['stop_requested'] = True
        with self.assertRaises(app_module.StopOptimization):
            callback.notify(algorithm)
        values['X'][:] = False
        np.testing.assert_array_equal(callback.last_pop_X, np.eye(2, dtype=bool))

    def test_history_cursor_and_run_revision(self):
        self.state['opt_state']['history'] = [{'generation': i} for i in range(1, 6)]
        self.state.run_revision = 3
        self.assertEqual(len(self.client.get('/api/status').json['history']), 5)
        response = self.client.get('/api/status?since_generation=3&run_revision=3').json
        self.assertEqual(response['history'], [{'generation': 4}, {'generation': 5}])
        self.assertEqual(response['run_revision'], 3)
        self.assertEqual(self.client.get('/api/status?since_generation=5').json['history'], [])
        self.assertEqual(len(self.client.get('/api/status?since_generation=99&run_revision=2').json['history']), 5)
        for value in ('nan', '1.5', '-2'):
            self.assertEqual(self.client.get(f'/api/status?since_generation={value}').status_code, 400)

    def test_multifile_targets_resolve_once_and_keep_per_file_summaries(self):
        resolved = {name: {'is_chembl': True, 'chembl_id': f'CHEMBL{i}', 'pref_name': name}
                    for i, name in enumerate(('A', 'B'))}
        with patch.object(app_module, '_resolve_targets', return_value=resolved) as resolver:
            response = self.client.post('/api/upload-targets', data={'files[]': [
                (io.BytesIO(b'Target\nA\n'), 'first.csv'),
                (io.BytesIO(b'Target\nB\n'), 'second.csv'),
            ]})
        self.assertEqual(response.status_code, 200)
        resolver.assert_called_once_with(['A', 'B'])
        files = response.json['uploaded_files']
        self.assertEqual([f['name'] for f in files], ['first.csv', 'second.csv'])
        self.assertEqual(files[0]['chembl_ids'], ['CHEMBL0'])
        self.assertEqual(files[1]['chembl_ids'], ['CHEMBL1'])

    def test_selection_uses_memory_and_download_exports_once(self):
        frame = pd.DataFrame({'SMILES': ['CC', 'CCC'], 'Compound_Name': ['A', 'B'],
                              'Price_USD_per_mg': [10., 20.], 'T': [2., -1.], 'U': [1., 3.]})
        app_module._publish_dataset(self.state, frame, 'unused.csv', False, 0, {})
        problem = app_module.DrugLibraryProblem(self.state['dataset']['selectivities'],
                                                self.state['dataset']['prices'])
        self.state['opt_results'].update(res_X=np.eye(2, dtype=bool),
                                         res_F=np.array([[-1., .3], [-.8, .6]]), problem=problem)
        with patch.object(app_module.pd, 'read_csv', side_effect=AssertionError('Selection read CSV')), \
                patch.object(app_module, '_get_target_info', side_effect=lambda t: (t, t)), \
                patch.object(app_module, 'write_library_excel', wraps=app_module.write_library_excel) as export:
            self.assertEqual(self.client.post('/api/select-solution', json={'index': 1}).status_code, 200)
            export.assert_not_called()
            self.assertEqual(self.state['opt_results']['winning_matrix_df']['Compound_Name'].tolist(), ['B'])
            self.assertNotIn('T', self.state['opt_results']['winning_matrix_df'])
            for _ in range(2):
                response = self.client.get('/api/download/library')
                self.assertEqual(response.status_code, 200)
                result = pd.read_excel(io.BytesIO(response.data))
                self.assertEqual(result['Compound_Name'].tolist(), ['B'])
                response.close()
            self.assertEqual(export.call_count, 1)


if __name__ == '__main__':
    unittest.main()
