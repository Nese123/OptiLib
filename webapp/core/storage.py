"""Atomic publication of session matrix and spreadsheet exports."""

import hashlib
import os
import tempfile
from pathlib import Path

import pandas as pd


def publish_matrix(frame, output_dir, *, filename=None, scoring_version=""):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=output_dir, prefix=".matrix-", suffix=".csv")
    os.close(fd)
    temporary = Path(name)
    try:
        frame.to_csv(temporary, index=False)
        if filename is None:
            digest = hashlib.sha256(scoring_version.encode("utf-8") + b"\0")
            with temporary.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            filename = f"selectivity_matrix_affinity_{digest.hexdigest()}.csv"
        destination = output_dir / filename
        os.replace(temporary, destination)
        return str(destination)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_matrix_excel(csv_path):
    """Call under the session export lock; CSV paths identify immutable content."""
    destination = Path(csv_path).with_suffix(".xlsx")
    if destination.is_file():
        return str(destination)
    fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".excel-", suffix=".xlsx")
    os.close(fd)
    temporary = Path(name)
    try:
        pd.read_csv(csv_path).to_excel(temporary, index=False, engine="xlsxwriter")
        os.replace(temporary, destination)
        return str(destination)
    finally:
        temporary.unlink(missing_ok=True)
