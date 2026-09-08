"""Regression tests for optimization, selectivity, and session state.

Run with: python -m unittest discover -s tests -v
"""

import unittest
import tempfile
from pathlib import Path

import pandas as pd
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from webapp.core.algorithm import (
    DrugLibraryProblem,
    build_smart_init,
    run_optimization,
    select_best_solution,
    save_results,
)
from webapp.core.selectivity import generate_selectivity_matrix
from webapp.core.state import make_session_state, reset_session_state


class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.matrix = np.array([[3., np.nan], [1., 4.], [np.nan, 2.]])
        self.prices = np.array([2., 3., 1.])

    def test_seeds_are_binary_reproducible_and_handle_missing_scores(self):
        seeds = build_smart_init(self.matrix, self.prices, pop_size=10)
        self.assertEqual(seeds.dtype, np.dtype(bool))
        np.testing.assert_array_equal(seeds[1], [True, True, False])
        np.testing.assert_array_equal(
            seeds, build_smart_init(self.matrix, self.prices, pop_size=10)
        )

    def test_initialization_preserves_global_random_state(self):
        before = np.random.get_state()
        build_smart_init(self.matrix, self.prices)
        after = np.random.get_state()
        self.assertEqual(before[0], after[0])
        np.testing.assert_array_equal(before[1], after[1])
        self.assertEqual(before[2:], after[2:])

    def test_small_population_has_clear_error(self):
        with self.assertRaisesRegex(ValueError, "at least 5"):
            build_smart_init(self.matrix, self.prices, pop_size=4)

    def test_zero_cost_and_uncovered_pool_have_finite_objectives(self):
        for matrix in (self.matrix, np.zeros_like(self.matrix)):
            with self.subTest(matrix=matrix):
                problem = DrugLibraryProblem(matrix, np.zeros(3))
                out = {}
                problem._evaluate(np.ones(3, dtype=bool), out)
                self.assertTrue(np.isfinite(out['F']).all())
                self.assertEqual(out['F'][1], 0.)

    def test_mutation_multiplier_controls_bit_probability(self):
        problem = DrugLibraryProblem(self.matrix, self.prices)
        # Accept integer seeds from callers, but pass binary data to pymoo.
        seeds = np.ones((5, 3), dtype=int)
        for multiplier, probability in ((0., 0.), (1., 1 / 3), (2., 2 / 3), (20., 1.)):
            with self.subTest(multiplier=multiplier), patch(
                'webapp.core.algorithm.minimize'
            ) as minimize:
                run_optimization(problem, seeds, mutation_multiplier=multiplier)
                algorithm = minimize.call_args.args[1]
                self.assertEqual(algorithm.mating.mutation.prob.value, 1.)
                self.assertEqual(algorithm.mating.mutation.prob_var.value, probability)
                self.assertEqual(algorithm.initialization.sampling.dtype, np.dtype(bool))
        for multiplier in (-1., np.nan, np.inf):
            with self.subTest(multiplier=multiplier), self.assertRaises(ValueError):
                run_optimization(problem, seeds, mutation_multiplier=multiplier)

    def test_real_optimization_returns_feasible_binary_solutions(self):
        problem = DrugLibraryProblem(self.matrix, self.prices)
        seeds = build_smart_init(self.matrix, self.prices, pop_size=5)
        result, elapsed = run_optimization(problem, seeds, pop_size=5, max_gen=5)
        self.assertIsNotNone(result.F)
        self.assertTrue(np.isfinite(result.F).all())
        self.assertTrue(np.isin(result.X, [0, 1]).all())
        self.assertTrue((result.G <= 0).all())
        self.assertGreaterEqual(elapsed, 0.)

    def test_export_preserves_selected_rows_with_duplicate_or_missing_smiles(self):
        for smiles in ([np.nan, np.nan], ['CC', 'CC']):
            with self.subTest(smiles=smiles), tempfile.TemporaryDirectory() as tmp:
                frame = pd.DataFrame({
                    'SMILES': smiles,
                    'Compound_Name': ['A', 'B'],
                    'Price_USD_per_mg': [10., 20.],
                    'Target': [2., 1.],
                }).set_index('SMILES')
                output = Path(tmp) / 'library.xlsx'
                _, selected, exported = save_results(
                    SimpleNamespace(X=np.array([[True, False]])), 0, frame, output
                )
                np.testing.assert_array_equal(selected, [0])
                self.assertEqual(exported['Compound_Name'].tolist(), ['A'])
                self.assertEqual(exported['Price_USD_per_mg'].sum(), 10.)
                self.assertEqual(pd.read_excel(output)['Compound_Name'].tolist(), ['A'])

    def test_solution_selection_converts_units_without_writing_a_plot(self):
        problem = DrugLibraryProblem(self.matrix, self.prices)
        objectives = np.array([[-.2, .1], [-.8, .3], [-1., 1.]])
        original = objectives.copy()
        with patch('pathlib.Path.mkdir') as mkdir:
            index, front = select_best_solution(SimpleNamespace(F=objectives), problem)
        mkdir.assert_not_called()
        self.assertEqual(index, 1)
        np.testing.assert_allclose(front[:, 0], -objectives[:, 0] * problem.pool_baseline_score)
        np.testing.assert_allclose(front[:, 1], objectives[:, 1] * problem.pool_total_cost)
        np.testing.assert_array_equal(objectives, original)
        for values in (None, np.empty((0, 2))):
            with self.assertRaises(ValueError):
                select_best_solution(SimpleNamespace(F=values), problem)


