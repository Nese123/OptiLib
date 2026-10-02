"""Prepare and read an indexed ChEMBL target identifier lookup."""

import sqlite3


TARGET_LOOKUP_VERSION = "1"
_SYNONYM_TYPES = {"GENE_SYMBOL", "UNIPROT", "EC_NUMBER"}


def rebuild_target_lookup(conn):
    """Atomically refresh derived metadata during offline database preparation.

    Return the number of identifiers, or None for non-ChEMBL databases.
    Callers must stop database readers and writers before preparation.
    """
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='target_dictionary'"
    ).fetchone() is None:
        return None

    conn.execute("SAVEPOINT prepare_target_lookup")
    try:
        conn.execute("DROP TABLE IF EXISTS optilib_target_lookup_build")
        conn.execute("DROP TABLE IF EXISTS optilib_target_lookup_rows_build")
        conn.execute("""
            CREATE TABLE optilib_target_lookup_build (
                identifier TEXT COLLATE NOCASE NOT NULL,
                row_number INTEGER NOT NULL,
                PRIMARY KEY (identifier, row_number)
            ) WITHOUT ROWID
        """)
        conn.execute("""
            CREATE TABLE optilib_target_lookup_rows_build (
                row_number INTEGER PRIMARY KEY,
                chembl_id TEXT NOT NULL,
                pref_name TEXT NOT NULL,
                gene_symbol TEXT NOT NULL,
                accession TEXT NOT NULL,
                synonym TEXT,
                syn_type TEXT
            )
        """)
        rows = conn.execute("""
            SELECT td.chembl_id, td.pref_name,
                   (SELECT sy.component_synonym
                    FROM target_components tc2
                    JOIN component_synonyms sy ON sy.component_id = tc2.component_id
                    WHERE tc2.tid = td.tid AND sy.syn_type = 'GENE_SYMBOL'
                    LIMIT 1) AS gene_symbol,
                   cs.accession, csy.component_synonym, csy.syn_type
            FROM target_dictionary td
            LEFT JOIN target_components tc ON tc.tid = td.tid
            LEFT JOIN component_sequences cs ON cs.component_id = tc.component_id
            LEFT JOIN component_synonyms csy ON csy.component_id = cs.component_id
            WHERE td.target_type = 'SINGLE PROTEIN' AND td.organism = 'Homo sapiens'
            ORDER BY td.tid
        """)
        insert = "INSERT OR IGNORE INTO optilib_target_lookup_build VALUES (?,?)"
        row_number = 0
        while batch := rows.fetchmany(1000):
            identifiers, records = [], []
            for cid, name, gene, accession, synonym, syn_type in batch:
                row_number += 1
                records.append((row_number, cid or "", name or "", gene or "", accession or "", synonym, syn_type))
                aliases = [cid, name, accession]
                if syn_type in _SYNONYM_TYPES:
                    aliases.append(synonym)
                for alias in aliases:
                    if alias:
                        identifiers.append((str(alias), row_number))
            conn.executemany("INSERT INTO optilib_target_lookup_rows_build VALUES (?,?,?,?,?,?,?)", records)
            conn.executemany(insert, identifiers)
        count = conn.execute("SELECT count(DISTINCT identifier) FROM optilib_target_lookup_build").fetchone()[0]
        conn.execute("DROP TABLE IF EXISTS optilib_target_lookup")
        conn.execute("DROP TABLE IF EXISTS optilib_target_lookup_rows")
        conn.execute("ALTER TABLE optilib_target_lookup_build RENAME TO optilib_target_lookup")
        conn.execute("ALTER TABLE optilib_target_lookup_rows_build RENAME TO optilib_target_lookup_rows")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS optilib_target_lookup_metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT OR REPLACE INTO optilib_target_lookup_metadata VALUES ('schema_version', ?)
        """, (TARGET_LOOKUP_VERSION,))
        conn.execute("RELEASE prepare_target_lookup")
    except Exception:
        conn.execute("ROLLBACK TO prepare_target_lookup")
        conn.execute("RELEASE prepare_target_lookup")
        raise
    return count


def lookup_target_rows(conn, identifiers):
    """Return indexed matches, or None when an older database needs the joins."""
    try:
        version = conn.execute("""
            SELECT value FROM optilib_target_lookup_metadata WHERE key='schema_version'
        """).fetchone()
        if version is None or version[0] != TARGET_LOOKUP_VERSION:
            return None
        # Keep the original query's batch order when an alias is ambiguous.
        search_set = set()
        for identifier in identifiers:
            search_set.update((identifier, identifier.upper(), identifier.lower()))
        keys = list(search_set)
        by_identifier = {}
        for start in range(0, len(keys), 500):
            batch = keys[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(f"""
                SELECT DISTINCT r.row_number, r.chembl_id, r.pref_name, r.gene_symbol,
                                r.accession, r.synonym, r.syn_type
                FROM optilib_target_lookup AS lookup
                JOIN optilib_target_lookup_rows AS r ON r.row_number=lookup.row_number
                WHERE lookup.identifier IN ({placeholders})
                ORDER BY r.row_number
            """, batch).fetchall()
            for row_number, *record in rows:
                row = tuple(record)
                for identifier in row[:5]:
                    if identifier:
                        by_identifier.setdefault(str(identifier).lower(), row)
        return by_identifier
    except sqlite3.OperationalError:
        # Missing, incompatible or partly prepared tables use the original query.
        return None
