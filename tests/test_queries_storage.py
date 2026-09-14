"""ChEMBL candidate parity and complete, content-specific matrix exports."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import pandas as pd

from webapp.core.queries import read_chembl_candidates
from webapp.core.storage import ensure_matrix_excel, publish_matrix


class CandidateQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "chembl.db"
        with closing(sqlite3.connect(self.database)) as conn:
            conn.executescript("""
                CREATE TABLE target_dictionary (
                    tid INTEGER PRIMARY KEY, chembl_id TEXT, pref_name TEXT,
                    target_type TEXT, organism TEXT
                );
                CREATE TABLE target_components (tid INTEGER, component_id INTEGER);
                CREATE TABLE component_synonyms (
                    component_id INTEGER, component_synonym TEXT, syn_type TEXT
                );
                CREATE TABLE assays (assay_id INTEGER, tid INTEGER, confidence_score INTEGER);
                CREATE TABLE activities (assay_id INTEGER, molregno INTEGER, pchembl_value REAL);
                CREATE TABLE molecule_dictionary (molregno INTEGER PRIMARY KEY, chembl_id TEXT, pref_name TEXT);
                CREATE TABLE molecule_hierarchy (molregno INTEGER, parent_molregno INTEGER);
                CREATE TABLE compound_structures (molregno INTEGER, canonical_smiles TEXT, standard_inchi_key TEXT);
                CREATE TABLE compound_properties (molregno INTEGER, full_mwt REAL);
                CREATE TABLE compound_target_selectivity (
                    molregno INTEGER, tid INTEGER, selectivity_score REAL,
                    PRIMARY KEY (molregno, tid)
                );
            """)
            targets = [
                (tid, f"CHEMBL_T{tid}", f"Target {tid}",
                 "PROTEIN COMPLEX" if tid == 5 else "SINGLE PROTEIN",
                 "Mus musculus" if tid == 4 else "Homo sapiens")
                for tid in range(1, 10)
            ]
            conn.executemany("INSERT INTO target_dictionary VALUES (?, ?, ?, ?, ?)", targets)
            conn.executemany("INSERT INTO target_components VALUES (?, ?)", [(1, 11), (2, 12)])
            conn.executemany("INSERT INTO component_synonyms VALUES (?, ?, ?)", [
                (11, "Not a gene", "OTHER"), (11, "GENE1", "GENE_SYMBOL"),
            ])
            conn.executemany("INSERT INTO assays VALUES (?, ?, ?)", [
                (1, 1, 8), (2, 2, 9), (3, 3, 9), (4, 4, 9), (5, 5, 9),
                (6, 6, 7), (7, 7, 9), (8, 8, 8), (9, 9, 8),
            ])
            conn.executemany("INSERT INTO activities VALUES (?, ?, ?)", [
                (1, 101, 8), (1, 101, 8),  # Child potency maps to parent 100.
                (2, 200, 6), (1, 300, 7), (2, 400, 6),
                (3, 500, 5), (4, 600, 8), (5, 700, 8), (6, 800, 8),
                (1, 900, 5), (8, 1000, None), (9, 1100, 8), (7, 1200, 8),
            ])
            molecules = [100, 101, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200]
            conn.executemany("INSERT INTO molecule_dictionary VALUES (?, ?, ?)", [
                (mol, f"CHEMBL_M{mol}", f"Compound {mol}") for mol in molecules
            ])
            conn.execute("INSERT INTO molecule_hierarchy VALUES (101, 100)")
            conn.executemany("INSERT INTO compound_structures VALUES (?, ?, ?)", [
                (mol, f"SMILES_{mol}", f"KEY_{mol}") for mol in molecules if mol != 400
            ])
            conn.executemany("INSERT INTO compound_properties VALUES (?, ?)", [
                (mol, 200.0) for mol in molecules if mol != 400
            ])
            conn.executemany("INSERT INTO compound_target_selectivity VALUES (?, ?, ?)", [
                (100, 1, .2), (100, 2, .4), (100, 7, .9),
                (200, 1, .6), (200, 2, .1), (300, 1, .5), (300, 2, .49),
                (400, 1, .8), (500, 1, .9), (600, 1, .9), (700, 1, .9),
                (800, 1, .9), (900, 1, .9), (1000, 1, .9), (1200, 1, .9),
            ])
            conn.commit()

    def connect_readonly(self):
        conn = sqlite3.connect(f"{self.database.as_uri()}?mode=ro", uri=True)
        conn.execute("PRAGMA temp_store = FILE")
        conn.execute("BEGIN")
        return closing(conn)

    @staticmethod
    def legacy_candidates(conn, identifiers, threshold):
        """Original active-target and global compound-eligibility SQL."""
        placeholders = ",".join("?" for _ in identifiers)
        active = {row[0] for row in conn.execute(f"""
            SELECT DISTINCT td.chembl_id
            FROM target_dictionary td
            JOIN assays ass ON td.tid = ass.tid
            JOIN activities act ON act.assay_id = ass.assay_id
            WHERE td.chembl_id IN ({placeholders})
              AND td.target_type = 'SINGLE PROTEIN' AND td.organism = 'Homo sapiens'
              AND ass.confidence_score IN (8, 9) AND act.pchembl_value > 5.0
        """, [identifier.upper() for identifier in identifiers])}
        if not active:
            return pd.DataFrame(), active
        identifiers = sorted(active)
        placeholders = ",".join("?" for _ in identifiers)
        frame = pd.read_sql_query(f"""
            SELECT cts.molregno AS Clean_Molregno,
                   md.chembl_id AS Molecule_ChEMBL_ID, md.pref_name AS Compound_Name,
                   cs.canonical_smiles AS SMILES, cs.standard_inchi_key AS InChIKey,
                   cp.full_mwt AS MW, td.chembl_id AS Target_ChEMBL_ID,
                   td.pref_name AS Target_Pref_Name,
                   (SELECT sy.component_synonym FROM target_components tc
                    JOIN component_synonyms sy ON sy.component_id = tc.component_id
                    WHERE tc.tid = td.tid AND sy.syn_type = 'GENE_SYMBOL'
                    LIMIT 1) AS Target_Gene_Symbol,
                   cts.selectivity_score AS Selectivity_Score
            FROM compound_target_selectivity cts
            JOIN target_dictionary td ON td.tid = cts.tid
            JOIN molecule_dictionary md ON md.molregno = cts.molregno
            LEFT JOIN compound_structures cs ON cs.molregno = cts.molregno
            LEFT JOIN compound_properties cp ON cp.molregno = cts.molregno
            WHERE td.chembl_id IN ({placeholders})
              AND cts.molregno IN (
                  SELECT molregno FROM compound_target_selectivity WHERE selectivity_score > ?
              )
              AND cts.molregno IN (
                  SELECT DISTINCT COALESCE(mh.parent_molregno, md2.molregno)
                  FROM target_dictionary td2 JOIN assays ass ON td2.tid = ass.tid
                  JOIN activities act ON act.assay_id = ass.assay_id
                  JOIN molecule_dictionary md2 ON md2.molregno = act.molregno
                  LEFT JOIN molecule_hierarchy mh ON mh.molregno = md2.molregno
                  WHERE td2.chembl_id IN ({placeholders})
                    AND td2.target_type = 'SINGLE PROTEIN' AND td2.organism = 'Homo sapiens'
                    AND ass.confidence_score IN (8, 9) AND act.pchembl_value > 5.0
              )
        """, conn, params=identifiers + [threshold] + identifiers)
        return frame, active

    def test_requested_query_matches_legacy_eligibility_and_metadata(self):
        requested = [f"chembl_t{tid}" for tid in (1, 1, 2, 3, 4, 5, 6, 8, 9)] + ["UNKNOWN"]
        with self.connect_readonly() as conn:
            expected, expected_ids = self.legacy_candidates(conn, requested, .5)
            progress = []
            actual, actual_ids = read_chembl_candidates(conn, requested, .5, progress.append)
            self.assertTrue(conn.in_transaction)
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                conn.execute("DELETE FROM main.activities")
        sort = ["Clean_Molregno", "Target_ChEMBL_ID"]
        pd.testing.assert_frame_equal(
            actual.sort_values(sort).reset_index(drop=True),
            expected.sort_values(sort).reset_index(drop=True),
        )
        self.assertEqual(actual_ids, expected_ids)
        self.assertEqual(actual_ids, {"CHEMBL_T1", "CHEMBL_T2", "CHEMBL_T9"})
        self.assertEqual(set(actual["Clean_Molregno"]), {100, 200, 400})
        self.assertEqual(progress, [3])
        parent = actual.loc[actual["Clean_Molregno"] == 100]
        self.assertEqual(set(parent["Selectivity_Score"]), {.2, .4})
        self.assertTrue((parent["Molecule_ChEMBL_ID"] == "CHEMBL_M100").all())
        optional = actual.loc[actual["Clean_Molregno"] == 400]
        self.assertTrue(optional[["SMILES", "InChIKey", "MW"]].isna().all().all())
        self.assertEqual(actual.loc[actual["Target_ChEMBL_ID"] == "CHEMBL_T1", "Target_Gene_Symbol"].unique().tolist(), ["GENE1"])

    def test_selectivity_threshold_is_strict_and_rows_are_not_individually_pruned(self):
        for threshold, includes_equal in ((.5, False), (.49, True)):
            with self.subTest(threshold=threshold), self.connect_readonly() as conn:
                frame, _ = read_chembl_candidates(conn, ["CHEMBL_T1", "CHEMBL_T2"], threshold)
            self.assertEqual(300 in set(frame["Clean_Molregno"]), includes_equal)
            scores = frame.loc[frame["Clean_Molregno"] == 200, "Selectivity_Score"].tolist()
            self.assertCountEqual(scores, [.6, .1])

    def test_inactive_requests_are_empty_but_active_targets_can_have_no_candidates(self):
        for requested, expected_ids in ((["CHEMBL_T3", "CHEMBL_T4", "CHEMBL_T5", "CHEMBL_T6", "CHEMBL_T8"], set()),
                                        (["CHEMBL_T9"], {"CHEMBL_T9"})):
            with self.subTest(requested=requested), self.connect_readonly() as conn:
                frame, active_ids = read_chembl_candidates(conn, requested, .5)
            self.assertTrue(frame.empty)
            self.assertEqual(active_ids, expected_ids)


class MatrixStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.frame = pd.DataFrame({"SMILES": ["CC", "CCC"], "Price_USD_per_mg": [10., 20.], "Target": [.5, .7]})

    def test_content_and_scoring_version_identify_distinct_same_size_exports(self):
        original = publish_matrix(self.frame, self.output, scoring_version="v1")
        duplicate = publish_matrix(self.frame.copy(), self.output, scoring_version="v1")
        price_change = self.frame.copy()
        price_change.loc[0, "Price_USD_per_mg"] = 99.
        affinity_change = self.frame.copy()
        affinity_change.loc[0, "Target"] = 2.
        versions = [original,
                    publish_matrix(price_change, self.output, scoring_version="v1"),
                    publish_matrix(affinity_change, self.output, scoring_version="v1"),
                    publish_matrix(self.frame, self.output, scoring_version="v2")]
        self.assertEqual(original, duplicate)
        self.assertEqual(len(set(versions)), 4)
        pd.testing.assert_frame_equal(pd.read_csv(original), self.frame)

    def test_csv_is_replaced_only_after_complete_write(self):
        destination = Path(publish_matrix(self.frame, self.output, filename="matrix.csv"))
        previous = destination.read_bytes()
        changed = self.frame.assign(Target=[1., 2.])
        replace = os.replace

        def publish(source, target):
            self.assertEqual(destination.read_bytes(), previous)
            self.assertEqual(Path(source).parent, self.output)
            pd.testing.assert_frame_equal(pd.read_csv(source), changed)
            replace(source, target)

        with patch("webapp.core.storage.os.replace", side_effect=publish):
            publish_matrix(changed, self.output, filename="matrix.csv")
        pd.testing.assert_frame_equal(pd.read_csv(destination), changed)
        self.assertEqual(list(self.output.iterdir()), [destination])

    def test_csv_failures_preserve_previous_file_and_clean_temporary_output(self):
        destination = Path(publish_matrix(self.frame, self.output, filename="matrix.csv"))
        previous = destination.read_bytes()

        def fail(frame, path, **kwargs):
            Path(path).write_bytes(b"partial csv")
            raise OSError("write failed")

        for failure in (patch.object(pd.DataFrame, "to_csv", autospec=True, side_effect=fail),
                        patch("webapp.core.storage.os.replace", side_effect=OSError("replace failed"))):
            with failure, self.assertRaises(OSError):
                publish_matrix(self.frame, self.output, filename="matrix.csv")
            self.assertEqual(destination.read_bytes(), previous)
            self.assertEqual(list(self.output.iterdir()), [destination])

    def test_excel_failures_never_publish_partial_workbooks(self):
        csv_path = Path(publish_matrix(self.frame, self.output))
        excel_path = csv_path.with_suffix(".xlsx")
        previous = csv_path.read_bytes()

        def fail(columns, rows, path):
            Path(path).write_bytes(b"partial workbook")
            raise OSError("write failed")

        for failure in (patch("webapp.core.storage.write_rows_excel", side_effect=fail),
                        patch("webapp.core.storage.os.replace", side_effect=OSError("replace failed"))):
            with failure, self.assertRaises(OSError):
                ensure_matrix_excel(csv_path)
            self.assertFalse(excel_path.exists())
            self.assertEqual(csv_path.read_bytes(), previous)
            self.assertEqual(list(self.output.iterdir()), [csv_path])

    def test_parallel_excel_requests_reuse_one_complete_export_under_caller_lock(self):
        csv_path = Path(publish_matrix(self.frame, self.output))
        export_lock = threading.Lock()
        ready = threading.Barrier(4)
        writes = []
        from webapp.core.storage import write_rows_excel
        write_excel = write_rows_excel
        replace = os.replace

        def write(columns, rows, path):
            writes.append(path)
            return write_excel(columns, rows, path)

        def publish(source, destination):
            self.assertFalse(Path(destination).exists())
            pd.testing.assert_frame_equal(pd.read_excel(source), self.frame, check_dtype=False)
            replace(source, destination)

        def download(_):
            ready.wait(timeout=5)
            with export_lock:
                return ensure_matrix_excel(csv_path)

        with patch("webapp.core.storage.write_rows_excel", side_effect=write), \
                patch("webapp.core.storage.os.replace", side_effect=publish), \
                ThreadPoolExecutor(max_workers=4) as executor:
            paths = list(executor.map(download, range(4)))
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(set(self.output.iterdir()), {csv_path, Path(paths[0])})
        with patch("webapp.core.storage.pd.read_csv") as read:
            self.assertEqual(ensure_matrix_excel(csv_path), paths[0])
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