class SelectivityTests(unittest.TestCase):
    def test_integer_affinities_keep_fractional_scores(self):
        scores = generate_selectivity_matrix([[5, 6, 8]])
        np.testing.assert_allclose(scores, [[-2., -.5, 2.5]])

    def test_missing_data_and_target_subset(self):
        affinities = np.array([[5., 6., np.nan], [np.nan, 7., np.nan]])
        scores = generate_selectivity_matrix(affinities, target_indices=[1])
        np.testing.assert_allclose(scores[:, 1], [1., 0.])
        self.assertTrue(np.isnan(scores[:, [0, 2]]).all())

    def test_local_neighbors_match_direct_calculation(self):
        row = np.array([5., 5.3, 6.1, 7.8, 9.2, 10.7, 12.9])
        expected = []
        for index, value in enumerate(row):
            others = np.delete(row, index)
            nearest = others[np.argsort(np.abs(others - value))[:2]]
            expected.append(.5 * (value - others.mean()) + .5 * (value - nearest.mean()))
        np.testing.assert_allclose(generate_selectivity_matrix(row[None, :], h=2)[0], expected)

    def test_invalid_arguments(self):
        for h in (0, -1, 1.5, True):
            with self.subTest(h=h), self.assertRaises(ValueError):
                generate_selectivity_matrix([[5, 6]], h=h)
        for matrix in ([5, 6], [[5]]):
            with self.subTest(matrix=matrix), self.assertRaises(ValueError):
                generate_selectivity_matrix(matrix)


class SessionStateTests(unittest.TestCase):
    def test_sessions_do_not_share_mutable_defaults(self):
        first, second = make_session_state(), make_session_state()
        first['opt_state']['history'].append({'generation': 1})
        first['price_upload_state']['files']['price.csv'] = {}
        self.assertEqual(second['opt_state']['history'], [])
        self.assertEqual(second['price_upload_state']['files'], {})

    def test_reset_clears_custom_affinity_and_runtime_fields(self):
        state = make_session_state()
        dataset = state['dataset']
        dataset['has_custom_affinity'] = True
        dataset['ready'] = True
        dataset['temporary_field'] = 'stale'
        state['opt_state']['history'].append({'generation': 1})
        reset_session_state(state)
        self.assertIs(state['dataset'], dataset)
        expected = make_session_state()
        for key in expected:
            if key != 'last_activity':
                self.assertEqual(state[key], expected[key])


if __name__ == '__main__':
    unittest.main()
