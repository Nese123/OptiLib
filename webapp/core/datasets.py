"""Build immutable dataset snapshots before publishing session state."""

import numpy as np
from .records import METADATA_COLUMN_SET, target_columns


def prepare_dataset(frame, matrix_file, custom, provenance):
    meta = METADATA_COLUMN_SET
    targets = target_columns(frame)
    prices = frame["Price_USD_per_mg"].to_numpy(dtype=float)
    dataset = {
        "selectivities": frame[targets].to_numpy(dtype=float), "prices": prices,
        "num_drugs": len(frame),
        "num_targets": len(targets), "total_cost": float(np.sum(prices)),
        "matrix_file": matrix_file, "ready": True, "has_custom_affinity": custom,
        "provenance": dict(provenance),
        "matrix_metadata": frame[[c for c in frame.columns if c in meta]].copy(),
        "target_columns": targets,
    }
    return dataset
