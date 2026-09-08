#!/usr/bin/env python3
"""
Generate and populate the compound_target_selectivity table in ChEMBL 37.

Criteria & Inactivity Imputation Rules:
- Target: Homo sapiens single protein targets (td.target_type = 'SINGLE PROTEIN' AND td.organism = 'Homo sapiens')
- Assays: High confidence assays (confidence_score IN (8, 9))
- Activities:
  1. Valid affinity measurements (pchembl_value IS NOT NULL).
  2. If pchembl_value IS NULL and activity_comment indicates inactivity (e.g. 'inactive', 'not active', 'no activity', 'inhibition < 50%'): set pchembl_value = 5.0.
  3. If pchembl_value IS NULL and standard_relation IN ('>', '>=') with molar concentration >= 10,000 nM (10 uM): set pchembl_value = 5.0.
- Molecules: Grouped by parent molecule (COALESCE(molecule_hierarchy.parent_molregno, activities.molregno))
- Per-target affinity: Median pchembl_value per (parent_molregno, tid) pair
- Compound filter: Only compounds with at least one median pchembl_value > 5.0 are retained
- Selectivity formula: OptiLib blended formula (50% global mean diff + 50% top-5 nearest neighbors mean diff; 0.0 for 1-target compounds)
- Output table: compound_target_selectivity (molregno INTEGER, tid INTEGER, selectivity_score REAL, PRIMARY KEY (molregno, tid))
- Indexes: idx_cts_tid, idx_cts_molregno, idx_cts_score
"""

import os
import sys
import time
import sqlite3
import argparse
import logging
from pathlib import Path
import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("build_selectivity")


def compute_blended_selectivity_fast(pvals: np.ndarray, group_starts: np.ndarray, h: int = 5) -> np.ndarray:
    """
    Compute blended selectivity scores matching webapp/core/selectivity.py with high performance.
    
    Mathematical equivalence:
    - For compounds with count == 1: score = 0.0
    - For compounds with count <= (h + 1): top-h nearest neighbors == all other neighbors,
      so local_diff == global_diff, and blended score == global_diff.
    - For compounds with count > (h + 1): compute top-h NN partition.
    """
    total_entries = len(pvals)
    scores = np.zeros(total_entries, dtype=np.float32)
    num_compounds = len(group_starts) - 1

    for idx in range(num_compounds):
        start_i = group_starts[idx]
        end_i = group_starts[idx + 1]
        n = end_i - start_i

        if n <= 1:
            continue

        sub_pvals = pvals[start_i:end_i]
        total_sum = np.sum(sub_pvals)
        other_count = n - 1

        # Global potency difference for all targets in this compound
        global_diffs = sub_pvals - ((total_sum - sub_pvals) / other_count)

        if n <= (h + 1):
            # When other_count <= h, top-h NN are all other targets -> local_diff == global_diff
            scores[start_i:end_i] = global_diffs.astype(np.float32)
        else:
            # Compound with > (h+1) targets: compute top-h nearest neighbors
            sub_scores = np.empty(n, dtype=np.float32)
            for j in range(n):
                val = sub_pvals[j]
                diffs = np.abs(sub_pvals - val)
                diffs[j] = np.inf  # exclude self
                nearest_idx = np.argpartition(diffs, h - 1)[:h]
                local_diff = val - np.mean(sub_pvals[nearest_idx])
                sub_scores[j] = 0.5 * local_diff + 0.5 * global_diffs[j]
            scores[start_i:end_i] = sub_scores

    return scores


