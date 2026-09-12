"""Bounded MolPrice inference and per-molecule failure regressions."""

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

MOLPRICE_ROOT = Path(__file__).resolve().parents[1] / "MolPrice"
if str(MOLPRICE_ROOT) not in sys.path:
    sys.path.insert(0, str(MOLPRICE_ROOT))

from bin import numpy_predict


class FeatureExtractor:
    """Keep descriptor scaling independent of local model assets."""

    def __init__(self, path):
        pass

    def encode(self, smi):
        return np.zeros((1, 10), dtype=np.float32)

    def standardise_features(self, features):
        return features


class FakeRay:
    """Execute remote work at collection time and count outstanding molecules."""

    def __init__(self):
        self.pending = 0
        self.max_pending = 0
        self.fail_next_get = False
        self.initialized = False

    def remote(self, function):
        def submit(*args):
            future = SimpleNamespace(function=function, args=args, done=False)
            self.pending += len(args[0])
            self.max_pending = max(self.max_pending, self.pending)
            return future
        return SimpleNamespace(remote=submit)

    def is_initialized(self):
        return self.initialized

    def init(self, **kwargs):
        self.initialized = True

    def available_resources(self):
        return {"CPU": 0}

    def cancel(self, future):
        if not future.done:
            self.pending -= len(future.args[0])
            future.done = True

    def get(self, futures):
        if self.fail_next_get:
            self.fail_next_get = False
            raise RuntimeError("Ray worker unavailable")
        results = []
        for future in futures:
            try:
                results.append(future.function(*future.args))
            finally:
                self.cancel(future)
        return results


