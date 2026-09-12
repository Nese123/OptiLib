"""Custom-affinity price precedence and bounded MolPrice integration."""

import importlib
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

# Match the route-test import guard: no database, output cleanup, or daemon work.
with patch("sqlite3.connect"), patch("shutil.rmtree"), patch.object(Path, "unlink"), \
        patch("threading.Thread.start"), patch("atexit.register"):
    app_module = importlib.import_module("webapp.app")


def molecule(name, inchikey="", smiles=""):
    return {"Compound_Name": name, "InChIKey": inchikey, "SMILES": smiles}


class AffinityPriceTests(unittest.TestCase):
    def test_source_precedence_and_partial_predictions_preserve_input_order(self):
        records = [
            molecule("custom", "CUSTOM", "C"), molecule("direct", "DIRECT", "CC"),
            molecule("cached", "CACHED", "CCC"), molecule("predicted A", "MISS1", "CO"),
            molecule("invalid", "MISS2", "invalid"), molecule("no structure", "NOSMI", "Missing_SMILES"),
            molecule("predicted B", "MISS3", "CN"), molecule("duplicate key", "DIRECT", "CC"),
        ]
        compounds = {"custom": {"chembl_id": "CHEMBL_CUSTOM"}}
        state = {"price_map": {"CHEMBL_CUSTOM": 12.}}
        with patch.object(app_module, "_lookup_molport_prices", return_value=(
            {"CUSTOM": 999., "DIRECT": 20., "CACHED": 30.},
            {"CUSTOM": "vendor", "DIRECT": "vendor", "CACHED": "MolPrice"},
        )) as lookup, patch.object(app_module, "NumpyFingerprints") as constructor:
            model = constructor.return_value
            model.predict_batch_from_smiles.return_value = np.array([[40.], [np.nan], [60.]])
            prices, counts = app_module._resolve_affinity_prices(records, compounds, state)
        np.testing.assert_array_equal(prices, [12., 20., 30., 40., 25., 25., 60., 20.])
        self.assertEqual(prices.shape, (len(records),))
        self.assertEqual(counts, {"custom": 1, "molport": 2, "molprice": 3, "fallback": 2})
        lookup.assert_called_once_with(["DIRECT", "CACHED", "MISS1", "MISS2", "NOSMI", "MISS3"])
        constructor.assert_called_once()
        model.predict_batch_from_smiles.assert_called_once_with(
            ["CO", "invalid", "CN"], batch_size=256, errors="coerce"
        )

    def test_model_failure_or_wrong_result_count_falls_back_without_losing_known_prices(self):
        records = [molecule("custom"), molecule("vendor", "KNOWN"),
                   molecule("missing A", smiles="CC"), molecule("missing B", smiles="CCC")]
        failures = [RuntimeError("prediction failed"), np.array([[80.]])]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(app_module, "_lookup_molport_prices", return_value=({"KNOWN": 30.}, {})), \
                    patch.object(app_module, "NumpyFingerprints") as constructor, \
                    self.assertLogs("optilib", level="WARNING"):
                predict = constructor.return_value.predict_batch_from_smiles
                if isinstance(failure, Exception):
                    predict.side_effect = failure
                else:
                    predict.return_value = failure
                prices, counts = app_module._resolve_affinity_prices(records, {}, {"price_map": {"custom": 10.}})
            np.testing.assert_array_equal(prices, [10., 30., 20., 20.])
            self.assertEqual(counts, {"custom": 1, "molport": 1, "molprice": 0, "fallback": 2})

    def test_nonfinite_model_values_and_missing_structures_use_default_fallback(self):
        records = [molecule("bad A", smiles="invalid"), molecule("bad B", smiles="CC"),
                   molecule("missing", smiles="Missing_SMILES"), molecule("empty", smiles="")]
        with patch.object(app_module, "_lookup_molport_prices", return_value=({}, {})), \
                patch.object(app_module, "NumpyFingerprints") as constructor:
            constructor.return_value.predict_batch_from_smiles.return_value = np.array([[np.nan], [np.inf]])
            prices, counts = app_module._resolve_affinity_prices(records, {}, {"price_map": {}})
        np.testing.assert_array_equal(prices, [100.] * 4)
        self.assertEqual(counts, {"custom": 0, "molport": 0, "molprice": 0, "fallback": 4})

    def test_nonfinite_source_prices_continue_to_the_next_source(self):
        records = [molecule("custom", "DIRECT", "CC"), molecule("vendor", "NONFINITE", "CCC")]
        with patch.object(app_module, "_lookup_molport_prices", return_value=({"DIRECT": 20., "NONFINITE": np.inf}, {})), \
                patch.object(app_module, "NumpyFingerprints") as constructor:
            constructor.return_value.predict_batch_from_smiles.return_value = np.array([[40.]])
            prices, counts = app_module._resolve_affinity_prices(records, {}, {"price_map": {"custom": np.nan}})
        np.testing.assert_array_equal(prices, [20., 40.])
        self.assertEqual(counts, {"custom": 0, "molport": 1, "molprice": 1, "fallback": 0})

    def test_model_is_not_loaded_when_all_rows_are_resolved_or_input_is_empty(self):
        for records in ([molecule("custom")], []):
            with self.subTest(records=records), \
                    patch.object(app_module, "_lookup_molport_prices", return_value=({}, {})), \
                    patch.object(app_module, "NumpyFingerprints") as constructor:
                prices, counts = app_module._resolve_affinity_prices(records, {}, {"price_map": {"custom": 15.}})
            constructor.assert_not_called()
            self.assertEqual(prices.shape, (len(records),))
            self.assertEqual(counts, {"custom": len(records), "molport": 0, "molprice": 0, "fallback": 0})

    def test_real_batch_adapter_salvages_singletons_after_forward_failure(self):
        # Exercise the actual bounded predictor, with deterministic lightweight
        # encodings and a network that fails for one molecule and multirow batches.
        model = app_module.NumpyFingerprints(FP_len=4)
        model._restored = True
        model.final_weight = np.ones((10, 1), dtype=np.float32)
        model.final_bias = np.zeros(1, dtype=np.float32)
        model.smi_to_fp = lambda smi: np.full(14, float(smi), dtype=np.float32)
        calls = []

        def forward(fps):
            calls.append(len(fps))
            if len(fps) > 1 or fps[0, 0] == 2:
                raise RuntimeError("Molecule cannot be predicted")
            return fps[:, :1] * 10, np.repeat(fps[:, :1], 10, axis=1)

        model.forward = forward
        records = [molecule("custom"), molecule("first", smiles="1"),
                   molecule("broken", smiles="2"), molecule("last", smiles="3")]
        with patch.object(app_module, "_lookup_molport_prices", return_value=({}, {})), \
                patch.object(app_module, "NumpyFingerprints", return_value=model):
            prices, counts = app_module._resolve_affinity_prices(records, {}, {"price_map": {"custom": 40.}})
        np.testing.assert_array_equal(prices, [40., 10., 30., 30.])
        self.assertEqual(calls, [3, 1, 1, 1])
        self.assertEqual(counts, {"custom": 1, "molport": 0, "molprice": 2, "fallback": 1})

    def test_target_cache_key_tracks_price_scoring_and_build_changes(self):
        provenance = {"scoring_version": "v1", "build_id": "build-1"}
        args = (["CHEMBL2", "CHEMBL1"], .5, True, 2)
        baseline = app_module._matrix_cache_key(*args, {"a": 10.}, provenance)
        reordered = app_module._matrix_cache_key(["CHEMBL1", "CHEMBL2"], .5, True, 2, {"a": 10.}, provenance)
        variants = [
            app_module._matrix_cache_key(*args, {"a": 20.}, provenance),
            app_module._matrix_cache_key(*args, {"a": 10.}, {**provenance, "scoring_version": "v2"}),
            app_module._matrix_cache_key(*args, {"a": 10.}, {**provenance, "build_id": "build-2"}),
        ]
        self.assertEqual(baseline, reordered)
        self.assertEqual(len({baseline, *variants}), 4)


if __name__ == "__main__":
    unittest.main()