def main():
    parser = argparse.ArgumentParser(description="Build compound_target_selectivity table in ChEMBL database.")
    parser.add_argument(
        "--db-path",
        type=str,
        default="/home/nese/OptiLib/database/chembl_37.db",
        help="Path to ChEMBL SQLite database file",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=250000,
        help="Batch size for database insertions",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path).resolve()
    if not db_path.exists():
        logger.error(f"Database not found at {db_path}")
        sys.exit(1)

    logger.info(f"Connecting to database: {db_path}")
    t_start = time.time()

    conn = sqlite3.connect(str(db_path), timeout=120.0)
    cursor = conn.cursor()

    # Apply SQLite performance PRAGMAs for fast bulk processing
    cursor.execute("PRAGMA journal_mode = WAL;")
    cursor.execute("PRAGMA synchronous = NORMAL;")
    cursor.execute("PRAGMA cache_size = -2097152;")  # 2GB cache
    cursor.execute("PRAGMA temp_store = MEMORY;")
    cursor.execute("PRAGMA mmap_size = 30000000000;")

    # 1. Fetch qualified human single-protein assays using indexed temp table
    logger.info("Step 1: Identifying qualified assays (Homo sapiens single protein targets, confidence 8 or 9)...")
    t0 = time.time()
    cursor.execute("DROP TABLE IF EXISTS temp_human_tids;")
    cursor.execute("CREATE TEMP TABLE temp_human_tids (tid INTEGER PRIMARY KEY);")
    cursor.execute("""
        INSERT INTO temp_human_tids (tid)
        SELECT tid FROM target_dictionary
        WHERE target_type = 'SINGLE PROTEIN' AND organism = 'Homo sapiens';
    """)
    cursor.execute("SELECT COUNT(*) FROM temp_human_tids;")
    target_count = cursor.fetchone()[0]

    cursor.execute("DROP TABLE IF EXISTS temp_qualified_assays;")
    cursor.execute("CREATE TEMP TABLE temp_qualified_assays (assay_id INTEGER PRIMARY KEY, tid INTEGER);")
    cursor.execute("""
        INSERT INTO temp_qualified_assays (assay_id, tid)
        SELECT a.assay_id, a.tid
        FROM assays a
        JOIN temp_human_tids t ON a.tid = t.tid
        WHERE a.confidence_score IN (8, 9);
    """)
    cursor.execute("SELECT COUNT(*) FROM temp_qualified_assays;")
    assay_count = cursor.fetchone()[0]
    cursor.execute("DROP TABLE IF EXISTS temp_human_tids;")
    logger.info(f"Found {assay_count:,} assays across {target_count:,} human single-protein targets in {time.time() - t0:.2f}s")

    # 2. Extract activities and apply pChEMBL = 5.0 inactivity imputation rules
    logger.info("Step 2: Extracting activities, resolving molecule parents, and applying inactivity imputation...")
    t0 = time.time()
    query_activities = """
        SELECT 
            COALESCE(mh.parent_molregno, act.molregno) AS molregno,
            qa.tid,
            act.pchembl_value,
            act.activity_comment,
            act.standard_relation,
            act.standard_value,
            act.standard_units
        FROM temp_qualified_assays qa
        JOIN activities act ON qa.assay_id = act.assay_id
        LEFT JOIN molecule_hierarchy mh ON act.molregno = mh.molregno
        WHERE act.pchembl_value IS NOT NULL
           OR act.activity_comment IS NOT NULL
           OR act.standard_relation IN ('>', '>=');
    """
    df = pd.read_sql_query(query_activities, conn)
    logger.info(f"Extracted {len(df):,} candidate activity rows in {time.time() - t0:.2f}s")

    # Clean up temp table
    cursor.execute("DROP TABLE IF EXISTS temp_qualified_assays;")

    # Apply imputation rules
    t_imp = time.time()
    pchembl = df["pchembl_value"].to_numpy(dtype=np.float64, copy=True)
    nan_mask = np.isnan(pchembl)

    # Inactivity comments check
    comments = df["activity_comment"].astype(str).str.lower()
    inactive_comment_mask = (
        comments.str.contains("inactive", na=False) |
        comments.str.contains("not active", na=False) |
        comments.str.contains("no activity", na=False) |
        comments.str.contains("no inhibition", na=False) |
        comments.str.contains("non-active", na=False) |
        comments.str.contains("inhibition < 50%", na=False) |
        comments.str.contains("inhibition <50%", na=False)
    )

    # Standard relation > or >= with molar concentration >= 10,000 nM (10 uM)
    std_rel = df["standard_relation"].astype(str)
    std_val = pd.to_numeric(df["standard_value"], errors="coerce").to_numpy(dtype=np.float64)
    std_units = df["standard_units"].astype(str).str.strip().str.lower()

    val_in_nm = np.full_like(std_val, np.nan)
    is_nm = std_units.isin(["nm", "nmol/l", "nmol.l-1"])
    val_in_nm[is_nm] = std_val[is_nm]

    is_um = std_units.isin(["um", "µm", "umol/l", "umol.l-1", "um/ml", "umol"])
    val_in_nm[is_um] = std_val[is_um] * 1000.0

    is_mm = std_units.isin(["mm", "mmol/l", "mmol.l-1"])
    val_in_nm[is_mm] = std_val[is_mm] * 1000000.0

    is_m = std_units.isin(["m", "mol/l", "mol.l-1"])
    val_in_nm[is_m] = std_val[is_m] * 1000000000.0

    greater_mask = std_rel.isin([">", ">="]) & (val_in_nm >= 10000.0)

    # Impute pchembl = 5.0 for qualified inactive records
    impute_mask = nan_mask & (inactive_comment_mask | greater_mask)
    pchembl[impute_mask] = 5.0

    valid_mask = ~np.isnan(pchembl)
    df_act = df.loc[valid_mask, ["molregno", "tid"]].copy()
    df_act["pchembl_value"] = pchembl[valid_mask]
    del df, pchembl, nan_mask, comments, inactive_comment_mask, std_rel, std_val, std_units, val_in_nm, greater_mask

    logger.info(
        f"Imputed {int(impute_mask.sum()):,} inactive rows (pchembl=5.0). "
        f"Total valid activities: {len(df_act):,} in {time.time() - t_imp:.2f}s"
    )

    # 3. Aggregate median pchembl_value per (molregno, tid)
    logger.info("Step 3: Aggregating median pChEMBL value per (molregno, tid) pair...")
    t0 = time.time()
    df_median = df_act.groupby(["molregno", "tid"], as_index=False)["pchembl_value"].median()
    del df_act  # Free memory immediately
    logger.info(f"Aggregated into {len(df_median):,} unique (molregno, tid) pairs across {df_median['molregno'].nunique():,} unique compounds in {time.time() - t0:.2f}s")

    # 3b. Filter: keep only compounds with at least one median pchembl_value > 5.0
    logger.info("Step 3b: Filtering out compounds with no median pChEMBL > 5.0...")
    t0 = time.time()
    pre_filter_compounds = df_median['molregno'].nunique()
    pre_filter_pairs = len(df_median)
    max_pchembl = df_median.groupby('molregno')['pchembl_value'].transform('max')
    df_median = df_median[max_pchembl > 5.0].reset_index(drop=True)
    del max_pchembl
    post_filter_compounds = df_median['molregno'].nunique()
    post_filter_pairs = len(df_median)
    logger.info(
        f"Removed {pre_filter_compounds - post_filter_compounds:,} compounds "
        f"({pre_filter_pairs - post_filter_pairs:,} pairs) with max median pChEMBL <= 5.0. "
        f"Remaining: {post_filter_pairs:,} pairs across {post_filter_compounds:,} compounds "
        f"in {time.time() - t0:.2f}s"
    )

    # 4. Compute blended selectivity scores
    logger.info("Step 4: Computing blended selectivity scores...")
    t0 = time.time()

    # Sort by molregno for contiguous grouping
    df_median.sort_values(by=["molregno", "tid"], inplace=True)
    molregnos = df_median["molregno"].to_numpy()
    pvals = df_median["pchembl_value"].to_numpy(dtype=np.float64)

    # Find boundaries of each compound group
    change_mask = np.concatenate(([True], molregnos[1:] != molregnos[:-1], [True]))
    group_starts = np.flatnonzero(change_mask)

    total_compounds = len(group_starts) - 1
    logger.info(f"Processing selectivity for {total_compounds:,} compounds...")
    df_median["selectivity_score"] = compute_blended_selectivity_fast(pvals, group_starts, h=5)
    del molregnos, pvals, change_mask, group_starts  # Free memory
    logger.info(f"Computed selectivity scores in {time.time() - t0:.2f}s")

    # 5. Create table in chembl_37.db and bulk insert
    logger.info("Step 5: Creating compound_target_selectivity table and populating data...")
    t0 = time.time()

    cursor.execute("DROP TABLE IF EXISTS compound_target_selectivity;")
    cursor.execute("""
        CREATE TABLE compound_target_selectivity (
            molregno INTEGER NOT NULL,
            tid INTEGER NOT NULL,
            selectivity_score REAL NOT NULL,
            PRIMARY KEY (molregno, tid)
        );
    """)

    insert_sql = "INSERT INTO compound_target_selectivity (molregno, tid, selectivity_score) VALUES (?, ?, ?);"
    
    records = list(zip(
        df_median["molregno"].tolist(),
        df_median["tid"].tolist(),
        [round(float(s), 4) for s in df_median["selectivity_score"].tolist()]
    ))
    del df_median

    total_rows = len(records)
    batch_size = args.batch_size
    for i in range(0, total_rows, batch_size):
        batch = records[i:i + batch_size]
        cursor.executemany(insert_sql, batch)
        conn.commit()
        if (i // batch_size) % 4 == 0 or i + batch_size >= total_rows:
            logger.info(f"Inserted {min(i + batch_size, total_rows):,} / {total_rows:,} rows...")

    del records
    logger.info(f"Inserted all {total_rows:,} rows in {time.time() - t0:.2f}s")

    # 6. Build Indexes and Analyze
    logger.info("Step 6: Building B-Tree indexes on compound_target_selectivity...")
    t0 = time.time()

    logger.info("Creating index idx_cts_tid...")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cts_tid ON compound_target_selectivity (tid);")

    logger.info("Creating index idx_cts_molregno...")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cts_molregno ON compound_target_selectivity (molregno);")

    logger.info("Creating index idx_cts_score...")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cts_score ON compound_target_selectivity (selectivity_score);")

    logger.info("Running ANALYZE for query planner optimization...")
    cursor.execute("ANALYZE compound_target_selectivity;")
    conn.commit()
    logger.info(f"Built all indexes in {time.time() - t0:.2f}s")

    # 7. Verification Summary
    logger.info("Step 7: Verification and Summary Statistics...")
    cursor.execute("SELECT COUNT(*), COUNT(DISTINCT molregno), COUNT(DISTINCT tid), MIN(selectivity_score), AVG(selectivity_score), MAX(selectivity_score) FROM compound_target_selectivity;")
    row_cnt, mol_cnt, tid_cnt, min_s, avg_s, max_s = cursor.fetchone()
    
    logger.info("=" * 60)
    logger.info("  compound_target_selectivity TABLE CREATED SUCCESSFULLY")
    logger.info("=" * 60)
    logger.info(f"  Total Rows:             {row_cnt:,}")
    logger.info(f"  Unique Compounds:       {mol_cnt:,}")
    logger.info(f"  Unique Targets:         {tid_cnt:,}")
    logger.info(f"  Min Selectivity Score:  {min_s:.4f}")
    logger.info(f"  Avg Selectivity Score:  {avg_s:.4f}")
    logger.info(f"  Max Selectivity Score:  {max_s:.4f}")
    logger.info(f"  Total Execution Time:   {time.time() - t_start:.2f}s")
    logger.info("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
