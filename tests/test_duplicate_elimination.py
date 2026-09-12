"""Exact binary duplicate detection must preserve pymoo's search behavior."""

import contextlib
import io
import unittest
from unittest.mock import patch

import numpy as np
from pymoo.core.duplicate import DefaultDuplicateElimination
from pymoo.core.population import Population

from webapp.core.algorithm import (
    DrugLibraryProblem,
    PackedBinaryDuplicateElimination,
    build_smart_init,
    run_optimization,
)


class DuplicateEliminationTests(unittest.TestCase):
    def test_keeps_first_occurrence_and_original_order(self):
        population = Population.new(X=np.array([
            [True, False], [True, False], [False, True], [True, False],
        ]))
        surviving, kept, removed = PackedBinaryDuplicateElimination().do(
            population, return_indices=True,
        )
        self.assertEqual(kept, [0, 2])
        self.assertEqual(removed, [1, 3])
        self.assertIs(surviving[0], population[0])
        self.assertIs(surviving[1], population[2])

    def test_matches_default_with_external_populations_and_partial_bytes(self):
        rng = np.random.default_rng(831)
        for width in (1, 7, 8, 9, 17):
            values = rng.integers(0, 2, size=(21, width), dtype=bool)
            values[3] = values[0]
            values[12] = values[0]
            values[-1] = values[4]
            population = Population.new(X=values)
            first = Population.new(X=values[[1, 7]])
            second = Population.new(X=values[[2, 8]])
            empty = Population.create()
            for others in ((), (first,), (first, second), (empty,)):
                for to_itself in (True, False):
                    with self.subTest(width=width, others=len(others), to_itself=to_itself):
                        expected = DefaultDuplicateElimination().do(
                            population, *others, to_itself=to_itself, return_indices=True,
                        )
                        actual = PackedBinaryDuplicateElimination().do(
                            population, *others, to_itself=to_itself, return_indices=True,
                        )
                        self.assertEqual(actual[1:], expected[1:])
                        for actual_row, expected_row in zip(actual[0], expected[0]):
                            self.assertIs(actual_row, expected_row)
            np.testing.assert_array_equal(population.get('X'), values)

    def test_external_comparison_does_not_remove_internal_duplicates(self):
        population = Population.new(X=np.array([[True, False], [True, False]]))
        other = Population.new(X=np.array([[False, True]]))
        _, kept, removed = PackedBinaryDuplicateElimination().do(
            population, other, to_itself=False, return_indices=True,
        )
        self.assertEqual(kept, [0, 1])
        self.assertEqual(removed, [])

    def test_empty_population(self):
        population, kept, removed = PackedBinaryDuplicateElimination().do(
            Population.create(), return_indices=True,
        )
        self.assertEqual(len(population), 0)
        self.assertEqual(kept, [])
        self.assertEqual(removed, [])

    def test_seeded_optimization_matches_default_duplicate_detection(self):
        rng = np.random.default_rng(42)
        matrix = rng.normal(1., 2., size=(65, 8))
        matrix[rng.random(matrix.shape) < .7] = np.nan
        prices = rng.uniform(1., 100., size=len(matrix))
        problem = DrugLibraryProblem(matrix, prices, allowed_miss_pct=.25)
        seeds = build_smart_init(matrix, prices, pop_size=20, seed=4)
        with contextlib.redirect_stdout(io.StringIO()):
            actual, _ = run_optimization(problem, seeds, pop_size=20, max_gen=10, seed=4)
            with patch(
                'webapp.core.algorithm.PackedBinaryDuplicateElimination',
                DefaultDuplicateElimination,
            ):
                expected, _ = run_optimization(
                    problem, seeds, pop_size=20, max_gen=10, seed=4,
                )
        for name in ('X', 'F', 'G'):
            np.testing.assert_array_equal(getattr(actual, name), getattr(expected, name))
        self.assertEqual(actual.algorithm.evaluator.n_eval, expected.algorithm.evaluator.n_eval)
        self.assertEqual(actual.algorithm.n_gen, expected.algorithm.n_gen)


if __name__ == '__main__':
    unittest.main()
