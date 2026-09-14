"""Offline WAL checkpoint for databases that will be mounted read-only.

Stop all application, updater and migration processes before running this command.
"""
import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


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
        print(f'{path.name}: checkpointed and ready for read-only mounts')


if __name__=='__main__':main()