class NumpyPredictionTests(unittest.TestCase):
    def setUp(self):
        ray_disabled = patch.object(numpy_predict, "RAY_AVAILABLE", False)
        ray_disabled.start()
        self.addCleanup(ray_disabled.stop)
        self.model = self.make_model()

    def make_model(self, module=numpy_predict):
        model = module.NumpyFingerprints(FP_len=4)
        model.feature_gen = FeatureExtractor(None)
        model.nn_weights = [np.arange(140, dtype=np.float32).reshape(14, 10) / 140]
        model.nn_biases = [np.zeros(10, dtype=np.float32)]
        model.final_weight = np.ones((10, 1), dtype=np.float32)
        model.final_bias = np.zeros(1, dtype=np.float32)
        model._restored = True
        return model

    @staticmethod
    def numeric_fingerprint(smi):
        return np.full(14, float(smi), dtype=np.float32)

    def test_chunks_match_full_forward_and_preserve_shapes_and_order(self):
        smiles = ["7", "1", "5", "2", "8", "3", "4"]
        self.model.smi_to_fp = self.numeric_fingerprint
        expected_prices, expected_latent = self.model.forward(
            np.stack([self.numeric_fingerprint(smi) for smi in smiles])
        )
        with patch.object(self.model, "forward", wraps=self.model.forward) as forward:
            prices, latent = self.model.predict_batch_from_smiles(smiles, True, batch_size=3)
        self.assertEqual([len(call.args[0]) for call in forward.call_args_list], [3, 3, 1])
        self.assertEqual(prices.shape, (7, 1))
        self.assertEqual(latent.shape, (7, 10))
        self.assertEqual(prices.dtype, expected_prices.dtype)
        np.testing.assert_allclose(prices, expected_prices, rtol=1e-6)
        np.testing.assert_allclose(latent, expected_latent, rtol=1e-6)

    def test_default_bounds_feature_generation_before_each_forward(self):
        encoded = 0
        observed = []
        original_forward = self.model.forward

        def encode(smi):
            nonlocal encoded
            encoded += 1
            return self.numeric_fingerprint(smi)

        def forward(fps):
            observed.append((encoded, len(fps)))
            return original_forward(fps)

        self.model.smi_to_fp = encode
        self.model.forward = forward
        prices = self.model.predict_batch_from_smiles(["1"] * 513)
        self.assertEqual(observed, [(256, 256), (512, 256), (513, 1)])
        self.assertEqual(prices.shape, (513, 1))

    def test_invalid_smiles_raise_by_default(self):
        for invalid in ("", None, "not a smiles"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.model.predict_batch_from_smiles(["CC", invalid, "N"])

    def test_coercion_preserves_valid_rows_across_chunks(self):
        smiles = ["CC", "", "not a smiles", "O", None, "N"]
        prices, latent = self.model.predict_batch_from_smiles(
            smiles, True, batch_size=2, errors="coerce"
        )
        expected_prices, expected_latent = self.model.predict_batch_from_smiles(["CC", "O", "N"], True)
        np.testing.assert_allclose(prices[[0, 3, 5]], expected_prices, rtol=1e-6)
        np.testing.assert_allclose(latent[[0, 3, 5]], expected_latent, rtol=1e-6)
        self.assertTrue(np.isnan(prices[[1, 2, 4]]).all())
        self.assertTrue(np.isnan(latent[[1, 2, 4]]).all())
        with patch.object(self.model, "forward") as forward:
            failed = self.model.predict_batch_from_smiles(["", None], errors="coerce")
        forward.assert_not_called()
        self.assertTrue(np.isnan(failed).all())

    def test_failed_forward_retries_singletons_only_when_coercing(self):
        self.model.smi_to_fp = self.numeric_fingerprint
        original_forward = self.model.forward

        def forward(fps):
            if len(fps) > 1 or fps[0, 0] == 3:
                raise RuntimeError("Cannot predict these rows")
            return original_forward(fps)

        with patch.object(self.model, "forward", side_effect=forward) as mocked:
            with self.assertRaisesRegex(RuntimeError, "Cannot predict"):
                self.model.predict_batch_from_smiles(["1", "2", "3", "4"])
            self.assertEqual(mocked.call_count, 1)
            mocked.reset_mock()
            prices, latent = self.model.predict_batch_from_smiles(
                ["1", "2", "3", "4"], True, errors="coerce"
            )
            self.assertEqual([len(call.args[0]) for call in mocked.call_args_list], [4, 1, 1, 1, 1])
        self.assertTrue(np.isnan(prices[2]).all())
        self.assertTrue(np.isnan(latent[2]).all())
        expected, _ = original_forward(np.stack([self.numeric_fingerprint(smi) for smi in ("1", "2", "4")]))
        np.testing.assert_allclose(prices[[0, 1, 3]], expected, rtol=1e-6)

    def test_parameters_are_validated_before_encoding(self):
        with patch.object(self.model, "smi_to_fp") as encode:
            for batch_size in (0, -1, 1.5, True, "2"):
                with self.subTest(batch_size=batch_size), self.assertRaisesRegex(ValueError, "batch_size"):
                    self.model.predict_batch_from_smiles(["CC"], batch_size=batch_size)
            with self.assertRaisesRegex(ValueError, "errors"):
                self.model.predict_batch_from_smiles(["CC"], errors="ignore")
            with self.assertRaisesRegex(ValueError, "empty"):
                self.model.predict_batch_from_smiles([])
            with self.assertRaises(TypeError):
                self.model.predict_batch_from_smiles(["CC"], False, 2)
        encode.assert_not_called()

    def load_ray_module(self, ray):
        spec = importlib.util.spec_from_file_location("numpy_predict_ray_test", MOLPRICE_ROOT / "bin/numpy_predict.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"ray": ray}):
            spec.loader.exec_module(module)
        return module

    def test_ray_has_same_error_semantics_and_bounds_pending_features(self):
        ray = FakeRay()
        module = self.load_ray_module(ray)
        model = self.make_model(module)
        smiles = ["CC", "N", "not a smiles", "O"] * 26
        expected_prices, expected_latent = self.model.predict_batch_from_smiles(smiles, True, errors="coerce")
        with patch("src.model_utils.MolFeatureExtractor", FeatureExtractor):
            prices, latent = model.predict_batch_from_smiles(smiles, True, batch_size=16, errors="coerce")
            with self.assertRaisesRegex(ValueError, "Invalid SMILES"):
                model.predict_batch_from_smiles(smiles, batch_size=16)
        np.testing.assert_allclose(prices, expected_prices, rtol=1e-6, equal_nan=True)
        np.testing.assert_allclose(latent, expected_latent, rtol=1e-6, equal_nan=True)
        self.assertLessEqual(ray.max_pending, 16)
        self.assertEqual(ray.pending, 0)

    def test_ray_worker_failure_coerces_with_local_fallback(self):
        ray = FakeRay()
        ray.fail_next_get = True
        model = self.make_model(self.load_ray_module(ray))
        with patch("src.model_utils.MolFeatureExtractor", FeatureExtractor):
            prices = model.predict_batch_from_smiles(["CC"] * 101, batch_size=16, errors="coerce")
        self.assertTrue(np.isfinite(prices).all())
        self.assertLessEqual(ray.max_pending, 16)
        self.assertEqual(ray.pending, 0)


if __name__ == "__main__":
    unittest.main()
