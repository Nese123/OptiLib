#!/usr/bin/env python3
"""Rebuild ChEMBL selectivity with disk staging and reversible publication.

Source selection, inactivity imputation, parent grouping, exact medians and
max-median > 5 filtering are unchanged. Only cutoff ties use the shared v2 rule.
The active table remains available until validation and an atomic table swap.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
from itertools import groupby
import json
import logging
from pathlib import Path
import re
import resource
import sqlite3
import sys
import time
import uuid

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from webapp.core.chembl import get_selectivity_provenance
from webapp.core.selectivity import (
    SELECTIVITY_SCORING_VERSION,
    score_measured_affinities,
)

logger = logging.getLogger("build_selectivity")
ACTIVE_TABLE = "compound_target_selectivity"
ACTIVITY_QUERY = """
    SELECT COALESCE(mh.parent_molregno, act.molregno) AS molregno,
           qa.tid, act.pchembl_value, act.activity_comment,
           act.standard_relation, act.standard_value, act.standard_units
    FROM temp_qualified_assays qa
    JOIN activities act ON qa.assay_id = act.assay_id
    LEFT JOIN molecule_hierarchy mh ON act.molregno = mh.molregno
    WHERE act.pchembl_value IS NOT NULL
       OR act.activity_comment IS NOT NULL
       OR act.standard_relation IN ('>', '>=')
"""
MEDIAN_QUERY = """
    WITH ranked AS (
        SELECT molregno, tid, pvalue,
               ROW_NUMBER() OVER pair AS position,
               COUNT(*) OVER pair AS n
        FROM raw_activity
        WINDOW pair AS (
            PARTITION BY molregno, tid ORDER BY pvalue
            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
        )
    )
    SELECT molregno, tid, AVG(pvalue)
    FROM ranked
    WHERE position IN ((n + 1) / 2, (n + 2) / 2)
    GROUP BY molregno, tid
