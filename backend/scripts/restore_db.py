import os
import sys
import subprocess
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.environ.get("DATABASE_URL")
if not DB_URL:
    print("Error: DATABASE_URL not found in .env")
    exit(1)

if len(sys.argv) < 2:
    print("Usage: python restore_db.py <backup_file.sql>")
    exit(1)

backup_file = sys.argv[1]
if not os.path.exists(backup_file):
    print(f"Error: File {backup_file} not found.")
    exit(1)

print(f"WARNING: This will overwrite the current database with {backup_file}.")
confirm = input("Type 'RESTORE' to confirm: ")
if confirm != "RESTORE":
    print("Aborted.")
    exit(0)

print("Restoring database...")
try:
    subprocess.run(["psql", DB_URL, "-f", backup_file], check=True)
    print("Restore completed successfully.")
except FileNotFoundError:
    print("Error: psql not found. Please install PostgreSQL client tools.")
except subprocess.CalledProcessError as e:
    print(f"Error during restore: {e}")
