"""Numerical invariants and reversible ChEMBL migration on tiny real databases."""

from contextlib import closing
from itertools import combinations
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

import numpy as np
import pandas as pd

from scripts.build_selectivity_table import (
    compute_blended_selectivity_fast,
    impute_activity_chunk,
    publish_shadow,
    rebuild_selectivity,
    rollback_selectivity,
    stage_affinities,
)
from webapp.core.chembl import get_selectivity_provenance
from webapp.core.selectivity import (
    SELECTIVITY_SCORING_VERSION,
    generate_selectivity_matrix,
    score_measured_affinities,
)


class SelectivityTieTests(unittest.TestCase):
    def test_cutoff_ties_average_all_valid_neighbor_choices(self):
        values = np.array([8., 6., 10., 9., 8., 10., 5., 9.])
        actual = score_measured_affinities(values)
        for index, value in enumerate(values):
            others = np.delete(values, index)
            choices = list(combinations(range(len(others)), 5))
            distances = [sum(abs(others[list(choice)] - value)) for choice in choices]
            best_distance = min(distances)
            means = [others[list(choice)].mean() for choice, distance in zip(choices, distances)
                     if distance == best_distance]
            expected = .5 * (value - others.mean()) + .5 * (value - np.mean(means))
            self.assertAlmostEqual(actual[index], expected)
        self.assertAlmostEqual(actual[0], -.40476190476190477)

    def test_order_missing_columns_and_builder_cannot_change_scores(self):
        values = np.array([8., 6., 10., 9., 8., 10., 5., 9.])
        expected = score_measured_affinities(values)
        cols = [0, 1, 3, 7, 8, 12, 13, 14]
        matrix = np.full((1, 15), np.nan)
        matrix[0, cols] = values
        np.testing.assert_array_equal(generate_selectivity_matrix(matrix)[0, cols], expected)
        np.testing.assert_array_equal(
            compute_blended_selectivity_fast(values, np.array([0, 8])), expected,
        )
        permutation = np.random.default_rng(7).permutation(8)
        np.testing.assert_array_equal(score_measured_affinities(values[permutation]), expected[permutation])
        subset = generate_selectivity_matrix(matrix, [cols[0], cols[4]])
        self.assertEqual(np.count_nonzero(~np.isnan(subset)), 2)
        np.testing.assert_array_equal(subset[0, [cols[0], cols[4]]], expected[[0, 4]])

    def test_singletons_missing_rows_and_h_larger_than_measurements(self):
        matrix = np.array([[np.nan, 7., np.nan], [np.nan, np.nan, np.nan], [5., 6., 8.]])
        actual = generate_selectivity_matrix(matrix, h=10)
        np.testing.assert_allclose(actual[0], [np.nan, 0., np.nan], equal_nan=True)
        self.assertTrue(np.isnan(actual[1]).all())
        np.testing.assert_allclose(actual[2], [-2., -.5, 2.5])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "chembl.db"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript("""
                CREATE TABLE target_dictionary (tid INTEGER PRIMARY KEY, target_type TEXT, organism TEXT);
                CREATE TABLE assays (assay_id INTEGER PRIMARY KEY, tid INTEGER, confidence_score INTEGER);
                CREATE TABLE activities (activity_id INTEGER PRIMARY KEY, assay_id INTEGER, molregno INTEGER,
                    pchembl_value REAL, activity_comment TEXT, standard_relation TEXT, standard_value, standard_units TEXT);
                CREATE TABLE molecule_hierarchy (molregno INTEGER PRIMARY KEY, parent_molregno INTEGER);
                CREATE TABLE version (name TEXT);
                INSERT INTO version VALUES ('ChEMBL test');
                CREATE TABLE compound_target_selectivity (molregno INTEGER, tid INTEGER,
                    selectivity_score REAL NOT NULL, PRIMARY KEY (molregno, tid));
                INSERT INTO molecule_hierarchy VALUES (101, 100);
            """)
            conn.executemany("INSERT INTO target_dictionary VALUES (?, 'SINGLE PROTEIN', 'Homo sapiens')", [(i,) for i in range(1, 9)])
            conn.executemany("INSERT INTO assays VALUES (?, ?, 9)", [(i, i) for i in range(1, 9)])
            conn.execute("INSERT INTO target_dictionary VALUES (9, 'SINGLE PROTEIN', 'Mus musculus')")
            conn.execute("INSERT INTO assays VALUES (9, 9, 9)")
            rows = [(i, i, 100, value, None, None, None, None)
                    for i, value in enumerate([8., 6., 10., 9., 8., 10., 5., 9.], 1)]
            rows += [
                (20, 1, 101, 7., None, None, None, None),
                (21, 1, 101, 9., None, None, None, None),
                (22, 1, 200, 6., None, None, None, None),
                (23, 2, 200, None, 'Not Active', None, None, None),
                (24, 1, 300, None, None, '>=', 10, 'µM'),
                (25, 1, 400, None, None, '>', 2, 'nM'),
                (26, 9, 500, 9., None, None, None, None),
                (27, 1, 200, 7., None, None, None, None),
            ]
            conn.executemany("INSERT INTO activities VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            conn.executemany("INSERT INTO compound_target_selectivity VALUES (?, ?, -99.)", [(100, i) for i in range(1, 9)] + [(200, 1), (200, 2)])
            conn.commit()
            self.source_rows = conn.execute("SELECT * FROM activities ORDER BY activity_id").fetchall()

    def _rows(self, conn):
        return conn.execute("SELECT * FROM compound_target_selectivity ORDER BY molregno, tid").fetchall()

    def test_migration_preserves_keys_sources_and_can_rollback(self):
        result = rebuild_selectivity(self.db_path, batch_size=2, expected_row_count=10)
        self.assertEqual(result["scoring_version"], SELECTIVITY_SCORING_VERSION)
        self.assertEqual(result["row_count"], 10)
        self.assertEqual(result["compound_count"], 2)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(get_selectivity_provenance(conn), result)
            self.assertEqual(conn.execute("SELECT * FROM activities ORDER BY activity_id").fetchall(), self.source_rows)
            rows = self._rows(conn)
            self.assertAlmostEqual(rows[0][2], -.4048)
            self.assertEqual(rows[-2:], [(200, 1, 1.5), (200, 2, -1.5)])
            self.assertEqual(conn.execute("SELECT selectivity_score FROM " + result["previous_table"] + " LIMIT 1").fetchone()[0], -99.)
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(activities)")}
            self.assertIn("idx_optilib_activities_active_assay_molregno", indexes)
        restored = rollback_selectivity(self.db_path)
        self.assertEqual(restored["scoring_version"], "legacy_argpartition_v1")
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertTrue(all(row[2] == -99. for row in self._rows(conn)))
        # The replacement was retained too, allowing a rollback of the rollback.
        reapplied = rollback_selectivity(self.db_path)
        self.assertEqual(reapplied["build_id"], result["build_id"])

    def test_chunk_boundaries_do_not_change_exact_medians(self):
        with closing(sqlite3.connect(self.db_path)) as source, closing(sqlite3.connect(':memory:')) as stage:
            stage_affinities(source, stage, batch_size=1)
            self.assertEqual(stage.execute("SELECT pvalue FROM median_affinity WHERE molregno=100 AND tid=1").fetchone(), (8.,))
            self.assertEqual(stage.execute("SELECT pvalue FROM median_affinity WHERE molregno=200 AND tid=1").fetchone(), (6.5,))
            self.assertEqual(stage.execute("SELECT pvalue FROM median_affinity WHERE molregno=300 AND tid=1").fetchone(), (5.,))
            self.assertEqual(stage.execute("SELECT COUNT(*) FROM median_affinity WHERE molregno IN (400,500)").fetchone(), (0,))

    def test_key_mismatch_refuses_publication(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE compound_target_selectivity SET molregno=999 WHERE molregno=200")
            conn.commit()
        with self.assertRaisesRegex(ValueError, "keys differ"):
            rebuild_selectivity(self.db_path, batch_size=3)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(get_selectivity_provenance(conn)["scoring_version"], "legacy_argpartition_v1")
            self.assertTrue(all(row[2] == -99. for row in self._rows(conn)))

    def test_failed_atomic_swap_keeps_active_table_and_metadata(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            before = self._rows(conn)
            with self.assertRaises(sqlite3.OperationalError):
                publish_shadow(conn, "nonexistent_shadow", {"build_id": "failed"})
            self.assertEqual(self._rows(conn), before)
            self.assertEqual(get_selectivity_provenance(conn)["scoring_version"], "legacy_argpartition_v1")

    def test_publication_archives_provenance_after_acquiring_writer_lock(self):
        started = threading.Event()
        results = []
        errors = []

        def publish():
            try:
                with closing(sqlite3.connect(self.db_path, timeout=5.)) as conn:
                    conn.set_trace_callback(
                        lambda sql: started.set() if sql == "BEGIN IMMEDIATE" else None,
                    )
                    results.append(publish_shadow(conn, "ready_build", {
                        "build_id": "replacement", "scoring_version": SELECTIVITY_SCORING_VERSION,
                        "h": 5, "storage_decimals": 4,
                    }))
            except BaseException as error:
                errors.append(error)
                started.set()

        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("CREATE TABLE ready_build AS SELECT * FROM compound_target_selectivity")
            conn.execute("CREATE TABLE optilib_selectivity_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            previous = get_selectivity_provenance(conn)
            conn.execute("INSERT INTO optilib_selectivity_metadata VALUES ('provenance', ?)", (json.dumps(previous),))
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            previous["build_id"] = "concurrent-publication"
            conn.execute("UPDATE optilib_selectivity_metadata SET value=?", (json.dumps(previous),))
            worker = threading.Thread(target=publish)
            worker.start()
            try:
                self.assertTrue(started.wait(3.), "Publisher did not reach writer lock")
            finally:
                conn.commit()
                worker.join(5.)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            archived = conn.execute(
                "SELECT provenance_json FROM optilib_selectivity_backups WHERE table_name=?",
                (results[0]["previous_table"],),
            ).fetchone()[0]
            self.assertEqual(json.loads(archived)["build_id"], "concurrent-publication")

    def test_imputation_retains_explicit_measurements_and_unit_rules(self):
        frame = pd.DataFrame({
            "molregno": [1] * 8, "tid": list(range(8)),
            "pchembl_value": [7., None, None, None, None, None, None, None],
            "activity_comment": ['inactive', 'NO INHIBITION', None, None, None, None, None, None],
            "standard_relation": [None, None, '>', '>=', '>', '>', '<', '>'],
            "standard_value": [None, None, 10000, 10, .01, .00001, 100000, 'bad'],
            "standard_units": [None, None, 'nM', ' µM ', 'mM', 'M', 'nM', 'nM'],
        })
        values, count = impute_activity_chunk(frame)
        self.assertEqual(values['pvalue'].tolist(), [7., 5., 5., 5., 5., 5.])
        self.assertEqual(count, 5)


if __name__ == '__main__':
    unittest.main()