"""


def _identifier(name):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
        raise ValueError("Invalid generated table identifier")
    return '"' + name + '"'


def _configure(conn, cache_mib=128):
    conn.execute(f"PRAGMA cache_size = {-cache_mib * 1024}")
    conn.execute("PRAGMA temp_store = FILE")
    conn.execute("PRAGMA mmap_size = 0")
    conn.execute("PRAGMA busy_timeout = 120000")


def _exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone() is not None


def compute_blended_selectivity_fast(pvals, group_starts, h=5):
    """Compatibility wrapper over the same row scorer used by uploads."""
    pvals = np.asarray(pvals, dtype=float)
    scores = np.empty(len(pvals), dtype=float)
    for start, end in zip(group_starts[:-1], group_starts[1:]):
        scores[start:end] = score_measured_affinities(pvals[start:end], h=h)
    return scores


def impute_activity_chunk(frame):
    """Preserve the original row-local imputation and pandas grouping rules."""
    pchembl = frame["pchembl_value"].to_numpy(dtype=np.float64, copy=True)
    missing = np.isnan(pchembl)
    comments = frame["activity_comment"].astype(str).str.lower()
    inactive = (
        comments.str.contains("inactive", na=False)
        | comments.str.contains("not active", na=False)
        | comments.str.contains("no activity", na=False)
        | comments.str.contains("no inhibition", na=False)
        | comments.str.contains("non-active", na=False)
        | comments.str.contains("inhibition < 50%", na=False)
        | comments.str.contains("inhibition <50%", na=False)
    )
    relations = frame["standard_relation"].astype(str)
    values = pd.to_numeric(frame["standard_value"], errors="coerce").to_numpy(dtype=np.float64)
    units = frame["standard_units"].astype(str).str.strip().str.lower()
    values_nm = np.full_like(values, np.nan)
    for names, factor in (
        (["nm", "nmol/l", "nmol.l-1"], 1.0),
        (["um", "µm", "umol/l", "umol.l-1", "um/ml", "umol"], 1000.0),
        (["mm", "mmol/l", "mmol.l-1"], 1000000.0),
        (["m", "mol/l", "mol.l-1"], 1000000000.0),
    ):
        mask = units.isin(names)
        values_nm[mask] = values[mask] * factor
    imputed = missing & (inactive | (relations.isin([">", ">="]) & (values_nm >= 10000.0)))
    pchembl[imputed] = 5.0
    valid = ~np.isnan(pchembl) & frame["molregno"].notna() & frame["tid"].notna()
    result = frame.loc[valid, ["molregno", "tid"]].copy()
    result["pvalue"] = pchembl[valid]
    return result, int(imputed.sum())


def stage_affinities(source, staging, batch_size=25000):
    """Stream source rows to disk, then compute exact per-target medians."""
    staging.execute("CREATE TABLE raw_activity (molregno INTEGER NOT NULL, tid INTEGER NOT NULL, pvalue REAL NOT NULL)")
    staging.commit()
    source.execute("CREATE TEMP TABLE temp_qualified_assays (assay_id INTEGER PRIMARY KEY, tid INTEGER NOT NULL)")
    source.execute("""
        INSERT INTO temp_qualified_assays
        SELECT a.assay_id, a.tid FROM assays a
        JOIN target_dictionary td ON td.tid = a.tid
        WHERE td.target_type='SINGLE PROTEIN' AND td.organism='Homo sapiens'
          AND a.confidence_score IN (8, 9)
    """)
    logger.info("Qualified assays: %s", source.execute("SELECT COUNT(*) FROM temp_qualified_assays").fetchone()[0])
    candidates = valid_rows = imputed_rows = 0
    try:
        for frame in pd.read_sql_query(ACTIVITY_QUERY, source, chunksize=batch_size):
            valid, imputed = impute_activity_chunk(frame)
            staging.executemany(
                "INSERT INTO raw_activity VALUES (?, ?, ?)",
                ((int(m), int(t), float(p)) for m, t, p in valid.itertuples(index=False, name=None)),
            )
            staging.commit()
            candidates += len(frame)
            valid_rows += len(valid)
            imputed_rows += imputed
            if candidates % (batch_size * 10) == 0:
                logger.info("Extracted %s candidates; %s valid; %s imputed; peak RSS %.1f MiB", candidates, valid_rows, imputed_rows, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
    finally:
        source.execute("DROP TABLE temp_qualified_assays")
    logger.info("Extraction complete: %s candidates, %s valid, %s imputed", candidates, valid_rows, imputed_rows)
    logger.info("Building disk-backed affinity ordering index")
    staging.execute("CREATE INDEX raw_activity_order ON raw_activity (molregno, tid, pvalue)")
    staging.commit()
    logger.info("Aggregating exact medians on disk")
    staging.execute("""
        CREATE TABLE median_affinity (
            molregno INTEGER NOT NULL, tid INTEGER NOT NULL, pvalue REAL NOT NULL,
            PRIMARY KEY (molregno, tid)
        ) WITHOUT ROWID
    """)
    staging.execute("INSERT INTO median_affinity " + MEDIAN_QUERY)
    staging.commit()
    count = staging.execute("SELECT COUNT(*) FROM median_affinity").fetchone()[0]
    logger.info("Median aggregation complete: %s compound-target pairs", count)
    return {"candidate_rows": candidates, "valid_activity_rows": valid_rows,
            "imputed_rows": imputed_rows, "median_rows": count}


def _log_query_plans(conn, label):
    for name, query in (
        ("target assays", "SELECT assay_id FROM assays WHERE tid=? AND confidence_score IN (8,9)"),
        ("active activities", "SELECT molregno FROM activities WHERE assay_id=? AND pchembl_value>5.0"),
    ):
        plan = conn.execute("EXPLAIN QUERY PLAN " + query, (1,)).fetchall()
        logger.info("%s query plan (%s): %s", label, name, plan)


def create_query_indexes(conn):
    logger.info("Building target-assay covering index")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_optilib_assays_target_confidence ON assays (tid, confidence_score, assay_id)")
    conn.commit()
    logger.info("Building partial active-activity covering index")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_optilib_activities_active_assay_molregno ON activities (assay_id, molregno) WHERE pchembl_value > 5.0")
    conn.commit()
    conn.execute("ANALYZE idx_optilib_assays_target_confidence")
    conn.execute("ANALYZE idx_optilib_activities_active_assay_molregno")
    conn.commit()
    _log_query_plans(conn, "After indexes")


def populate_shadow(conn, staging, table_name, batch_size):
    table = _identifier(table_name)
    conn.execute(f"CREATE TABLE {table} (molregno INTEGER NOT NULL, tid INTEGER NOT NULL, selectivity_score REAL NOT NULL, PRIMARY KEY (molregno, tid))")
    conn.commit()
    insert = f"INSERT INTO {table} VALUES (?, ?, ?)"
    cursor = staging.execute("SELECT molregno, tid, pvalue FROM median_affinity ORDER BY molregno, tid")
    batch = []
    row_count = compound_count = 0
    for molregno, rows in groupby(cursor, key=lambda row: row[0]):
        measured = list(rows)  # At most one entry per qualified target.
        values = np.array([row[2] for row in measured], dtype=float)
        if values.max() <= 5.0:
            continue
        scores = score_measured_affinities(values)
        if not np.isfinite(scores).all():
            raise ValueError(f"Non-finite selectivity for compound {molregno}")
        compound_count += 1
        for row, score in zip(measured, scores):
            batch.append((int(molregno), int(row[1]), round(float(score), 4)))
            if len(batch) >= batch_size:
                conn.executemany(insert, batch)
                conn.commit()
                row_count += len(batch)
                batch.clear()
                if row_count % (batch_size * 10) == 0:
                    logger.info("Scored and inserted %s rows", row_count)
    if batch:
        conn.executemany(insert, batch)
        conn.commit()
        row_count += len(batch)
    logger.info("Scoring complete: %s rows across %s compounds", row_count, compound_count)
    suffix = table_name.rsplit("_", 1)[-1]
    conn.execute(f"CREATE INDEX {_identifier('idx_cts_tid_' + suffix)} ON {table} (tid)")
    conn.execute(f"CREATE INDEX {_identifier('idx_cts_score_' + suffix)} ON {table} (selectivity_score)")
    conn.execute(f"ANALYZE {table}")
    conn.commit()
    return row_count, compound_count


def validate_shadow(conn, table_name, row_count, expected_row_count=None):
    table = _identifier(table_name)
    actual = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if actual != row_count or actual == 0:
        raise ValueError(f"Invalid rebuilt row count: {actual}; expected {row_count}")
    if expected_row_count is not None and actual != expected_row_count:
        raise ValueError(f"Rebuilt row count {actual} differs from expected {expected_row_count}")
    if _exists(conn, ACTIVE_TABLE):
        old_count = conn.execute(f"SELECT COUNT(*) FROM {ACTIVE_TABLE}").fetchone()[0]
        if old_count != actual:
            raise ValueError(f"Row count changed: previous {old_count}, rebuilt {actual}")
        for left, right in ((ACTIVE_TABLE, table_name), (table_name, ACTIVE_TABLE)):
            mismatch = conn.execute(f"""
                SELECT l.molregno, l.tid FROM {_identifier(left)} l
                LEFT JOIN {_identifier(right)} r
                  ON r.molregno=l.molregno AND r.tid=l.tid
                WHERE r.molregno IS NULL LIMIT 1
            """).fetchone()
            if mismatch is not None:
                raise ValueError(f"Rebuilt compound-target keys differ: {mismatch}")
        logger.info("Validated exact equality of all %s existing compound-target keys", actual)
        changed = conn.execute(f"""
            SELECT COUNT(*), MAX(ABS(n.selectivity_score-o.selectivity_score))
            FROM {table} n JOIN {ACTIVE_TABLE} o USING (molregno, tid)
            WHERE n.selectivity_score != o.selectivity_score
        """).fetchone()
        logger.info("Changed scores: %s; maximum absolute change: %s", *changed)


def _metadata_schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS optilib_selectivity_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS optilib_selectivity_backups (table_name TEXT PRIMARY KEY, provenance_json TEXT NOT NULL, created_at TEXT NOT NULL)")


def publish_shadow(conn, table_name, provenance):
    """Swap the validated table and matching metadata in one transaction."""
    table = _identifier(table_name)
    backup_name = "compound_target_selectivity_backup_" + uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        previous = get_selectivity_provenance(conn)
        _metadata_schema(conn)
        if _exists(conn, ACTIVE_TABLE):
            conn.execute(f"ALTER TABLE {ACTIVE_TABLE} RENAME TO {_identifier(backup_name)}")
            conn.execute("INSERT INTO optilib_selectivity_backups VALUES (?, ?, ?)",
                         (backup_name, json.dumps(previous), now))
            provenance = {**provenance, "previous_table": backup_name}
        conn.execute(f"ALTER TABLE {table} RENAME TO {ACTIVE_TABLE}")
        conn.execute("INSERT OR REPLACE INTO optilib_selectivity_metadata VALUES ('provenance', ?)",
                     (json.dumps(provenance, sort_keys=True),))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    logger.info("Published build %s; rollback table: %s", provenance["build_id"], provenance.get("previous_table"))
    return provenance


def rollback_selectivity(db_path, backup_table=None):
    """Restore a retained table and provenance; retain the replaced build too."""
    with closing(sqlite3.connect(str(db_path), timeout=120.0)) as conn:
        _configure(conn)
        current = get_selectivity_provenance(conn)
        backup_table = backup_table or current.get("previous_table")
        if not backup_table or not _exists(conn, "optilib_selectivity_backups"):
            raise ValueError("No retained selectivity build to restore")
        record = conn.execute("SELECT provenance_json FROM optilib_selectivity_backups WHERE table_name=?", (backup_table,)).fetchone()
        if record is None or not _exists(conn, backup_table):
            raise ValueError("Unknown retained selectivity table")
        result = publish_shadow(conn, backup_table, json.loads(record[0]))
        conn.execute("DELETE FROM optilib_selectivity_backups WHERE table_name=?", (backup_table,))
        conn.commit()
        return result


def rebuild_selectivity(db_path, staging_path=None, batch_size=25000, expected_row_count=None):
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    build_id = uuid.uuid4().hex
    staging_path = Path(staging_path or db_path.parent / f"selectivity_staging_{build_id}.sqlite").resolve()
    if staging_path.exists():
        raise FileExistsError(f"Staging path already exists: {staging_path}")
    started = time.monotonic()
    shadow = "compound_target_selectivity_build_" + build_id[:12]
    logger.info("Staging database: %s", staging_path)
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as source:
        _configure(source, 64)
        old_count = source.execute(f"SELECT COUNT(*) FROM {ACTIVE_TABLE}").fetchone()[0] if _exists(source, ACTIVE_TABLE) else None
        logger.info("Baseline derived row count: %s; provenance: %s", old_count, get_selectivity_provenance(source))
        if expected_row_count is not None and old_count is not None and old_count != expected_row_count:
            raise ValueError(f"Baseline row count {old_count} differs from expected {expected_row_count}")
        _log_query_plans(source, "Before indexes")
        source_version = source.execute("SELECT name FROM version ORDER BY name").fetchall() if _exists(source, "version") else []
    # Build the target index before qualification. Without it, SQLite can
    # choose a full assays-table scan for every qualifying target.
    with closing(sqlite3.connect(str(db_path), timeout=120.0)) as target:
        _configure(target)
        create_query_indexes(target)
    with closing(sqlite3.connect(str(staging_path))) as staging:
        _configure(staging)
        staging.execute("PRAGMA synchronous=NORMAL")
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as source:
            _configure(source, 64)
            extraction = stage_affinities(source, staging, batch_size)
        with closing(sqlite3.connect(str(db_path), timeout=120.0)) as target:
            _configure(target)
            row_count, compound_count = populate_shadow(target, staging, shadow, batch_size)
            validate_shadow(target, shadow, row_count, expected_row_count)
            provenance = {
                "scoring_version": SELECTIVITY_SCORING_VERSION,
                "build_id": build_id,
                "h": 5,
                "storage_decimals": 4,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_version": [row[0] for row in source_version],
                "row_count": row_count,
                "compound_count": compound_count,
                **extraction,
            }
            provenance = publish_shadow(target, shadow, provenance)
    logger.info("Migration complete in %.1f seconds; peak RSS %.1f MiB; staging retained at %s", time.monotonic() - started, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, staging_path)
    return provenance


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=PROJECT_ROOT / "database" / "chembl_37.db")
    parser.add_argument("--staging-path", type=Path)
    parser.add_argument("--batch-size", type=int, default=25000)
    parser.add_argument("--expected-row-count", type=int)
    parser.add_argument("--rollback", action="store_true", help="Restore the previous retained selectivity table")
    parser.add_argument("--backup-table", help="Restore this retained table instead of the most recent one")
    args = parser.parse_args()
    if args.rollback:
        result = rollback_selectivity(args.db_path, args.backup_table)
    else:
        result = rebuild_selectivity(args.db_path, args.staging_path, args.batch_size, args.expected_row_count)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
