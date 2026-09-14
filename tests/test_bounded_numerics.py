"""Block boundaries must preserve the established seed and fitness definitions."""
import unittest
from unittest.mock import patch
import numpy as np
from webapp.core import algorithm


class BoundedNumericsTests(unittest.TestCase):
    def test_seeds_match_column_reference_across_boundaries_and_ties(self):
        rng=np.random.default_rng(123)
        matrix=rng.integers(-2,5,(57,9)).astype(float)
        matrix[rng.random(matrix.shape)<.3]=np.nan
        matrix[:,0]=np.nan
        prices=rng.integers(0,5,57).astype(float)
        expected=np.zeros((5,57),dtype=bool)
        for j in range(matrix.shape[1]):
            covered=np.flatnonzero(matrix[:,j]>0)
            if not len(covered):continue
            costs=sorted(covered,key=lambda i:(prices[i],i))
            expected[0,costs[0]]=True
            if len(costs)>1:expected[4,costs[1]]=True
            expected[1,max(covered,key=lambda i:(matrix[i,j],-i))]=True
            expected[2,max(covered,key=lambda i:(matrix[i,j]/max(prices[i],1e-6),-i))]=True
        expected[3]=expected[0]|expected[1]
        blocks=algorithm.matrix_blocks
        with patch.object(algorithm,'matrix_blocks',side_effect=lambda matrix:blocks(matrix,512)):
            actual=algorithm.build_smart_init(matrix,prices,10,seed=1)
        np.testing.assert_array_equal(actual[:5],expected)
        reference=np.random.default_rng(1).integers(0,2,(10,57),dtype=bool)
        np.testing.assert_array_equal(actual[5:],reference[5:])

    def test_bounded_fitness_matches_direct_reference(self):
        rng=np.random.default_rng(42)
        matrix=rng.normal(size=(321,13));matrix[rng.random(matrix.shape)<.5]=np.nan
        prices=rng.uniform(1,10,321)
        problem=algorithm.DrugLibraryProblem(matrix,prices,weight_mean=.4,allowed_miss_pct=.2,prepared_score_budget=0)
        blocks=algorithm.matrix_blocks
        for _ in range(10):
            chosen=rng.random(321)<.4
            target_max=np.maximum(np.nan_to_num(np.fmax.reduce(matrix[chosen],axis=0),nan=-1.),0)
            positive=target_max[target_max>0]
            expected=[-(.4*target_max.mean()+.6*positive.min())/problem.pool_baseline_score,prices[chosen].sum()/prices.sum()]
            result={}
            with patch.object(algorithm,'matrix_blocks',side_effect=lambda matrix:blocks(matrix,1024)):
                problem._evaluate(chosen,result)
            np.testing.assert_array_equal(result['F'],expected)
            self.assertEqual(result['G'],[int((target_max<=0).sum())-problem.max_allowed_misses])
