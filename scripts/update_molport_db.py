import os
import sys
import ftplib
import sqlite3
import gzip
import shutil
import tempfile
from pathlib import Path
from rdkit import Chem

import re

FTP_HOST = "ftp.molport.com"
DB_PATH = Path(__file__).resolve().parent.parent / "database" / "molport.db"

def get_ftp_connection():
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        with open(env_path, "r") as f:
            for line in f:
                if line.strip() and not line.startswith('#'):
                    key, val = line.strip().split('=', 1)
                    os.environ.setdefault(key.strip(), val.strip().strip("'\""))

    user = os.environ.get("MOLPORT_FTP_USER")
    passwd = os.environ.get("MOLPORT_FTP_PASS")
    ftp_host = os.environ.get("MOLPORT_FTP_HOST", "ftp.molport.com")
    ftp_port_str = os.environ.get("MOLPORT_FTP_PORT", "21")
    
    try:
        ftp_port = int(ftp_port_str)
    except ValueError:
        print(f"Error: MOLPORT_FTP_PORT must be an integer, got '{ftp_port_str}'", file=sys.stderr)
        sys.exit(1)
        
    if not user or not passwd:
        print("Error: MOLPORT_FTP_USER and MOLPORT_FTP_PASS environment variables are required.", file=sys.stderr)
        print("Please export them or add them to a .env file in the root directory before running the script:", file=sys.stderr)
        print("MOLPORT_FTP_USER='your_username'", file=sys.stderr)
        print("MOLPORT_FTP_PASS='your_password'", file=sys.stderr)
        print("MOLPORT_FTP_HOST='your_ftp_host' # optional, defaults to ftp.molport.com", file=sys.stderr)
        print("MOLPORT_FTP_PORT='21' # optional, defaults to 21", file=sys.stderr)
        sys.exit(1)
    
    print(f"Connecting to {ftp_host}:{ftp_port} (timeout=30s)...")
    try:
        ftp = ftplib.FTP()
        ftp.connect(ftp_host, ftp_port, timeout=30)
        ftp.login(user=user, passwd=passwd)
        ftp.set_pasv(True) # explicitly enable passive mode
        return ftp
    except ftplib.all_errors as e:
        print(f"FTP connection failed: {e}", file=sys.stderr)
        sys.exit(1)

def get_latest_ftp_dir(ftp):
    base_dir = "/FTP_PROFESSIONAL"
    try:
        ftp.cwd(base_dir)
    except ftplib.error_perm:
        print(f"Error: Could not change directory to {base_dir}")
        sys.exit(1)
    
    names = ftp.nlst()
    month_dirs = [n for n in names if re.match(r"^\d{4}-\d{2}$", n)]
    if not month_dirs:
        print(f"Error: No YYYY-MM directories found in {base_dir}")
        sys.exit(1)
    
    latest_month = sorted(month_dirs)[-1]
    full_dir = f"{base_dir}/{latest_month}/All Stock Compounds/Changed Since Previous Update"
    print(f"Identified latest update directory: {full_dir}")
    return full_dir

def download_file(ftp, filename, dest_dir, target_dir):
    try:
        ftp.cwd(target_dir)
    except ftplib.error_perm:
        print(f"Warning: Could not change directory to {target_dir}. Trying in current directory.")

    dest_path = Path(dest_dir) / filename
    print(f"Downloading {filename}...")
    try:
        with open(dest_path, 'wb') as fp:
            ftp.retrbinary(f"RETR {filename}", fp.write)
        return dest_path
    except ftplib.error_perm as e:
        print(f"Error downloading {filename}: {e}", file=sys.stderr)
        if dest_path.exists():
            dest_path.unlink()
        return None

def process_removals(db_conn, removed_file_gz):
    print(f"Processing removals from {removed_file_gz.name}...")
    ids_to_remove = []
    with gzip.open(removed_file_gz, 'rt', encoding='utf-8') as f:
        for line in f:
            molport_id = line.strip()
            if molport_id:
                # Format to match existing DB entries like "Molport-000-000-274"
                if molport_id[0].isdigit():
                    molport_id = f"Molport-{molport_id}"
                if molport_id.startswith("MolPort-"):
                    molport_id = "Molport-" + molport_id[8:]
                ids_to_remove.append((molport_id,))
    
    if ids_to_remove:
        cursor = db_conn.cursor()
        cursor.executemany("DELETE FROM compounds WHERE MOLPORTID = ?", ids_to_remove)
        print(f"Removed {cursor.rowcount} compounds from database (out of {len(ids_to_remove)} in file).")

def process_additions(db_conn, added_file_gz):
    print(f"Processing additions from {added_file_gz.name}...")
    cursor = db_conn.cursor()
    
    count = 0
    with gzip.open(added_file_gz, 'rt', encoding='utf-8') as f:
        for line in f:
            parts = line.strip().split()
            if not parts or len(parts) < 2:
                continue
            
            part0, part1 = parts[0], parts[1]
            
            # Infer ID and SMILES
            if "MolPort-" in part0 or part0.count('-') == 2 and part0.replace('-', '').isdigit():
                molport_id = part0
                smiles = part1
            elif "MolPort-" in part1 or part1.count('-') == 2 and part1.replace('-', '').isdigit():
                molport_id = part1
                smiles = part0
            else:
                if len(part0) < len(part1):
                    molport_id, smiles = part0, part1
                else:
                    molport_id, smiles = part1, part0
            
            if molport_id.startswith("MolPort-"):
                molport_id = "Molport-" + molport_id[8:]
            elif not molport_id.lower().startswith("molport-"):
                molport_id = "Molport-" + molport_id
            
            mol = Chem.MolFromSmiles(smiles)
            inchikey = None
            if mol:
                inchikey = Chem.MolToInchiKey(mol)
            
            cursor.execute("""
                INSERT OR IGNORE INTO compounds (MOLPORTID, INCHIKEY, SMILES_CANONICAL, PRICE_1MG)
                VALUES (?, ?, ?, NULL)
            """, (molport_id, inchikey, smiles))
            count += 1
            if count % 10000 == 0:
                print(f"Processed {count} added compounds...")
                
    print(f"Total processed {count} additions.")

def main():
    if not DB_PATH.exists():
        print(f"Error: Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
        
    temp_dir = tempfile.mkdtemp(prefix="molport_update_")
    try:
        ftp = get_ftp_connection()
        target_dir = get_latest_ftp_dir(ftp)
        
        removed_file = download_file(ftp, "lmiis_removed.txt.gz", temp_dir, target_dir)
        added_file = download_file(ftp, "lmiis_added_smiles.txt.gz", temp_dir, target_dir)
        
        ftp.quit()
        print("FTP connection closed.")
        
        print(f"Connecting to database {DB_PATH}...")
        conn = sqlite3.connect(str(DB_PATH), timeout=120.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout = 60000;")
        
        try:
            if removed_file and removed_file.exists():
                process_removals(conn, removed_file)
            else:
                print("No removals file to process.")
            
            if added_file and added_file.exists():
                process_additions(conn, added_file)
            else:
                print("No additions file to process.")
                
            print("Committing changes to database...")
            conn.commit()
            print("Database updated successfully.")
        except Exception as e:
            conn.rollback()
            print(f"Error during database update, transaction rolled back: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            conn.close()
            
    finally:
        shutil.rmtree(temp_dir)
        print("Cleaned up temporary files.")

if __name__ == "__main__":
    main()
