"""Prepare indexed target lookups and checkpoint databases for read-only mounts.

Stop all application, updater and migration processes before running this command.
"""
import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from webapp.core.target_lookup import rebuild_target_lookup


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=Path(__file__).resolve().parents[1]/'database')
    args=parser.parse_args()
    for path in sorted(args.directory.glob('*.db')):
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=rw',uri=True,timeout=5)) as db:
            result=db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if result[0]:
                raise RuntimeError(f'{path.name}: active readers prevent checkpoint; stop services first')
            mode=db.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
            if mode.lower()!='delete':
                raise RuntimeError(f'{path.name}: could not switch to DELETE journal mode')
            count=rebuild_target_lookup(db)
            if count is not None:
                print(f'{path.name}: indexed {count:,} target identifiers')
        print(f'{path.name}: checkpointed and ready for read-only mounts')


if __name__=='__main__':main()
