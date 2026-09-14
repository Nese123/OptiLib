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
        import csv
        with open(csv_path, newline="") as source:
            reader = csv.reader(source)
            columns = next(reader)
            def rows():
                for row in reader:
                    yield [float(v) if column not in ('Compound_Name','Molecule_ChEMBL_ID','InChIKey','SMILES') and _numeric(v) else v for column,v in zip(columns,row)]
            write_rows_excel(columns, rows(), temporary)
        os.replace(temporary, destination)
        return str(destination)
    finally:
        temporary.unlink(missing_ok=True)


def write_rows_excel(columns, rows, destination):
    """Write rows directly: pandas writes by column and cannot stream XLSX."""
    import math
    import numbers
    import xlsxwriter
    with xlsxwriter.Workbook(str(destination), {
        'constant_memory': True, 'strings_to_formulas': False,
        'strings_to_urls': False, 'tmpdir': str(Path(destination).parent),
    }) as book:
        sheet = book.add_worksheet()
        sheet.write_row(0, 0, list(columns))
        for index, values in enumerate(rows, 1):
            sheet.write_row(index, 0, [
                None if value is None or (isinstance(value, numbers.Real) and not math.isfinite(value)) else value
                for value in values
            ])


def write_frame_excel(frame, destination):
    write_rows_excel(frame.columns, frame.itertuples(index=False, name=None), destination)


def _numeric(value):
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False
