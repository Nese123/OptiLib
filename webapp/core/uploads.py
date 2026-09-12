"""Shared CSV/Excel upload parsing with stable user-facing errors."""

import pandas as pd


class UploadTableError(ValueError):
    """An uploaded table has an unsupported format or cannot be parsed."""


def read_upload_table(file, safe_name, *, label=""):
    filename = safe_name.lower()
    if filename.endswith('.csv'):
        reader = pd.read_csv
    elif filename.endswith(('.xlsx', '.xls')):
        reader = pd.read_excel
    else:
        raise UploadTableError(f"Unsupported file type for {safe_name}. Use CSV or Excel (.xlsx).")
    try:
        return reader(file)
    except Exception as exc:
        raise UploadTableError(f"Failed to read {label}{safe_name}: {exc}") from exc
