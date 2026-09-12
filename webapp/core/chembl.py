"""Read scoring provenance without requiring migration of legacy databases."""

import json


def get_selectivity_provenance(conn):
    """Return active scoring metadata; an unversioned table remains legacy."""
    legacy = {
        "scoring_version": "legacy_argpartition_v1",
        "build_id": "legacy-unversioned",
        "h": 5,
        "storage_decimals": 4,
    }
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='optilib_selectivity_metadata'"
    ).fetchone()
    if not exists:
        return legacy
    row = conn.execute(
        "SELECT value FROM optilib_selectivity_metadata WHERE key='provenance'"
    ).fetchone()
    if row is None:
        return legacy
    provenance = json.loads(row[0])
    if not isinstance(provenance, dict) or not {
        "scoring_version", "build_id", "h", "storage_decimals",
    }.issubset(provenance):
        raise ValueError("Invalid ChEMBL selectivity provenance")
    return provenance
